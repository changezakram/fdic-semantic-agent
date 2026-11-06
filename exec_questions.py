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

from semantic_layer import EXT_SNAPSHOT, SemanticLayer

TERRITORIES = ["Guam", "Puerto Rico", "Virgin Islands", "Micronesia"]
PEERS = ["M&T BANK CORP", "CITIZENS FINANCIAL GROUP INC (RI)", "KEYCORP", "HUNTINGTON BANCSHARES INC"]


def _q(L, **kw):
    return L.query_metrics(**kw)["rows"]


def _by(rows, dim, metric):
    return {r[dim]: r[metric] for r in rows}


def _without_driver(L, dim, group, filters):
    """The group's noncurrent rate without its largest problem-loan bank, computed the way the app does."""
    import analyst
    req = {"metrics": ["noncurrent_loan_rate"], "period": "2026Q2", "dimensions": [dim], "filters": filters}
    res = L.query_metrics(**req)
    block = analyst.concentration(L, res, req, analyst.rankings(res, req))[2]
    assert block["group"] == f"{dim} = {group}"
    return block["group_rate_without_driver"]


def build(L: SemanticLayer | None = None) -> list[dict]:
    L = L or SemanticLayer(ext=EXT_SNAPSHOT, pro_forma=True)
    Q = []

    # E01 peer scorecard (generic regional peers)
    rows = _q(L, metrics=["return_on_assets_quarterly", "efficiency_ratio", "noncurrent_loan_rate"], period="2026Q2",
              dimensions=["holding_company"], filters={"holding_company": PEERS})
    roa, eff = _by(rows, "holding_company", "return_on_assets_quarterly"), _by(rows, "holding_company", "efficiency_ratio")
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
                  expect="answer", headline=[["deposit"]], numbers=[occ], chart=True,
                  key={"industry_pct": tot["change_pct"], "occ_pct": occ}))

    # E06 regions, two metrics, opposite directions
    def reg(p, m):
        return _by(_q(L, metrics=[m], period=p, dimensions=["region"]), "region", m)
    r0, r1 = reg("2025Q2", "return_on_assets_quarterly"), reg("2026Q2", "return_on_assets_quarterly")
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
    rows = _q(L, metrics=["return_on_assets_quarterly"], period="2024Q4:2026Q2", dimensions=["period"])
    Q.append(dict(id="E11", topic="industry trend",
                  question="How has industry ROA trended since 2024?",
                  expect="answer", headline=[["ROA", "return on assets"]], numbers=[rows[-1]["return_on_assets_quarterly"]],
                  chart=True, key=rows))

    # E12 region with concentration
    rows = [r for r in _q(L, metrics=["noncurrent_loan_rate"], period="2026Q2", dimensions=["region"],
                          order_by="noncurrent_loan_rate desc") if r["region"] != "Unknown"]
    Q.append(dict(id="E12", topic="region ranking + concentration",
                  question="Noncurrent loan rate by FDIC region, 2026Q2",
                  expect="answer", headline=[[rows[0]["region"]]], numbers=[rows[0]["noncurrent_loan_rate"]],
                  must_text=[["MidFirst"]], not_headline=["Unknown"], chart=True, key=rows))

    # E16 gainers and losers, peer group by place and size, change by group
    rows = L.query_metrics(metrics=["total_deposits"], period="2026Q2", dimensions=["organization"],
                           filters={"us_region": "New England", "organization_asset_band": ["$5B-$10B", "$10B-$50B"]},
                           compare_to="prior_quarter")["rows"]
    top, bottom = rows[0], rows[-1]
    Q.append(dict(id="E16", topic="gainers and losers",
                  question="Show the top 5 deposit gainers and the top 5 decliners among New England banking "
                           "organizations with $5B-$50B in assets, by percent change from 2026Q1 to 2026Q2.",
                  expect="answer", headline=[[top["organization"].split()[0]], [bottom["organization"].split()[0]]],
                  numbers=[top["change_pct"], bottom["change_pct"]], chart=True, key=rows))

    # E17 loan-to-deposit ratio, peer group by place and size (no "better" direction)
    band = {"us_region": "New England", "organization_asset_band": ["$5B-$10B", "$10B-$50B"]}
    rows = L.query_metrics(metrics=["loan_to_deposit_ratio"], period="2026Q2", dimensions=["organization"],
                           filters=band, order_by="loan_to_deposit_ratio desc")["rows"]
    Q.append(dict(id="E17", topic="peer ratio, no direction",
                  question="Loan-to-deposit ratio for New England banking organizations with $5B-$50B in assets, 2026Q2",
                  expect="answer", headline=[["loan-to-deposit"]], must_text=[[rows[0]["organization"].split()[0]]],
                  numbers=[rows[0]["loan_to_deposit_ratio"]], not_text=["best loan-to-deposit", "worst loan-to-deposit"],
                  chart=True, key=rows))

    # E18 loan gainers and losers
    rows = L.query_metrics(metrics=["gross_loans"], period="2026Q2", dimensions=["organization"], filters=band,
                           compare_to="prior_quarter")["rows"]
    Q.append(dict(id="E18", topic="gainers and losers (loans)",
                  question="Show the top 5 loan gainers and the top 5 decliners among New England banking organizations "
                           "with $5B-$50B in assets, by percent change from 2026Q1 to 2026Q2.",
                  expect="answer", headline=[[rows[0]["organization"].split()[0]]],
                  numbers=[rows[0]["change_pct"], rows[-1]["change_pct"]], chart=True, key=rows))

    # E19 top 10 by noncurrent rate, peer group
    rows = L.query_metrics(metrics=["noncurrent_loan_rate"], period="2026Q2", dimensions=["organization"],
                           filters=band, order_by="noncurrent_loan_rate desc", limit=10)["rows"]
    Q.append(dict(id="E19", topic="top 10, credit, peer group",
                  question="Top 10 New England banking organizations with $5B-$50B in assets by noncurrent loan rate, "
                           "highest first, 2026Q2",
                  expect="answer", headline=[[rows[0]["organization"].split()[0]]],
                  numbers=[rows[0]["noncurrent_loan_rate"]], chart=True, key=rows))

    # E20 New England states, concentration (Rhode Island = Citizens)
    rows = L.query_metrics(metrics=["noncurrent_loan_rate"], period="2026Q2", dimensions=["state"],
                           filters={"us_region": "New England"}, order_by="noncurrent_loan_rate desc")["rows"]
    Q.append(dict(id="E20", topic="states + concentration",
                  question="Noncurrent loan rate by New England state, 2026Q2",
                  expect="answer", headline=[["Rhode Island", "RI"]],
                  numbers=[rows[0]["noncurrent_loan_rate"], _without_driver(L, "state", "RI", {"us_region": "New England"})],
                  must_text=[["Citizens"]], chart=True, key=rows))

    # E21 trend for named-size peers, pro forma for mergers (Eastern's HarborOne, Beacon's Brookline banks)
    rows = L.query_metrics(metrics=["loan_to_deposit_ratio"], period="2024Q3:2026Q2",
                           dimensions=["period", "organization"],
                           filters={"us_region": "New England", "organization_asset_band": ["$10B-$50B"]})["rows"]
    beacon = {r["period"]: r["loan_to_deposit_ratio"] for r in rows if r["organization"].startswith("BEACON")}
    Q.append(dict(id="E21", topic="trend, pro forma",
                  question="Loan-to-deposit ratio trend by banking organization for New England organizations with "
                           "$10B-$50B in assets since 2024",
                  expect="answer", headline=[["loan-to-deposit"]], numbers=[beacon["2026Q2"]], chart=True,
                  must_text=[["Beacon"], ["Eastern"], ["Independent"]],
                  not_text=["best loan-to-deposit", "worst loan-to-deposit"], key=rows))

    # E22 capital and reserves scorecard, peer group (v0.2.1)
    cap = ["cet1_ratio", "leverage_ratio", "allowance_to_loans"]
    rows = L.query_metrics(metrics=cap, period="2026Q2", dimensions=["organization"],
                           filters={"us_region": "New England", "organization_asset_band": ["$10B-$50B"]})["rows"]
    top = max(rows, key=lambda r: r["cet1_ratio"])
    Q.append(dict(id="E22", topic="capital and reserves (new in v0.2.1)",
                  question="Compare CET1 ratio, leverage ratio and allowance to loans for New England banking "
                           "organizations with $10B-$50B in assets, 2026Q2",
                  expect="answer", headline=[[top["organization"].split()[0]]], numbers=[top["cet1_ratio"]],
                  must_text=[["Beacon"], ["Eastern"]], not_text=["best allowance", "worst allowance"], key=rows))

    # E23 deposit mix, peer group (v0.2.2)
    mix = ["uninsured_deposit_share", "noninterest_bearing_deposit_share", "brokered_deposit_share"]
    rows = L.query_metrics(metrics=mix, period="2026Q2", dimensions=["organization"],
                           filters={"us_region": "New England", "organization_asset_band": ["$10B-$50B"]})["rows"]
    top = max(rows, key=lambda r: r["uninsured_deposit_share"])
    Q.append(dict(id="E23", topic="deposit mix (new in v0.2.2)",
                  question="Compare uninsured, non-interest-bearing and brokered deposit shares for New England "
                           "banking organizations with $10B-$50B in assets, 2026Q2",
                  expect="answer", headline=[["deposit"]], must_text=[[top["organization"].split()[0].title()]],
                  numbers=[top["uninsured_deposit_share"]], not_text=["best uninsured", "worst uninsured"], key=rows))

    # E25-E26 deposit market share (v0.2.4, Summary of Deposits)
    m = L.market_share("metro", "Worcester, MA")
    top = m["rows"][0]
    Q.append(dict(id="E25", topic="market share (new in v0.2.4)",
                  question="Who has the largest deposit market share in the Worcester, MA metro area?",
                  expect="answer", headline=[[" ".join(w for w in top["organization"].split()
                                                     if w not in ("CORP", "INC", "THE", "CO")).title()]],
                  must_text=[["June 30"]], numbers=[top["deposit_share"]], chart=True, key=m))
    m = L.market_share("county", "MA", organization="EASTERN BANKSHARES INC", limit=25)
    top = max(m["rows"], key=lambda r: r["deposit_share"])
    Q.append(dict(id="E26", topic="branch footprint (new in v0.2.4)",
                  question="In which Massachusetts counties does Eastern Bankshares have its highest deposit market share?",
                  expect="answer", headline=[[top["county"].split(",")[0]]], numbers=[top["deposit_share"]], key=m))

    # E27-E28 app samples added with v0.2.4
    m = L.market_share("metro", "Boston", compare_to_prior_year=True)
    Q.append(dict(id="E27", topic="market share, custody caveat",
                  question="Deposit market share in the Boston metro area as of June 30, 2026, with change from 2025",
                  expect="answer", headline=[["State Street", "Boston"]],
                  must_text=[["Bank of America"], ["custody", "head office", "head-office", "one office", "booked"],
                             ["HHI"]],
                  numbers=[m["rows"][0]["deposit_share"]], chart=True, key=m))
    mix = ["cre_loan_share", "residential_loan_share", "ci_loan_share"]
    rows = L.query_metrics(metrics=mix, period="2026Q2", dimensions=["organization"],
                           filters={"us_region": "New England", "organization_asset_band": ["$10B-$50B"]})["rows"]
    top = max(rows, key=lambda r: r["cre_loan_share"])
    Q.append(dict(id="E28", topic="loan mix, peer group",
                  question="Compare CRE, residential and C&I loan shares for New England banking organizations with "
                           "$10B-$50B in assets, 2026Q2",
                  expect="answer", headline=[["loan", "CRE"]], must_text=[[top["organization"].split()[0].title()]],
                  numbers=[top["cre_loan_share"]], not_text=["best", "worst"], chart=True, key=rows))

    # E13-E15 were out of scope in v0.1; answerable since v0.2.0-v0.2.3
    r = L.query_metrics(metrics=["cost_of_deposits"], period="2026Q2", filters={"size_class": "$10B-$100B"})["rows"][0]
    Q.append(dict(id="E13", topic="funding cost (new in v0.2.0)",
                  question="What's the cost of deposits for $10B-$100B banks?",
                  expect="answer", headline=[["cost of deposits", "deposit cost", "cost"]],
                  numbers=[r["cost_of_deposits"]], key=r))
    r = L.query_metrics(metrics=["cre_loan_share"], period="2026Q2", filters={"size_class": "Under $10B"})["rows"][0]
    Q.append(dict(id="E14", topic="loan mix (new in v0.2.3)",
                  question="What share of loans is commercial real estate at banks under $10B?",
                  expect="answer", headline=[["commercial real estate", "CRE"]], numbers=[r["cre_loan_share"]], key=r))
    # E24 CRE concentration, peer group (v0.2.3)
    rows = L.query_metrics(metrics=["cre_concentration"], period="2026Q2", dimensions=["organization"],
                           filters={"us_region": "New England", "organization_asset_band": ["$5B-$10B", "$10B-$50B"]},
                           order_by="cre_concentration desc")["rows"]
    Q.append(dict(id="E24", topic="CRE concentration (new in v0.2.3)",
                  question="Which New England banking organizations with $5B-$50B in assets have the highest CRE "
                           "concentration relative to capital, 2026Q2?",
                  expect="answer", headline=[["CRE", "commercial real estate"]],
                  must_text=[[rows[0]["organization"].split()[0].title()], ["300%"]],
                  numbers=[rows[0]["cre_concentration"]], not_text=["breach", "violat", "best", "worst"], chart=True,
                  key=rows))
    rows = L.query_metrics(metrics=["uninsured_deposit_share"], period="2026Q2", dimensions=["organization"],
                           filters={"organization_asset_band": ["$10B-$50B", "$50B-$100B"]},
                           order_by="uninsured_deposit_share desc", limit=10)["rows"]
    Q.append(dict(id="E15", topic="uninsured deposits (new in v0.2.2)",
                  question="Which banks have the highest uninsured deposit share?",
                  expect="answer", headline=[["uninsured"]], key=rows))
    return Q


if __name__ == "__main__":
    import json
    for q in build():
        print(q["id"], q["topic"], "|", q["question"])
        print("   headline:", q.get("headline"), "numbers:", [round(n, 2) for n in q.get("numbers", [])])
    _ = json
