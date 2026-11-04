"""Natural-language analytics on the governed semantic layer.

The AI model plans governed metric queries (query_metrics / list_values), then calls
present_answer with a headline, insights and chart specs that point at query results by id.
Every number comes from the semantic layer; charts are drawn by the app from those results.
"""
from __future__ import annotations

import ast
import json
import operator
import time

import prompts
from semantic_layer import QueryError, SemanticLayer

MAX_TURNS = 10
MAX_RESULT_CHARS = 8000

ANALYST_RULES = """You are a bank data analyst answering questions about FDIC-insured banks for business users.

How to work:
- Get every number from query_metrics. Never invent data.
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
  filters {"holding_company": ["NAME 1", "NAME 2"]}, and chart that result.
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


def build_spec() -> dict:
    """System prompt and tools: analyst rules + the same governed glossary as Setup C."""
    c = prompts.build("C")
    system = f"{ANALYST_RULES}\n\nThe data covers 2024Q3 through 2026Q2.\n\n{prompts.render_glossary()}"
    tools = [t for t in c["tools"] if t["name"] in ("query_metrics", "list_values")] + [CALC_TOOL, PRESENT_TOOL]
    return {"system": system, "tools": tools}


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
                probs = chart_problems(tu.input, results) if tu.input.get("answer_type") == "answer" else []
                if probs and chart_retries < 2:
                    chart_retries += 1
                    out.append({"type": "tool_result", "tool_use_id": tu.id, "is_error": True,
                                "content": "Not shown yet. Fix the charts and call present_answer again: " + " ".join(probs)})
                    if on_step:
                        on_step("Fixing the chart")
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
                    content, is_err = json.dumps(shown, default=str)[:MAX_RESULT_CHARS], False
                    if on_step:
                        on_step(f"Queried {', '.join(tu.input.get('metrics', []))}"
                                + (f" by {', '.join(tu.input['dimensions'])}" if tu.input.get("dimensions") else "")
                                + f" for {tu.input.get('period')}")
                elif tu.name == "calculate":
                    try:
                        content, is_err = json.dumps({"expression": tu.input.get("expression"),
                                                      "result": calculate(tu.input.get("expression", ""))}), False
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
            except QueryError as e:
                content, is_err = f"ERROR: {e}", True
                if on_step:
                    on_step("A request was outside the governed metrics; adjusting")
            except TypeError as e:
                content, is_err = f"ERROR: bad arguments: {e}", True
            steps.append({"tool": tu.name, "input": tu.input, "error": is_err})
            out.append({"type": "tool_result", "tool_use_id": tu.id, "content": content, "is_error": is_err})
        messages.append({"role": "user", "content": out})
        if answer is not None:
            break

    if answer is None and error is None:
        error = "The model did not finish an answer."
    return {"answer": answer, "results": results, "steps": steps, "usage": usage, "log": log,
            "latency_s": round(time.time() - t0, 1), "error": error}


def auto_chart(results: dict) -> dict | None:
    """A sensible chart when the model gave none: the last result with 1-2 dimensions and a metric."""
    candidates = []
    for order, (rid, r) in enumerate(results.items()):
        rows, req = r.get("rows") or [], r.get("request", {})
        dims = [d for d in req.get("dimensions") or [] if rows and d in rows[0]]
        metrics = [m for m in req.get("metrics") or [] if rows and m in rows[0]]
        if dims and metrics and 2 <= len(rows) <= 25:
            candidates.append((len(metrics), order, rid, r, dims, metrics))
    if len(candidates) != 1:          # never guess between several results
        return None
    for _, _, rid, r, dims, metrics in candidates:
        ratios = [m for m in metrics if r.get("units", {}).get(m) == "percent"]
        metrics = ratios + [m for m in metrics if m not in ratios]   # a scorecard charts its ratio first
        if "period" in dims:
            other = [d for d in dims if d != "period"]
            return {"result_id": rid, "kind": "line", "x": "period", "y": metrics[0],
                    "color": other[0] if other else None, "title": metrics[0].replace("_", " ").capitalize()}
        return {"result_id": rid, "kind": "bar", "x": dims[0], "y": metrics[0],
                "color": dims[1] if len(dims) > 1 else None,
                "title": f"{metrics[0].replace('_', ' ').capitalize()} by {dims[0].replace('_', ' ')}"}
    return None


def chart_problems(answer: dict, results: dict) -> list[str]:
    """Specific, fixable problems with the model's chart specs (checked before the answer is accepted)."""
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
        if c.get("kind") == "bar" and not c.get("color") and len(r["rows"]) > 20:
            probs.append(f"Chart {i}: {c['result_id']} has {len(r['rows'])} rows, too many for a bar chart. "
                         "Run a narrower query_metrics filtered to the items you discuss, and chart that result.")
    chartable = [rid for rid, r in results.items()
                 if r.get("request", {}).get("dimensions") and 2 <= len(r.get("rows") or []) <= 25]
    if (answer or {}).get("answer_type") == "answer" and not (answer or {}).get("charts") and chartable:
        probs.append(f"Add at least one chart. Results that can be charted: {chartable}.")
    return probs


def valid_charts(answer: dict, results: dict) -> list[dict]:
    """Chart specs whose result, x, y and color exist in the data; a fallback if none qualify."""
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
    return good
