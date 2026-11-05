"""Executive questions: the kind a bank executive asks when benchmarking with FDIC data.

Each question carries its expected answer, computed from the governed semantic layer (not typed
by hand), and the checks run_exec.py applies. No AI judge.

Check fields
  expect        "answer" or "decline"
  headline      list of groups; each group is a list of alternatives, one of which must appear
                in the headline (case-insensitive)
  numbers       expected values that must appear somewhere in the headline or insights
                (matched at the precision written, e.g. 1.23 or 1.2)
  not_headline  strings that must not appear in the headline
  not_text      strings that must not appear anywhere in headline or insights
  chart         True if a valid chart is required
Every answer is also re-checked with the app's own claim checker and percent checker; any
problem they find fails the run.
"""
from __future__ import annotations

from semantic_layer import SemanticLayer

TERRITORIES = ["Guam", "Puerto Rico", "Virgin Islands", "Micronesia"]
PEERS = ["M&T BANK CORP", "CITIZENS FINANCIAL GROUP INC (RI)", "KEYCORP", "HUNTINGTON BANCSHARES INC"]


def _q(L, **kw):
    return L.query_metrics(**kw)["rows"]


def _by(rows, dim, metric):
    return {r[dim]: r[metric] for r in rows}


def build(L: SemanticLayer | None = None) -> list[dict]:
    L = L or SemanticLayer()
    Q = []

    # E01 peer scorecard (generic regional peers)
    rows = _q(L, metrics=["return_on_assets", "efficiency_ratio", "noncurrent_loan_rate"], period="2026Q2",
              dimensions=["holding_company"], filters={"holding_company": PEERS})
    roa, eff = _by(rows, "holding_company", "return_on_assets"), _by(rows, "holding_company", "efficiency_ratio")
    top_roa = max(roa, key=roa.get)
    Q.append(dict(id="E01", topic="peer scorecard",
                  question="Compare ROA, efficiency ratio and noncurrent loan rate for M&T, Citizens Financial Group, "
                           "KeyCorp and Huntington in 2026Q2. Who leads on profitability?",
                  expect="answer", headline=[[top_roa.split()[0]]],
                  numbers=[roa[top_roa]], chart=True,
                  key={"roa": roa, "efficiency_ratio": eff}))

    # E02 states, highest noncurrent, concentration trap
    rows = _q(L, metrics=["noncurrent_loan_rate"], period="2026Q2", dimensions=["state"],
              filters={"geography_type": "State or DC"}, order_by="noncurrent_loan_rate desc", limit=3)
    Q.append(dict(id="E02", topic="state ranking + concentration",
                  question="Which states have the highest noncurrent loan rates in 2026Q2?",
                  expect="answer", headline=[["Oklahoma", "OK"]],
                  numbers=[rows[0]["noncurrent_loan_rate"], 1.12], not_text=TERRITORIES,
                  must_text=[["MidFirst"]], chart=True, key=rows))

    # E03 states, lowest (row-limit trap)
    rows = _q(L, metrics=["noncurrent_loan_rate"], period="2026Q2", dimensions=["state"],
              filters={"geography_type": "State or DC"}, order_by="noncurrent_loan_rate asc", limit=3)
    Q.append(dict(id="E03", topic="state ranking, low end",
                  question="Which states have the lowest noncurrent loan rates in 2026Q2?",
                  expect="answer", headline=[["Hawaii", "HI"]], numbers=[rows[0]["noncurrent_loan_rate"]],
                  not_text=TERRITORIES, chart=True, key=rows))

    # E04 trend by organization size, direction
    rows = _q(L, metrics=["noncurrent_loan_rate"], period="2024Q3:2026Q2",
              dimensions=["period", "organization_size_class"])
    first = {r["organization_size_class"]: r["noncurrent_loan_rate"] for r in rows if r["period"] == "2024Q3"}
    last = {r["organization_size_class"]: r["noncurrent_loan_rate"] for r in rows if r["period"] == "2026Q2"}
    chg = {k: last[k] - first[k] for k in last}
    worst = max(chg, key=chg.get)
    Q.append(dict(id="E04", topic="trend by segment",
                  question="How has the noncurrent loan rate trended by organization size since 2024, and which group "
                           "has deteriorated the most?",
                  expect="answer", headline=[["$10B", "10B-$100B", "10 billion"]] if worst == "$10B-$100B" else [[worst]],
                  numbers=[last[worst], round(chg[worst], 2)], chart=True, key={"first": first, "last": last}))

    # E05 deposits by regulator, YoY
    tot = L.query_metrics(metrics=["total_deposits"], period="2026Q2", compare_to="prior_year")["rows"][0]
    a = _by(_q(L, metrics=["total_deposits"], period="2026Q2", dimensions=["regulator"]), "regulator", "total_deposits")
    b = _by(_q(L, metrics=["total_deposits"], period="2025Q2", dimensions=["regulator"]), "regulator", "total_deposits")
    occ = (a["OCC"] - b["OCC"]) / b["OCC"] * 100
    Q.append(dict(id="E05", topic="growth by segment",
                  question="Deposit growth by regulator, year over year",
                  expect="answer", headline=[["deposit"]], numbers=[tot["change_pct"], occ], chart=True,
                  key={"industry_pct": tot["change_pct"], "occ_pct": occ}))

    # E06 regions, two metrics, opposite directions
    def reg(p, m):
        return _by(_q(L, metrics=[m], period=p, dimensions=["region"]), "region", m)
    r0, r1 = reg("2025Q2", "return_on_assets"), reg("2026Q2", "return_on_assets")
    d = {k: r1[k] - r0[k] for k in r1 if k != "Unknown"}
    best = max(d, key=d.get)
    Q.append(dict(id="E06", topic="two-period, two-metric judgment",
                  question="Which FDIC regions improved ROA the most from 2025Q2 to 2026Q2, and did their efficiency "
                           "ratios improve too?",
                  expect="answer", headline=[[best]], numbers=[r1[best], round(d[best], 2)],
                  not_headline=["Unknown"], chart=True, key=d))

    # E07 community banks vs others, medians
    rows = _by(_q(L, metrics=["median_bank_roa"], period="2026Q2", dimensions=["fdic_community_bank"]),
               "fdic_community_bank", "median_bank_roa")
    Q.append(dict(id="E07", topic="peer medians, Unknown trap",
                  question="Median ROA for community banks vs. other banks, 2026Q2",
                  expect="answer", headline=[["community"]],
                  numbers=[rows["Community bank"], rows["Non-community bank"]], not_headline=["Unknown"],
                  key=rows))

    # E08 YoY ratio change for a size class
    r = L.query_metrics(metrics=["efficiency_ratio"], period="2026Q2", filters={"size_class": "$10B-$100B"},
                        compare_to="prior_year")["rows"][0]
    Q.append(dict(id="E08", topic="ratio change in points",
                  question="How did the efficiency ratio for $10B-$100B banks change from 2025Q2 to 2026Q2?",
                  expect="answer", headline=[["efficiency"]], numbers=[r["2026Q2"], abs(r["change_pp"])],
                  key=r))

    # E09 credit trend, income-based ratio period rule
    rows = _q(L, metrics=["net_charge_off_rate"], period="2024Q4:2026Q2", dimensions=["period"],
              filters={"size_class": "$100B and over"})
    Q.append(dict(id="E09", topic="credit trend",
                  question="Net charge-off rate trend for banks over $100B since 2024",
                  expect="answer", headline=[["charge"]], numbers=[rows[-1]["net_charge_off_rate"]], chart=True,
                  key=rows))

    # E10 top organizations
    rows = _q(L, metrics=["total_assets"], period="2026Q2", dimensions=["organization"],
              order_by="total_assets desc", limit=10)
    Q.append(dict(id="E10", topic="top-N ranking",
                  question="Top 10 banking organizations by assets",
                  expect="answer", headline=[[rows[0]["organization"].split()[0]]], chart=True, key=rows))

    # E11 industry trend
    rows = _q(L, metrics=["return_on_assets"], period="2024Q3:2026Q2", dimensions=["period"])
    Q.append(dict(id="E11", topic="industry trend",
                  question="How has industry ROA trended since 2024?",
                  expect="answer", headline=[["ROA", "return on assets"]], numbers=[rows[-1]["return_on_assets"]],
                  chart=True, key=rows))

    # E12 region with concentration
    rows = [r for r in _q(L, metrics=["noncurrent_loan_rate"], period="2026Q2", dimensions=["region"],
                          order_by="noncurrent_loan_rate desc") if r["region"] != "Unknown"]
    Q.append(dict(id="E12", topic="region ranking + concentration",
                  question="Noncurrent loan rate by FDIC region, 2026Q2",
                  expect="answer", headline=[[rows[0]["region"]]], numbers=[rows[0]["noncurrent_loan_rate"]],
                  must_text=[["MidFirst"]], not_headline=["Unknown"], chart=True, key=rows))

    # E13-E15 out of scope: must decline, not estimate
    Q.append(dict(id="E13", topic="decline", question="What's the cost of deposits for $10B-$100B banks?",
                  expect="decline"))
    Q.append(dict(id="E14", topic="decline",
                  question="What share of loans is commercial real estate at banks under $10B?", expect="decline"))
    Q.append(dict(id="E15", topic="decline",
                  question="Which banks have the highest uninsured deposit share?", expect="decline"))
    return Q


if __name__ == "__main__":
    import json
    for q in build():
        print(q["id"], q["topic"], "|", q["question"])
        print("   headline:", q.get("headline"), "numbers:", [round(n, 2) for n in q.get("numbers", [])])
    _ = json
