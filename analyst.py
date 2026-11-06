"""Natural-language analytics on the governed semantic layer.

The AI model plans governed metric queries (query_metrics / list_values), then calls
present_answer with a headline, insights and chart specs that point at query results by id.
Every number comes from the semantic layer; charts are drawn by the app from those results.
"""
from __future__ import annotations

import ast
from pathlib import Path
import yaml
import copy
import json
import re
import operator
import time

import prompts
from semantic_layer import QueryError, SemanticLayer

MAX_TURNS = 10
MAX_RESULT_CHARS = 8000

ANALYST_RULES = """You are a bank data analyst answering questions about FDIC-insured banks for business users.

How to work:
- Get every number from query_metrics. Never invent data.
- Never rank in your head. Every "highest", "lowest", "leads", "best", "worst" or "second"
  claim must match the "ranking" block that comes with grouped results (computed by code).
  Remember which direction is better: a LOWER efficiency ratio, noncurrent loan rate or
  charge-off rate is better. Size, deposits, loans and the loan-to-deposit ratio have no
  "better" direction: say highest or lowest, not best or worst.
- For peer groups of holding companies by size, use the dimension organization_size_class
  (total assets of the whole organization), not size_class (each bank charter separately).
- For rankings or comparisons "by state", filter geography_type = "State or DC" unless the user asks
  about territories. Treat "Unknown" (banks with no institution record) as a data caveat, never as a peer.
- Industry and group TOTALS always include every filer, "Unknown" too (it is part of the industry). Leave
  "Unknown" out of rankings and charts only. For a total's growth, use query_metrics compare_to; never
  recompute it from rounded numbers. Feed calculate the full-precision values the results returned.
- Gainers and losers, or growth by group: ONE query_metrics call with compare_to and one dimension. Chart it
  as a bar with y = "change_pct" (or "change_pp" for ratios) so gains and losses show on each side of zero.
  Results are pro forma for mergers (acquired banks counted with their acquirer in every period), so growth is
  like for like. Say "pro forma for mergers" once when you report an organization's growth over time.
- Peer groups by place and size: us_region (e.g. "New England") and organization_asset_band (e.g. $5B-$50B =
  ["$5B-$10B", "$10B-$50B"]). Use these filters, never a hand-made list of states or companies.
- ROA for a quarter means return_on_assets_quarterly (the quarter annualized, as banks report it). Use
  return_on_assets (year-to-date) only when the user asks for year-to-date. Margin: net_interest_margin.
  Funding: cost_of_funds, cost_of_deposits, cost_of_interest_bearing_deposits (all quarterly, lower is better).
  These are bank-level call-report figures; they can differ slightly from a holding company's earnings release.
- Capital: cet1_ratio, tier1_risk_based_ratio, total_risk_based_ratio, leverage_ratio (bank level; usually
  above the holding company's ratios). Reserves: allowance_to_loans (no better direction) and
  allowance_to_noncurrent (coverage).
- Deposit mix: uninsured_deposit_share (call-report estimate, includes collateralized and affiliate deposits),
  brokered_deposit_share, noninterest_bearing_deposit_share, time_deposit_share, core_deposit_share.
- Loan mix (shares of gross loans, call-report categories, which differ from the segments banks use in
  earnings releases): cre_loan_share (construction + multifamily + nonfarm nonresidential, incl.
  owner-occupied), construction_loan_share, multifamily_loan_share, owner_occupied_cre_loan_share,
  non_owner_occupied_cre_loan_share, residential_loan_share, ci_loan_share, consumer_loan_share. No better
  direction. "CRE concentration" means cre_concentration: regulatory CRE (excl. owner-occupied) / total
  risk-based capital, bank level, so usually above a holding company's disclosed ratio. Supervisory guidance
  flags 300% (construction_concentration: 100%) for closer review; it is not a limit, so say "above the 300%
  guideline", never "in breach" or "violating". Lower is less concentrated; never call a ratio "best" or
  "worst". Credit by loan type: cre_noncurrent_rate, residential_noncurrent_rate, ci_noncurrent_rate,
  consumer_noncurrent_rate (lower is better).
- If a result has "peer_aggregate", say where the leaders and laggards sit against it (it is the whole
  queried group's aggregate, drawn as a line on the chart).
- If rows have merger_quarter true, the quarterly income figure for that organization and quarter is
  distorted by a merger (charges, purchase accounting). Say so in an insight, and do not call it a
  "best" or "worst" quarter or rank on it without that caveat.
- If a result has a "concentration" block, one bank drives that group's figure. Say so in an insight,
  name the bank, and give the group's rate without it (both numbers are in the block).
- Never do arithmetic in your head. Any number you state that query_metrics did not return
  (a multiple such as "1.7 times", a difference, a share, a percent change) must come from the
  calculate tool. For change over time, prefer query_metrics with compare_to.
- For trends, query a period range with dimension "period". Ratios (ROA, ROE, efficiency,
  noncurrent loan rate, etc.), balances (assets, deposits, loans) and counts cover every quarter
  from 2024Q3 to 2026Q2, so use 2024Q3:2026Q2 for them. Only quarterly income AMOUNTS (net income,
  net interest income, noninterest income/expense, provisions, charge-offs in dollars) start at
  2024Q4, because 2024Q3 would need 2024Q2 year-to-date data.
- For comparisons across segments (region, size class, regulator, charter type, state,
  community-bank flag, specialization), use dimensions. Combine "period" with one segment
  dimension for "trend by segment" questions.
- Each query_metrics result carries a result_id (q1, q2, ...). Charts must point at one of them.
- A chart plots every row of its result. Before presenting, make sure some result contains exactly
  the items your answer discusses. If you narrowed a broad result down in your reasoning (for example
  to four peer banks), run one more query_metrics filtered to those items, e.g.
  filters {"holding_company": ["NAME 1", "NAME 2"]}, and chart that result, or set the chart's
  "only" list to exactly those items. A chart must never show items your answer excluded.
- If the data cannot answer the question, call present_answer with answer_type "decline" and
  explain what the data does cover.
- Answer the business question; do not ask the user follow-up questions.
- Answer only the current question in headline and insights. If you notice an error in an
  earlier answer, put it in "correction", not in the insights.

When done, call present_answer exactly once:
- headline: one sentence that answers the question, with the key number and its unit.
- insights: 2 to 4 short sentences, each stating a specific observation backed by numbers from
  the results (largest/smallest, change over time, gaps between segments, notable outliers,
  data caveats such as the "Unknown" group). Do not speculate about causes.
- charts: 0 to 2 charts. Use "line" when x is "period", "bar" for comparing segments.
  y is one metric from that result; color is an optional second dimension in that result.
- follow_ups: 2 or 3 short, natural next questions this data can answer (a breakdown, a
  trend, a comparison), each under 70 characters."""

CALC_TOOL = {
    "name": "calculate",
    "description": ("Evaluate arithmetic on numbers you retrieved: + - * / and parentheses, e.g. "
                    "'25.9 / 15.3' or '(91.0 - 67.4) / 67.4 * 100'. Use it for every multiple, difference, "
                    "share or percent change you state that query_metrics did not return."),
    "input_schema": {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]},
}
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.USub: operator.neg, ast.UAdd: operator.pos}


def calculate(expression: str) -> float:
    """Safe arithmetic: numbers and + - * / ( ) only."""
    def ev(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.operand))
        raise ValueError("only numbers and + - * / ( ) are allowed")
    if len(expression) > 200:
        raise ValueError("expression too long")
    return round(ev(ast.parse(expression.replace(",", ""), mode="eval").body), 6)


PRESENT_TOOL = {
    "name": "present_answer",
    "description": "Present the final answer to the user. Call exactly once, at the end.",
    "input_schema": {
        "type": "object",
        "properties": {
            "answer_type": {"type": "string", "enum": ["answer", "decline"]},
            "headline": {"type": "string"},
            "insights": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
            "charts": {
                "type": "array", "maxItems": 2,
                "items": {
                    "type": "object",
                    "properties": {
                        "result_id": {"type": "string"},
                        "kind": {"type": "string", "enum": ["line", "bar"]},
                        "x": {"type": "string", "description": "A dimension in that result (e.g. period, region)."},
                        "y": {"type": "string", "description": "A metric in that result."},
                        "color": {"type": "string", "description": "Optional second dimension in that result."},
                        "title": {"type": "string"},
                        "only": {"type": "array", "items": {"type": "string"},
                                 "description": "Optional: the x values to show, if the result has rows your answer excluded."},
                    },
                    "required": ["result_id", "kind", "x", "y", "title"],
                },
            },
            "decline_reason": {"type": "string"},
            "follow_ups": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
            "correction": {"type": "string", "description": "Optional: a correction to an earlier answer in this conversation."},
        },
        "required": ["answer_type", "headline"],
    },
}


def _metrics_in(request: dict, rows: list) -> list:
    """The request's metrics plus any change columns a compare_to result added."""
    extra = [c for c in ("change_pct", "change_pp", "change") if rows and c in rows[0]]
    return [m for m in (request.get("metrics") or []) if not rows or m in rows[0]] + extra


NON_PEER = {"Unknown", "PR", "VI", "GU", "FM", "AS", "MP", "MH", "PW"}


def rankings(res: dict, request: dict) -> dict | None:
    """Highest/lowest per metric, computed in code, for results grouped by one non-period dimension.
    'Unknown' (and territories, when grouped by state) are listed separately, never ranked as peers."""
    rows = res.get("rows") or []
    dims = [d for d in request.get("dimensions") or [] if rows and d in rows[0]]
    if len(rows) < 2 or len(dims) != 1 or dims[0] == "period":
        return None
    d, out = dims[0], {}
    skip = NON_PEER if d == "state" else {"Unknown"}
    excluded = sorted({str(r[d]) for r in rows if str(r[d]) in skip})
    truncated = any("Only the first" in n for n in res.get("notes", []))
    if truncated:
        ob = (request.get("order_by") or "").split()
        if not ob or ob[0] not in (request.get("metrics") or []):
            return {"warning": "This result was cut off at the row limit, so it does not contain every group. "
                               "Do not rank from it. Narrow the query with filters (e.g. organization_size_class, "
                               "geography_type) or use order_by with a limit, then rank."}
    for m in _metrics_in(request, rows):
        vals = [(r[d], r[m]) for r in rows if isinstance(r.get(m), (int, float)) and str(r[d]) not in skip]
        if len(vals) < 2:
            continue
        ordered = sorted(vals, key=lambda t: t[1], reverse=True)
        entry = {"highest": [{d: k, m: round(v, 4)} for k, v in ordered[:3]],
                 "lowest": [{d: k, m: round(v, 4)} for k, v in ordered[-3:][::-1]],
                 "rank_order_high_to_low": [k for k, _ in ordered][:25], "of": len(ordered)}
        if truncated:                      # ordered and cut off: only the sorted end is complete
            ob = request["order_by"].split()
            if ob[0] != m:
                continue
            keep = "lowest" if len(ob) > 1 and ob[1].lower() == "asc" else "highest"
            entry = {keep: entry[keep], "note": f"Sorted by {m} and cut off: only the {keep} end is complete."}
        out[m] = entry
    if out and excluded:
        out["excluded_from_ranking"] = {"groups": excluded,
                                        "why": "Unknown = banks with no institution record"
                                               + ("; the others are U.S. territories, not states" if d == "state" else "")}
    return out or None


# metric -> (amount whose share picks the driver bank, denominator, which ends of the ranking to check)
CONCENTRATION = {
    "noncurrent_loan_rate":  ("noncurrent_loans", "gross_loans", ("highest",)),
    "net_charge_off_rate":   ("net_charge_offs", "gross_loans", ("highest",)),
    "loan_to_deposit_ratio": ("total_deposits", "total_deposits", ("highest", "lowest")),
}
DRILL_DIMS = {"state", "region", "regulator", "size_class", "organization_size_class", "specialization",
              "charter_type", "fdic_community_bank", "geography_type", "us_region"}


def concentration(layer, res: dict, request: dict, ranks: dict | None):
    """For ratio rankings, check in code whether one bank drives an extreme group's ratio (e.g. Oklahoma's
    noncurrent rate = MidFirst; Massachusetts' loan-to-deposit ratio = State Street's custody deposits).
    Returns (drill request, drill result, block) or None."""
    dims = request.get("dimensions") or []
    if not ranks or len(dims) != 1 or dims[0] not in DRILL_DIMS or not isinstance(request.get("period"), str) \
            or ":" in request["period"]:
        return None
    d = dims[0]
    for rate, (amount, den, ends) in CONCENTRATION.items():
        for end in ends:
            top = (ranks.get(rate) or {}).get(end)
            if not top:
                continue
            group = top[0][d]
            filters = {**(request.get("filters") or {}), d: group}
            mets = list(dict.fromkeys([rate, amount, den]))
            req = {"metrics": mets, "period": request["period"], "dimensions": ["bank_name"],
                   "filters": filters, "order_by": f"{amount} desc", "limit": 3}
            tot = layer.query_metrics(metrics=mets, period=request["period"], filters=filters)
            drill = layer.query_metrics(**req)
            if not tot["rows"] or not drill["rows"] or len(drill["rows"]) < 2:
                continue
            g, b = tot["rows"][0], drill["rows"][0]
            if not g.get(amount) or g[amount] <= 0 or g.get(rate) is None or b.get(rate) is None:
                continue
            share = b[amount] / g[amount] * 100
            if share < 40:
                continue
            rest_den = g[den] - b[den]
            rest = (g[rate] * g[den] - b[rate] * b[den]) / rest_den if rest_den > 0 else None
            block = {"group": f"{d} = {group}", "metric": rate, "end": end, "group_rate": round(g[rate], 4),
                     "driver_bank": b["bank_name"], "driver_bank_rate": round(b[rate], 4),
                     "driver_share_of_group_" + amount + "_pct": round(share, 1),
                     "group_rate_without_driver": round(rest, 4) if rest is not None else None}
            return req, drill, block
    return None


def peer_benchmark(layer, request: dict, res: dict) -> dict | None:
    """The aggregate of the whole queried group (same filters and period, no grouping) for each percent
    metric, so ranked charts can show a peer line and the answer can say above or below peers."""
    dims = request.get("dimensions") or []
    period = request.get("period")
    if len(dims) != 1 or dims[0] == "period" or not isinstance(period, str) or ":" in period \
            or request.get("compare_to") or len(res.get("rows") or []) < 3:
        return None
    mets = [m for m in request.get("metrics") or [] if (res.get("units") or {}).get(m) == "percent"
            and not m.startswith(("median_", "mean_", "reported_"))]
    if not mets:
        return None
    rows = layer.query_metrics(metrics=mets, period=period, filters=request.get("filters"))["rows"]
    if not rows:
        return None
    out = {m: round(rows[0][m], 4) for m in mets if rows[0].get(m) is not None}
    return out or None


LOWER_IS_BETTER = {"efficiency_ratio", "noncurrent_loan_rate", "net_charge_off_rate", "median_bank_efficiency_ratio",
                   "cost_of_funds", "cost_of_deposits", "cost_of_interest_bearing_deposits",
                   "cre_noncurrent_rate", "residential_noncurrent_rate", "ci_noncurrent_rate", "consumer_noncurrent_rate"}
HIGHER_IS_BETTER = {"return_on_assets", "return_on_assets_quarterly", "net_interest_margin", "cet1_ratio",
                    "noninterest_bearing_deposit_share", "core_deposit_share",
                    "tier1_risk_based_ratio", "total_risk_based_ratio", "leverage_ratio", "allowance_to_noncurrent", "return_on_equity", "equity_to_assets", "mean_bank_roa", "median_bank_roa",
                    "median_bank_nim", "median_bank_leverage_ratio", "reported_roa", "reported_roe", "reported_nim",
                    "reported_leverage_ratio"}
_SUPER = re.compile(r"\b(second[- ]highest|second[- ]lowest|second[- ]best|highest|lowest|best|worst|"
                    r"led|leads|leading|strongest|weakest|largest|smallest|top|bottom)\b", re.I)
_NUM = re.compile(r"-?\d[\d,]*\.?\d*")
_SENT = re.compile(r"(?<=[.;!?])\s+")


def _sentence(text: str, sm) -> str:
    cuts = [0] + [m.end() for m in _SENT.finditer(text)] + [len(text)]
    return text[max(c for c in cuts if c <= sm.start()): min(c for c in cuts if c > sm.start())]


def _sentence_numbers(sent: str) -> list[tuple[float, int]]:
    out = []
    for m in _NUM.finditer(sent):
        raw = m.group().replace(",", "").rstrip(".")
        try:
            out.append((float(raw), len(raw.split(".")[1]) if "." in raw else 0))
        except ValueError:
            pass
    return out


def _claim_number(text: str, sm):
    """The number a superlative refers to: the nearest number in the same sentence, before or after it.
    Periods and years (2026Q2, 2025) are not candidates."""
    cuts = [0] + [m.end() for m in _SENT.finditer(text)] + [len(text)]
    start = max(c for c in cuts if c <= sm.start())
    end = min(c for c in cuts if c > sm.start())
    best = None
    for m in _NUM.finditer(text, start, end):
        if text[m.end(): m.end() + 1] in ("Q", "q") or text[max(0, m.start() - 1): m.start()] in ("Q", "q") \
                or re.fullmatch(r"(19|20)\d\d", m.group()):
            continue
        if sm.start() <= m.start() < sm.end():
            continue
        dist = m.start() - sm.end() if m.start() >= sm.end() else sm.start() - m.end()
        if dist <= 90:
            best = (best or []) + [(dist, m)]
    return [m for _, m in sorted(best, key=lambda t: t[0])] if best else []


def _direction(word: str, metric: str):
    """('max' or 'min', rank) for a superlative word applied to a metric; None if it has no clear direction."""
    w = word.lower().replace("-", " ")
    rank = 2 if w.startswith("second") else 1
    w = w.replace("second ", "")
    if w in ("highest", "largest", "top"):
        return "max", rank
    if w in ("lowest", "smallest", "bottom"):
        return "min", rank
    if metric not in LOWER_IS_BETTER and metric not in HIGHER_IS_BETTER:
        return None                                        # e.g. "best loan-to-deposit ratio": no governed direction
    better_low = metric in LOWER_IS_BETTER
    if w in ("best", "led", "leads", "leading", "strongest"):
        return ("min" if better_low else "max"), rank
    return ("max" if better_low else "min"), rank          # worst, weakest


def check_claims(answer: dict, results: dict) -> list[str]:
    """Check 'highest / lowest / best / led ...' claims against code-computed rankings."""
    index = []          # (value, entity, metric, vals, complete_end)  complete_end: None = all, "max"/"min" = only that end
    for r in results.values():
        req, rows = r.get("request", {}), r.get("rows") or []
        dims = [d for d in req.get("dimensions") or [] if rows and d in rows[0]]
        if len(rows) < 2 or len(dims) != 1 or dims[0] == "period":
            continue
        end = None
        if any("Only the first" in n for n in r.get("notes", [])):
            ob = (req.get("order_by") or "").split()
            if not ob:
                continue
            end = "min" if len(ob) > 1 and ob[1].lower() == "asc" else "max"
        skip = NON_PEER if dims[0] == "state" else {"Unknown"}
        for m in _metrics_in(req, rows):
            if end and (req.get("order_by") or "").split()[0] != m:
                continue
            vals = [(row[dims[0]], row[m]) for row in rows
                    if isinstance(row.get(m), (int, float)) and str(row[dims[0]]) not in skip]
            for ent, v in vals:
                index.append((v, ent, m, vals, end))
    if not index:
        return []
    texts = [(answer or {}).get("headline", "") or ""] + list((answer or {}).get("insights") or [])
    problems = []
    for text in texts:
        for sm in _SUPER.finditer(text):
            if re.search(r"next[- ]?$", text[max(0, sm.start() - 6): sm.start()], re.I):
                continue                                 # "next-highest" lists, not a superlative claim
            if re.match(r"-(ranked|rated|ranking)", text[sm.end(): sm.end() + 10], re.I):
                continue                                 # "the three highest-ranked" describes a group
            nm = hits = None
            for cand in _claim_number(text, sm):      # nearest number that is one of the result values
                try:
                    n = float(cand.group().replace(",", "").rstrip("."))
                except ValueError:
                    continue
                decimals = len(cand.group().rstrip(".").split(".")[1]) if "." in cand.group().rstrip(".") else 0
                hits = [h for h in index if abs(round(h[0], decimals) - n) < 1e-9]
                if hits:
                    nm = cand
                    break
            if not nm:
                continue
            metrics_hit = {h[2] for h in hits}
            if len(metrics_hit) != 1:
                continue                                     # same number in two metrics: cannot tell which
            m = hits[0][2]
            d = _direction(sm.group(), m)
            if d is None:
                continue
            how, rank = d
            # Judge the claim against each result that contains this number; it stands if it holds in any
            # (a peer-group result and a broader one can both contain the same value).
            verdicts = []
            for vals, end in {(id(h[3]), h[4]): (h[3], h[4]) for h in hits}.values():
                if end and end != how:
                    verdicts.append(("cutoff", None))
                    continue
                ordered = sorted(vals, key=lambda t: t[1], reverse=(how == "max"))
                if len(ordered) < rank:
                    verdicts.append(("ok", None))
                    continue
                true_ent, true_v = ordered[rank - 1]
                ok = true_ent in {h[1] for h in hits if h[3] is vals}
                if not ok:                                    # a list such as "Hawaii (0.26%), Maine (0.42%) ... have
                    sent = _sentence(text, sm)               # the lowest rates": the true value is in the sentence
                    ok = any(abs(round(true_v, dd) - nn) < 1e-9 for nn, dd in _sentence_numbers(sent))
                verdicts.append(("ok", None) if ok else ("wrong", (true_ent, true_v)))
            if any(v == "ok" for v, _ in verdicts):
                continue
            if any(v == "wrong" for v, _ in verdicts):
                true_ent, true_v = next(x for v, x in verdicts if v == "wrong")
                problems.append(f'You wrote "{sm.group()} ... {nm.group()}" for {m}, but the '
                                f'{"second " if rank == 2 else ""}{"highest" if how == "max" else "lowest"} {m} is '
                                f"{true_v:.2f} ({true_ent}). Fix this claim.")
            else:
                problems.append(f'You wrote "{sm.group()} ... {nm.group()}" for {m}, but that result was sorted '
                                f'the other way and cut off, so its {"low" if how == "min" else "high"} end is '
                                f'missing. Run query_metrics with order_by "{m} {"asc" if how == "min" else "desc"}" '
                                f"and a limit, or drop the claim.")
    return problems


_PCT = re.compile(r"(?<![\w.])[-\u2212]?(\d[\d,]*\.?\d*)\s?%")


_RANGE = re.compile(r"(?:between|range[sd]?\s+(?:from|between))\s+(-?\d[\d.]*)%\s+(?:and|to)\s+(-?\d[\d.]*)%", re.I)


def check_ranges(answer: dict, results: dict) -> list[str]:
    """'between 0.67% and 1.85%' must really bound every value of that metric for the groups the sentence is
    about (all groups, or the one it names)."""
    probs = []
    texts = [(answer or {}).get("headline", "") or ""] + list((answer or {}).get("insights") or [])
    for text in texts:
        for sent in _SENT.split(text):
            for m in _RANGE.finditer(sent):
                lo, hi = sorted((float(m.group(1)), float(m.group(2))))
                for r in results.values():
                    rows = r.get("rows") or []
                    dims = [d for d in (r.get("request", {}).get("dimensions") or []) if d != "period"]
                    for met in _metrics_in(r.get("request", {}), rows):
                        vals = [(row.get(dims[0]) if dims else None, row[met]) for row in rows
                                if isinstance(row.get(met), (int, float))]
                        if not any(abs(v - lo) < 0.006 for _, v in vals) or not any(abs(v - hi) < 0.006 for _, v in vals):
                            continue                      # not this metric
                        named = {g for g, _ in vals if g and str(g).split()[0].lower() in sent.lower()}
                        scope = [v for g, v in vals if len(named) != 1 or g in named]
                        out = [v for v in scope if v < lo - 0.006 or v > hi + 0.006]
                        if out:
                            probs.append(f'"{m.group(0)}" for {met} leaves out {min(out) if min(out) < lo else max(out):.2f}%. '
                                         f"The real range is {min(scope):.2f}% to {max(scope):.2f}%. Fix the range or say "
                                         "which values it excludes and why.")
    return probs


def check_percentages(answer: dict, results: dict, calc_values: list[float]) -> list[str]:
    """Every 'X%' in the headline and insights must trace to a governed percent value, a growth or
    change figure the layer returned, a concentration figure, or a calculate result."""
    known = list(calc_values)
    for r in results.values():
        units = r.get("units", {})
        for row in r.get("rows") or []:
            pct_row = row.get("unit") == "pct" or "change_pct" in row
            for k, v in row.items():
                if isinstance(v, (int, float)) and (units.get(k) == "percent" or pct_row or k.startswith("change_p")):
                    known.append(v)
        for k, v in list((r.get("concentration") or {}).items()) + list((r.get("benchmark") or {}).items()):
            if isinstance(v, (int, float)):
                known.append(v)
    texts = [(answer or {}).get("headline", "")] + list((answer or {}).get("insights") or [])
    problems = []
    for text in texts:
        for m in _PCT.finditer(text):
            raw = m.group(1).replace(",", "").rstrip(".")
            try:
                n = float(raw)
            except ValueError:
                continue
            dec = len(raw.split(".")[1]) if "." in raw else 0
            if not any(abs(round(abs(v), dec) - n) < 1e-9 for v in known):
                problems.append(f'"{m.group(0).strip()}" does not match any number from query_metrics or calculate. '
                                f"Compute it with calculate (e.g. a share = part / total * 100) or remove it.")
    return problems


def build_spec() -> dict:
    """System prompt and tools: analyst rules + the same governed glossary as Setup C."""
    c = prompts.build("C")
    glossary = prompts.render_glossary()
    for item in (yaml.safe_load(Path(prompts.__file__).with_name("semantic_model.yaml").read_text())
                 .get("extensions", {}).get("retires_out_of_scope", [])):
        glossary = glossary.replace(f"- {item}\n", "").replace(f"- {item}", "")
    system = f"{ANALYST_RULES}\n\nThe data covers 2024Q3 through 2026Q2.\n\n{glossary}"
    tools = [copy.deepcopy(t) for t in c["tools"] if t["name"] in ("query_metrics", "list_values")]
    props = tools[0]["input_schema"]["properties"]      # the app's layer does more than the frozen Setup C
    props["compare_to"]["description"] = (
        "Optional. 'prior_quarter', 'prior_year', or a 'YYYYQn' period. Returns both values and the change "
        "(percent change, or percentage-point change for ratios). With no dimensions: any metrics. With ONE "
        "dimension and ONE metric: the change for every group, sorted by change (gainers and losers), with "
        "results pro forma for mergers.")
    props["limit"]["description"] = "Optional. Maximum rows (up to 60)."
    return {"system": system, "tools": tools + [CALC_TOOL, PRESENT_TOOL]}


def history_messages(history: list[dict]) -> list[dict]:
    """Earlier turns as compact text, so follow-up questions have context."""
    msgs = []
    for turn in history[-4:]:
        msgs.append({"role": "user", "content": turn["question"]})
        a = turn.get("answer") or {}
        summary = a.get("headline", "") + ("\n" + "\n".join(a.get("insights", [])) if a.get("insights") else "")
        queries = "; ".join(json.dumps(r["request"]) for r in turn.get("results", {}).values())
        msgs.append({"role": "assistant", "content": f"{summary}\n(Queries used: {queries})" if queries else summary or "(no answer)"})
    return msgs


def run(client, model: str, layer: SemanticLayer, question: str, history: list[dict] | None = None,
        on_step=None, max_tokens: int = 4096) -> dict:
    """Answer one question. Returns {answer, results, steps, usage, latency_s, error}."""
    spec = build_spec()
    system = [{"type": "text", "text": spec["system"], "cache_control": {"type": "ephemeral"}}]
    tools = [dict(t) for t in spec["tools"]]
    tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
    messages = history_messages(history or []) + [{"role": "user", "content": question}]
    results, steps, answer, error = {}, [], None, None
    chart_retries = 0
    calc_values: list[float] = []
    log = []
    user_step = on_step

    def on_step(msg):
        log.append(msg)
        if user_step:
            user_step(msg)
    usage = {"input": 0, "output": 0, "cache_read": 0}
    t0 = time.time()

    for _ in range(MAX_TURNS):
        try:
            resp = client.messages.create(model=model, max_tokens=max_tokens, system=system, tools=tools, messages=messages)
        except Exception as e:  # noqa: BLE001
            error = f"{type(e).__name__}: {str(e)[:200]}"
            break
        usage["input"] += resp.usage.input_tokens
        usage["output"] += resp.usage.output_tokens
        usage["cache_read"] += getattr(resp.usage, "cache_read_input_tokens", 0) or 0
        messages.append({"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in resp.content]})
        uses = [b for b in resp.content if b.type == "tool_use"]
        if not uses:
            messages.append({"role": "user", "content": "Call present_answer now."})
            continue
        out = []
        for tu in uses:
            if tu.name == "present_answer":
                probs = (chart_problems(tu.input, results) + check_claims(tu.input, results)
                         + check_percentages(tu.input, results, calc_values) + answer_problems(tu.input, results)
                         + check_ranges(tu.input, results)
                         if tu.input.get("answer_type") == "answer" else [])
                if probs and chart_retries < 2:
                    chart_retries += 1
                    out.append({"type": "tool_result", "tool_use_id": tu.id, "is_error": True,
                                "content": "Not shown yet. Fix these and call present_answer again: " + " ".join(probs)})
                    if on_step:
                        on_step("Correcting: " + "; ".join(p.split(". ")[0][:90] for p in probs))
                    continue
                answer = tu.input
                out.append({"type": "tool_result", "tool_use_id": tu.id, "content": "Shown to the user."})
                continue
            try:
                if tu.name == "query_metrics":
                    res = layer.query_metrics(**tu.input)
                    rid = f"q{len(results) + 1}"
                    results[rid] = {"request": tu.input, **res}
                    shown = {"result_id": rid, **{k: v for k, v in res.items() if k != "sql"}}
                    ranks = rankings(res, tu.input)
                    if ranks:
                        shown["ranking"] = ranks
                    try:
                        bench = peer_benchmark(layer, tu.input, res)
                    except Exception:  # noqa: BLE001
                        bench = None
                    if bench:
                        results[rid]["benchmark"] = bench
                        shown["peer_aggregate"] = {"value": bench, "what": "the whole queried group, same filters"}
                    try:
                        conc = concentration(layer, res, tu.input, ranks)
                    except Exception:  # noqa: BLE001 - a failed check never blocks the answer
                        conc = None
                    if conc:
                        dreq, dres, block = conc
                        did = f"q{len(results) + 1}"
                        results[did] = {"request": dreq, **dres, "auto": True}
                        results[rid]["concentration"] = block
                        shown["concentration"] = {**block, "detail_result_id": did}
                        if on_step:
                            on_step(f"Checked concentration: {block['driver_bank']} drives {block['group']}")
                    content, is_err = json.dumps(shown, default=str)[:MAX_RESULT_CHARS], False
                    if on_step:
                        on_step(f"Queried {', '.join(tu.input.get('metrics', []))}"
                                + (f" by {', '.join(tu.input['dimensions'])}" if tu.input.get("dimensions") else "")
                                + f" for {tu.input.get('period')}")
                elif tu.name == "calculate":
                    try:
                        val = calculate(tu.input.get("expression", ""))
                        calc_values.append(val)
                        content, is_err = json.dumps({"expression": tu.input.get("expression"), "result": val}), False
                    except (ValueError, SyntaxError, ZeroDivisionError, TypeError) as e:
                        content, is_err = f"ERROR: {e}", True
                    if on_step:
                        on_step(f"Calculated {tu.input.get('expression')}")
                elif tu.name == "list_values":
                    content, is_err = json.dumps(layer.list_values(**tu.input), default=str)[:MAX_RESULT_CHARS], False
                    if on_step:
                        on_step(f"Looked up {tu.input.get('dimension')} values")
                else:
                    content, is_err = f"ERROR: unknown tool {tu.name}", True
            except Exception as e:  # noqa: BLE001 - every tool failure goes back to the model, never crashes the app
                if type(e).__name__ == "QueryError":          # by name: survives a code reload mid-session
                    content = f"ERROR: {e}"
                    if on_step:
                        on_step("A request was outside the governed metrics; adjusting")
                elif isinstance(e, TypeError):
                    content = f"ERROR: bad arguments for {tu.name}: {e}"
                else:
                    content = f"ERROR: the request failed ({type(e).__name__}). Try a simpler request."
                is_err = True
            steps.append({"tool": tu.name, "input": tu.input, "error": is_err})
            out.append({"type": "tool_result", "tool_use_id": tu.id, "content": content, "is_error": is_err})
        messages.append({"role": "user", "content": out})
        if answer is not None:
            break

    if answer is None and error is None:
        error = "The model did not finish an answer."
    return {"answer": answer, "results": results, "steps": steps, "usage": usage, "log": log, "calc": calc_values,
            "latency_s": round(time.time() - t0, 1), "error": error}


def auto_chart(results: dict) -> dict | None:
    """A sensible chart when the model gave none: the last result with 1-2 dimensions and a metric."""
    candidates = []
    for order, (rid, r) in enumerate(results.items()):
        rows, req = r.get("rows") or [], r.get("request", {})
        dims = [d for d in req.get("dimensions") or [] if rows and d in rows[0]]
        metrics = [m for m in req.get("metrics") or [] if rows and m in rows[0]]
        if dims and metrics and 2 <= len(rows) <= (60 if "period" in dims else 25):
            candidates.append((len(metrics), order, rid, r, dims, metrics))
    if not candidates:
        return None
    if len(candidates) > 1:           # several results: prefer the latest trend, else the latest result
        trends = [c for c in candidates if "period" in c[4]]
        candidates = [(trends or candidates)[-1]]
    for _, _, rid, r, dims, metrics in candidates:
        ratios = [m for m in metrics if r.get("units", {}).get(m) == "percent"]
        metrics = ratios + [m for m in metrics if m not in ratios]   # a scorecard charts its ratio first
        if "period" in dims:
            other = [d for d in dims if d != "period"]
            per = str(r.get("request", {}).get("period", "")).replace(":", " to ")
            title = metrics[0].replace("_", " ").capitalize() + (f" by {other[0].replace('_', ' ')}" if other else "")
            return {"result_id": rid, "kind": "line", "x": "period", "y": metrics[0],
                    "color": other[0] if other else None, "title": f"{title}, {per}" if per else title}
        return {"result_id": rid, "kind": "bar", "x": dims[0], "y": metrics[0],
                "color": dims[1] if len(dims) > 1 else None,
                "title": f"{metrics[0].replace('_', ' ').capitalize()} by {dims[0].replace('_', ' ')}"}
    return None


def scorecard_result(chart_specs: list[dict], results: dict) -> str | None:
    """Which result the scorecard table is built from: the charted result first, otherwise the latest query
    (later queries refine earlier ones). Never an earlier, superseded result. Used by the page and the
    benchmark, so what is graded is what is shown."""
    import charts
    order = [c["result_id"] for c in chart_specs] + [rid for rid in reversed(list(results)) if not results[rid].get("auto")]
    if chart_specs:                  # a scorecard about other metrics than the chart would be off-topic
        charted = {c["y"] for c in chart_specs} | set(results[chart_specs[0]["result_id"]]["request"].get("metrics") or [])
        order = [rid for rid in order if set(results[rid]["request"].get("metrics") or []) & charted]
    for rid in dict.fromkeys(order):
        r = results[rid]
        if charts.scorecard(r, r["request"], LOWER_IS_BETTER, HIGHER_IS_BETTER) is not None:
            return rid
    return None


def display_problems(answer: dict, results: dict) -> list[str]:
    """Checks on what the page will show besides the text: the chart and the scorecard must cover the same
    group as the answer, and never rank non-peers."""
    import charts
    probs, specs = [], valid_charts(answer, results)
    scoped = [x["request"].get("filters") for x in results.values()
              if x.get("request", {}).get("filters") and not x.get("auto")]
    for c in specs:
        r = results[c["result_id"]]
        cdims = set(r.get("request", {}).get("dimensions") or [])
        if scoped and not r.get("request", {}).get("filters") and not r.get("auto") \
                and not r.get("request", {}).get("derived_from") and "national" not in str(c.get("title", "")).lower() \
                and not any(cdims & set(f) for f in scoped):
            probs.append(f"chart {c['result_id']} is unfiltered while the answer is about a filtered group")
        try:
            ch, _ = charts.build(c, r)
            data = ch.to_dict().get("datasets", {})
            shown = {str(v) for rows in data.values() for row in rows for k, v in row.items() if k == c["x"]}
            if shown & {"Unknown"}:
                probs.append(f"chart {c['result_id']} shows Unknown")
        except Exception as e:  # noqa: BLE001
            probs.append(f"chart {c['result_id']} fails to draw: {type(e).__name__}")
    rid = scorecard_result(specs, results)
    if rid and specs:
        c = specs[0]
        sc, ch_r = results[rid], results[c["result_id"]]
        d = (sc["request"].get("dimensions") or [None])[0]
        if d and d == c.get("x") and not ch_r.get("auto"):
            skip = {"Unknown"}
            a = {str(x[d]) for x in sc["rows"]} - skip
            b = {str(x[d]) for x in ch_r["rows"]} - skip
            if c.get("only"):
                b = b & {str(v) for v in c["only"]}
            if not a <= b and not b <= a:
                probs.append(f"scorecard ({rid}) and chart ({c['result_id']}) show different groups")
    return probs


def answer_problems(answer: dict, results: dict) -> list[str]:
    """Answer-level checks: a real headline, and a concentration finding is never dropped."""
    probs = []
    head = str((answer or {}).get("headline") or "").strip()
    if len(head) < 25 or re.search(r"placeholder|lorem|\bTBD\b|\.\.\.$", head, re.I):
        probs.append("The headline is missing or a placeholder. Write the one-sentence answer with its key number.")
    text = " ".join([head] + list((answer or {}).get("insights") or [])).lower()
    for r in results.values():
        blk = r.get("concentration")
        if blk:
            first = str(blk["driver_bank"]).split()[0].lower()
            if first not in text:
                probs.append(f"A concentration check found that {blk['driver_bank']} drives {blk['group']} "
                             f"({blk['metric']} {blk['group_rate']}, {blk.get('group_rate_without_driver')} without "
                             "it). Say so in an insight.")
    flagged = sorted({str(row.get(d)) for r in results.values() for row in (r.get("rows") or [])
                      if row.get("merger_quarter") for d in ("organization", "holding_company", "bank_name")
                      if row.get(d)} or ({"one or more organizations"} if any(
                          row.get("merger_quarter") for r in results.values() for row in (r.get("rows") or [])) else set()))
    if flagged and "merger" not in text:
        probs.append(f"Some quarterly income figures fall in a merger quarter ({', '.join(flagged)[:200]}). Say in "
                     "an insight that these quarters are distorted by the merger and can differ materially from "
                     "reported results.")
    return probs


def chart_problems(answer: dict, results: dict) -> list[str]:
    """Specific, fixable problems with the model's chart specs (checked before the answer is accepted)."""
    if (answer or {}).get("answer_type") == "answer" and change_chart(results):
        return []                                  # the gainers-and-losers chart is drawn by code
    probs = []
    for i, c in enumerate((answer or {}).get("charts") or [], 1):
        r = results.get(c.get("result_id"))
        if not r:
            probs.append(f"Chart {i}: result_id '{c.get('result_id')}' does not exist; use one of {list(results)}.")
            continue
        cols = list(r["rows"][0]) if r["rows"] else []
        for field in ("x", "y", "color"):
            v = c.get(field)
            if v and v not in cols:
                probs.append(f"Chart {i}: {field} '{v}' is not a column in {c['result_id']}; its columns are {cols}.")
        if c.get("only") and c.get("x") in cols:
            present = {str(row[c["x"]]) for row in r["rows"]}
            missing = [v for v in c["only"] if str(v) not in present]
            if missing:
                probs.append(f"Chart {i}: 'only' values {missing} are not in column {c['x']} of {c['result_id']}.")
        scoped = {rid: x["request"].get("filters") for rid, x in results.items()
                  if x.get("request", {}).get("filters") and not x.get("auto")}
        cdims = set(r.get("request", {}).get("dimensions") or [])
        if scoped and not r.get("request", {}).get("filters") and not r.get("auto") \
                and not r.get("request", {}).get("derived_from") \
                and not any(cdims & set(f) for f in scoped.values()):   # same dimension = the group beside its peers
            rid0, f0 = next(iter(scoped.items()))
            probs.append(f"Chart {i}: {c['result_id']} has no filters, but the answer is about a filtered group "
                         f"({rid0}: {f0}). Chart a result with the same filters, or say in the chart title that it "
                         "is national context.")
        if c.get("kind") == "bar" and not c.get("color") and len(r["rows"]) > 20 and not c.get("only"):
            probs.append(f"Chart {i}: {c['result_id']} has {len(r['rows'])} rows, too many for a bar chart. "
                         "Run a narrower query_metrics filtered to the items you discuss, and chart that result.")
    chartable = [rid for rid, r in results.items()
                 if r.get("request", {}).get("dimensions") and 2 <= len(r.get("rows") or []) <= 25]
    if (answer or {}).get("answer_type") == "answer" and not (answer or {}).get("charts") and chartable:
        probs.append(f"Add at least one chart. Results that can be charted: {chartable}.")
    return probs


def change_chart(results: dict) -> dict | None:
    """When the answer rests on a change-by-group result (compare_to with a dimension), the chart is always
    that result as gainers and losers. Decided in code, so a trend of the wrong groups is never drawn."""
    for rid in reversed(list(results)):
        r = results[rid]
        req, rows = r.get("request", {}), r.get("rows") or []
        if req.get("compare_to") and req.get("dimensions") and len(rows) >= 2:
            y = "change_pct" if "change_pct" in rows[0] else "change_pp" if "change_pp" in rows[0] else None
            if not y:
                continue
            m, d = req["metrics"][0], req["dimensions"][0]
            prior = next((k[len(m) + 1:] for k in rows[0] if k.startswith(m + "_")), "prior period")
            return {"result_id": rid, "kind": "bar", "x": d, "y": y,
                    "title": f"{m.replace('_', ' ').capitalize()}: change from {prior} to {req['period']}"
                             + (" (percentage points)" if y == "change_pp" else " (%)")}
    return None


def valid_charts(answer: dict, results: dict) -> list[dict]:
    """Chart specs whose result, x, y and color exist in the data; a fallback if none qualify."""
    if (answer or {}).get("answer_type") == "answer":
        forced = change_chart(results)
        if forced:
            return [forced]
    good = []
    for c in (answer or {}).get("charts") or []:
        r = results.get(c.get("result_id"))
        if not r or not r["rows"]:
            continue
        cols = set(r["rows"][0])
        if c.get("x") in cols and c.get("y") in cols and (not c.get("color") or c["color"] in cols):
            good.append(c)
    if not good and (answer or {}).get("answer_type") == "answer":
        fallback = auto_chart(results)
        if fallback:
            good.append(fallback)
    return [chart_rules(c, results) for c in good]


MAX_LINES = 8


def chart_rules(c: dict, results: dict) -> dict:
    """Chart-type rules applied in code to every chart, whatever the model asked for. They depend only on the
    shape of the data, so they hold for any metric or dimension:
      1. Change between two periods for many groups -> a diverging bar of the change (gains right, losses
         left; top 5 each way when there are more than 10). A line through two points shows little.
      2. A trend with more than 8 lines -> the 8 largest by their latest value.
      3. Lines on very different scales (largest 20x the smallest) -> each indexed to 100 at the first period,
         so every line's movement is visible.
    Rankings and comparisons of one period are sorted horizontal bars; scorecards cover several metrics."""
    r = results.get(c.get("result_id")) or {}
    rows, x, y, color = r.get("rows") or [], c.get("x"), c.get("y"), c.get("color")
    if c.get("kind") != "line" or x != "period" or not color or not rows:
        return c
    periods = sorted({row["period"] for row in rows})
    unit = (r.get("units") or {}).get(y, "")
    series = {}
    for row in rows:
        if isinstance(row.get(y), (int, float)) and str(row.get(color)) != "Unknown":
            series.setdefault(row[color], {})[row["period"]] = row[y]
    if len(periods) == 2 and len(series) >= 3:                                      # rule 1
        a, b = periods
        out = []
        for g, v in series.items():
            if a in v and b in v:
                if unit == "percent":
                    out.append({color: g, "change_pp": round(v[b] - v[a], 4)})
                elif v[a]:
                    out.append({color: g, "change_pct": round((v[b] / v[a] - 1) * 100, 4)})
        ycol = "change_pp" if unit == "percent" else "change_pct"
        did = f"{c['result_id']}_change"
        results[did] = {"request": {"metrics": [ycol], "dimensions": [color], "period": f"{a} to {b}",
                                    "derived_from": c["result_id"]},
                        "rows": sorted(out, key=lambda t: -t[ycol]), "units": {ycol: "percent"}, "auto": True,
                        "notes": [f"Change computed in code from {c['result_id']}."], "sql": ""}
        name = y.replace("_", " ").capitalize()
        return {"result_id": did, "kind": "bar", "x": color, "y": ycol,
                "title": f"{name}: change from {a} to {b}" + (" (percentage points)" if unit == "percent" else " (%)")}
    c = dict(c)
    if len(series) > MAX_LINES:                                                     # rule 2
        last = {g: v[max(v)] for g, v in series.items() if v}
        c["only_series"] = sorted(last, key=last.get, reverse=True)[:MAX_LINES]
        series = {g: series[g] for g in c["only_series"]}
    latest = [v[max(v)] for v in series.values() if v and v[max(v)] and v[max(v)] > 0]
    if len(latest) >= 2 and max(latest) / min(latest) >= 20 and unit != "percent":   # rule 3
        first = periods[0]
        out = [{"period": row["period"], color: row[color],
                "index": round(row[y] / series[row[color]][first] * 100, 2)}
               for row in rows if row.get(color) in series and series[row[color]].get(first)
               and isinstance(row.get(y), (int, float))]
        did = f"{c['result_id']}_index"
        results[did] = {"request": {"metrics": ["index"], "dimensions": ["period", color], "period": r["request"].get("period"),
                                    "derived_from": c["result_id"]},
                        "rows": out, "units": {"index": "index"}, "auto": True,
                        "notes": [f"Each line indexed to 100 at {first}, computed in code from {c['result_id']}."],
                        "sql": ""}
        c.update(result_id=did, y="index", title=f"{c.get('title') or y} (indexed, {first} = 100)")
    return c
