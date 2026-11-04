"""Check that the semantic layer can answer every in-scope dev question correctly,
and refuses the out-of-scope ones.

For each question, this file holds the query_metrics call a correct agent would make
(written by hand) and checks the result against answer_key_dev.json. If a check fails,
the semantic layer has a gap, and it must be fixed before any model uses it.

    python test_semantic_layer.py
"""
import json

from semantic_layer import QueryError, SemanticLayer

sl = SemanticLayer()
KEY = {a["id"]: a for a in json.load(open("answer_key_dev.json"))["answers"]}


def value(res, metric, row=0):
    return res["rows"][row][metric]


def bn_to_tn(x):
    return x / 1000


# id -> (query_metrics kwargs, function that extracts the answer from the result)
CALLS = {
    "D01": (dict(metrics=["net_income"], period="2025Q3"), lambda r: value(r, "net_income")),
    "D02": (dict(metrics=["net_income"], period="2026Q2", basis="ytd"), lambda r: value(r, "net_income")),
    "D03": (dict(metrics=["net_income"], period="2025"), lambda r: value(r, "net_income")),
    "D04": (dict(metrics=["net_income"], period="2025Q1:2025Q4", order_by="net_income desc", limit=1),
            lambda r: value(r, "period")),
    "D05": (dict(metrics=["net_interest_income"], period="2026Q2", filters={"bank": 628}),
            lambda r: value(r, "net_interest_income")),
    "D06": (dict(metrics=["total_deposits"], period="2026Q2"), lambda r: bn_to_tn(value(r, "total_deposits"))),
    "D07": (dict(metrics=["bank_count"], period="2026Q2", filters={"size_class": "$100B and over"}),
            lambda r: value(r, "bank_count")),
    "D08": (dict(metrics=["return_on_assets"], period="2026Q2"), lambda r: value(r, "return_on_assets")),
    "D09": (dict(metrics=["efficiency_ratio"], period="2025"), lambda r: value(r, "efficiency_ratio")),
    "D10": (dict(metrics=["median_bank_roa"], period="2026Q2"), lambda r: value(r, "median_bank_roa")),
    "D11": (dict(metrics=["noncurrent_loan_rate"], period="2026Q2", filters={"size_class": "$100B and over"}),
            lambda r: value(r, "noncurrent_loan_rate")),
    "D12": (dict(metrics=["return_on_equity"], period="2025"), lambda r: value(r, "return_on_equity")),
    "D13": (dict(metrics=["total_deposits"], period="2026Q2", compare_to="prior_year"),
            lambda r: r["rows"][0]["change_pct"]),
    "D14": (dict(metrics=["return_on_assets"], period="2026Q2", compare_to="prior_year"),
            lambda r: r["rows"][0]["change_pp"]),
    "D15": (dict(metrics=["net_income"], period="2026Q2", compare_to="prior_year"),
            lambda r: r["rows"][0]["change_pct"]),
    "D16": (dict(metrics=["bank_count"], period="2026Q2", compare_to="2024Q3"),
            lambda r: -r["rows"][0]["change"]),
    "D17": (dict(metrics=["bank_count"], period="2024Q4"), lambda r: value(r, "bank_count")),
    "D18": (dict(metrics=["bank_count"], period="2026Q2", filters={"fdic_community_bank": "Community bank"}),
            lambda r: value(r, "bank_count")),
    "D19": (dict(metrics=["return_on_assets"], period="2026Q2", filters={"size_class": "Under $10B"}),
            lambda r: value(r, "return_on_assets")),
    "D20": (dict(metrics=["bank_count"], period="2025Q4", filters={"active_today": "Inactive"}),
            lambda r: value(r, "bank_count")),
    "D21": (dict(metrics=["total_assets"], period="2026Q2", filters={"holding_company": "Wintrust"}),
            lambda r: value(r, "total_assets")),
    "D22": (dict(metrics=["total_assets"], period="2026Q2", dimensions=["region"], order_by="total_assets desc", limit=1),
            lambda r: value(r, "region")),
    "D23": (dict(metrics=["total_assets"], period="2026Q2", filters={"regulator": "OCC"}),
            lambda r: bn_to_tn(value(r, "total_assets"))),
    "D24": (dict(metrics=["total_assets"], period="2026Q2", filters={"state": "TX"}),
            lambda r: value(r, "total_assets")),
    "D25": (dict(metrics=["total_assets"], period="2026Q2", dimensions=["organization"], order_by="total_assets desc", limit=7),
            lambda r: [row["organization"] for row in r["rows"]]),
}

# Out-of-scope questions: the call a reasonable agent might try, which must be refused.
REFUSALS = {
    "D26": dict(metrics=["branch_count"], period="2026Q2", filters={"bank": 3510}),
    "D27": dict(metrics=["net_income"], period="2026Q4"),
    "D28": dict(metrics=["commercial_real_estate_loans"], period="2026Q2"),
    "D29": dict(metrics=["net_interest_margin"], period="2026Q2"),
    "D30": dict(metrics=["total_assets"], period="2026Q2", filters={"charter_type": "Credit union"}),
    # extra guardrails
    "quarterly 2024Q3": dict(metrics=["net_income"], period="2024Q3"),
    "bank figure without a bank": dict(metrics=["reported_roa"], period="2026Q2"),
}


def check(qid, got):
    a = KEY[qid]
    exp, tol = a["expected"], a["tolerance"]
    if "relative" in tol:
        return abs(got - exp) <= tol["relative"] * abs(exp)
    if "absolute" in tol:
        return abs(got - exp) <= tol["absolute"]
    if a["type"] == "list":
        return [g.lower() for g in got] == [e.lower() for e in exp]
    if a["type"] == "label":
        return str(got).lower() == str(exp).lower()
    return got == exp


def main():
    passed = 0
    print("In-scope questions (answer must match the key):")
    for qid, (kwargs, extract) in CALLS.items():
        try:
            got = extract(sl.query_metrics(**kwargs))
            ok = check(qid, got)
        except QueryError as e:
            got, ok = f"REFUSED: {e}", False
        passed += ok
        print(f"  {'PASS' if ok else 'FAIL'}  {qid}  got={got if not isinstance(got, float) else round(got, 4)}  expected={KEY[qid]['expected']}")

    refused = 0
    print("\nOut-of-scope and guardrail checks (must be refused):")
    for name, kwargs in REFUSALS.items():
        try:
            sl.query_metrics(**kwargs)
            print(f"  FAIL  {name}  was answered")
        except QueryError as e:
            refused += 1
            print(f"  PASS  {name}  refused: {str(e)[:110]}")

    print(f"\n{passed}/{len(CALLS)} in-scope answers match the key; {refused}/{len(REFUSALS)} refusals correct.")
    print(f"Semantic model version: {sl.version}")


if __name__ == "__main__":
    main()
