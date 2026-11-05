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

    print(f"\n{'All extension tests pass' if not fails else f'{fails} FAILED'} (semantic model v{pf.version})")
    return fails


if __name__ == "__main__":
    raise SystemExit(main())
