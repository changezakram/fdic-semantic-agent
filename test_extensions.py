"""Tests for the app's extensions to the semantic layer (v0.2.0+): pro forma for mergers and the new
quarterly metrics. The frozen experiment's layer is tested separately by test_semantic_layer.py.

    python test_extensions.py
"""
from semantic_layer import EXT_SNAPSHOT, SemanticLayer

PEERS = ["BEACON FINANCIAL CORP", "EASTERN BANKSHARES INC", "INDEPENDENT BANK CORP (MA)"]


def main():
    pf = SemanticLayer(ext=EXT_SNAPSHOT, pro_forma=True)
    rep = SemanticLayer(ext=EXT_SNAPSHOT)
    fails = 0

    def check(name, ok, detail=""):
        nonlocal fails
        fails += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}")

    print("Pro forma for mergers")
    for layer, label, expect in [(rep, "as reported", {"BEACON": 26.17, "EASTERN": 22.39, "INDEPENDENT": 26.78}),
                                 (pf, "pro forma", {"BEACON": -2.62, "EASTERN": 1.02, "INDEPENDENT": -0.15})]:
        rows = layer.query_metrics(metrics=["total_deposits"], period="2026Q2", dimensions=["organization"],
                                   filters={"organization": PEERS}, compare_to="prior_year")["rows"]
        got = {r["organization"].split()[0]: round(r["change_pct"], 2) for r in rows}
        check(f"deposit growth YoY, {label}", got == expect, str(got))
    # hand check from the raw tables: Beacon = Berkshire + Brookline + Bank Rhode Island + PCSB in 2025Q2
    hand = pf.con.execute("SELECT SUM(DEP) FROM raw_financials WHERE REPDTE = 20250630 "
                          "AND CERT IN (17798, 23621, 34147, 15995)").fetchone()[0]
    got = pf.query_metrics(metrics=["total_deposits"], period="2025Q2",
                           filters={"organization": ["BEACON FINANCIAL CORP"]})["rows"][0]["total_deposits"]
    check("Beacon 2025Q2 deposits = sum of its four predecessor banks", abs(got - hand / 1e6) < 1e-3,
          f"{got:.3f} vs {hand / 1e6:.3f}")
    ind = [pf.query_metrics(metrics=["total_deposits"], period=p)["rows"][0]["total_deposits"] for p in ("2025Q2", "2026Q2")]
    ind_r = [rep.query_metrics(metrics=["total_deposits"], period=p)["rows"][0]["total_deposits"] for p in ("2025Q2", "2026Q2")]
    check("industry totals unchanged by pro forma", all(abs(a - b) < 1e-6 for a, b in zip(ind, ind_r)))

    print("New metrics vs FDIC's own quarterly ratios (banks over $1B, 2026Q2)")
    q = """SELECT AVG(ABS(100*NETINC_Q*4/ASSET - ROAQ)), AVG(ABS(100*NIM_Q*4/ERNAST - NIMYQ)),
                  AVG(ABS(100*EINTEXP_Q*4/ERNAST - INTEXPYQ)), COUNT(*)
           FROM bank_quarter WHERE period = '2026Q2' AND ASSET > 1000000 AND ERNAST > 0"""
    roa, nim, cof, n = pf.con.execute(q).fetchone()
    check("quarterly ROA within 0.05 pts on average", roa < 0.05, f"{roa:.3f} ({n} banks)")
    check("net interest margin within 0.10 pts on average", nim < 0.10, f"{nim:.3f}")
    check("cost of funds within 0.05 pts on average", cof < 0.05, f"{cof:.3f}")
    b = pf.query_metrics(metrics=["return_on_assets_quarterly", "net_interest_margin", "cost_of_funds"],
                         period="2026Q2", filters={"bank": 17798})["rows"][0]
    check("Beacon Bank & Trust: ROA 1.22, NIM 3.88, cost of funds 1.90 (FDIC)",
          [round(b[k], 2) for k in ("return_on_assets_quarterly", "net_interest_margin", "cost_of_funds")]
          == [1.22, 3.88, 1.90], str(b))
    try:
        r = pf.query_metrics(metrics=["return_on_assets_quarterly"], period="2024Q3")["rows"]
        check("no quarterly figure for 2024Q3 (needs 2024Q2)", r[0]["return_on_assets_quarterly"] is None)
    except Exception:  # noqa: BLE001
        check("no quarterly figure for 2024Q3 (refused)", True)

    print("Capital and reserves (v0.2.1)")
    b = pf.query_metrics(metrics=["cet1_ratio", "total_risk_based_ratio", "leverage_ratio", "allowance_to_loans"],
                         period="2026Q2", filters={"bank": 17798})["rows"][0]
    check("Beacon Bank & Trust: CET1 11.77, total RBC 12.86, leverage 9.83 (FDIC); allowance/loans 1.34 (release)",
          [round(b[k], 2) for k in ("cet1_ratio", "total_risk_based_ratio", "leverage_ratio", "allowance_to_loans")]
          == [11.77, 12.86, 9.83, 1.34], str(b))
    al = pf.con.execute("SELECT LNLSRES FROM bank_quarter WHERE CERT = 17798 AND period = '2026Q2'").fetchone()[0]
    check("Beacon allowance = $238,189K (release)", al == 238189, str(al))
    q = """SELECT hc_name, MIN(RBCT1CER), MAX(RBCT1CER) FROM bank_quarter WHERE period = '2026Q2' AND RBCT1CER > 0
           AND hc_name IS NOT NULL GROUP BY 1 HAVING COUNT(*) > 2"""
    bad = []
    for h, lo, hi in pf.con.execute(q).fetchall():
        v = pf.query_metrics(metrics=["cet1_ratio"], period="2026Q2", filters={"holding_company": [h]})["rows"][0]["cet1_ratio"]
        if not (lo - 0.01 <= v <= hi + 0.01):
            bad.append(h)
    check("every multi-bank group's CET1 lies within its banks' range", not bad, ", ".join(bad[:3]))

    print("Deposit mix (v0.2.2)")
    d = pf.con.execute("SELECT BRO, DEPNI, NTRTIME FROM bank_quarter WHERE CERT = 17798 AND period = '2026Q2'").fetchone()
    check("Beacon brokered deposits = $231,357K (release)", d[0] == 231357, str(d[0]))
    check("Beacon time deposits = CDs + brokered in release ($4,064,518K + $231,357K, within $1M)",
          abs(d[2] - (4064518 + 231357)) < 1000, str(d[2]))
    check("Beacon non-interest-bearing within intercompany cash of release ($3,910,604K, within $200M)",
          0 <= d[1] - 3910604 < 200000, str(d[1]))
    m = pf.query_metrics(metrics=["uninsured_deposit_share", "brokered_deposit_share", "noninterest_bearing_deposit_share",
                                  "time_deposit_share", "core_deposit_share"], period="2026Q2")["rows"][0]
    check("industry deposit shares are all between 0% and 100%", all(0 < v < 100 for v in m.values()), str(m))

    print("Loan mix and CRE concentration (v0.2.3)")
    lm = pf.con.execute("""SELECT LNRE, LNRECONS + LNREMULT + LNRENRES + LNRERES, LNRENRES, LNRENROW + LNRENROT,
                                  NCRE, P9RECONS + NARECONS + P9REMULT + NAREMULT + P9RENRES + NARENRES + P9RERES + NARERES
                           FROM bank_quarter WHERE CERT = 17798 AND period = '2026Q2'""").fetchone()
    check("Beacon real estate loans = construction + multifamily + nonresidential + 1-4 family (farmland < $1M)",
          0 <= lm[0] - lm[1] < 1000, f"{lm[0]} vs {lm[1]}")
    check("Beacon nonresidential = owner-occupied + other", lm[2] == lm[3])
    check("Beacon noncurrent real estate = sum of the types", lm[4] == lm[5], f"{lm[4]} vs {lm[5]}")
    b = pf.query_metrics(metrics=["cre_concentration", "total_risk_based_ratio"], period="2026Q2",
                         filters={"bank": 17798})["rows"][0]
    # release: 317% of total risk-based capital, consolidated, at a 13.6% total capital ratio. Same loans over
    # the bank's smaller capital: 317 x 13.6 / bank ratio
    implied = 317 * 13.6 / b["total_risk_based_ratio"]
    check("Beacon CRE concentration matches the release's 317% once restated to bank capital (within 3 pts)",
          abs(b["cre_concentration"] - implied) < 3, f"{b['cre_concentration']:.1f} vs {implied:.1f}")
    mix = ["construction_loan_share", "multifamily_loan_share", "owner_occupied_cre_loan_share",
           "non_owner_occupied_cre_loan_share", "residential_loan_share", "ci_loan_share", "consumer_loan_share"]
    m = pf.query_metrics(metrics=mix + ["cre_loan_share"], period="2026Q2")["rows"][0]
    check("industry loan mix: the parts sum to less than 100%", sum(m[k] for k in mix) < 100, f"{sum(m[k] for k in mix):.1f}")
    check("industry CRE share = construction + multifamily + nonresidential",
          abs(m["cre_loan_share"] - sum(m[k] for k in mix[:4])) < 0.01)

    print(f"\n{'All extension tests pass' if not fails else f'{fails} FAILED'} (semantic model v{pf.version})")
    return fails


if __name__ == "__main__":
    raise SystemExit(main())
