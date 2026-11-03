"""Build the answer key from the frozen snapshot.

Runs every question's SQL against the raw CSVs in data/snapshot_2026Q2/ and writes
answer_key_<set>.json with the expected answer and grading tolerance for each question.

    python build_answer_key.py dev        # writes answer_key_dev.json and prints it
    python build_answer_key.py heldout    # writes answer_key_heldout.json, prints nothing
"""
import hashlib
import json
import sys
from pathlib import Path

import duckdb
import yaml

SNAPSHOT = Path("data/snapshot_2026Q2")
TOLERANCE = {
    "dollars_bn": {"relative": 0.005},
    "dollars_tn": {"relative": 0.005},
    "ratio_pct":  {"absolute": 0.02},
    "growth_pct": {"absolute": 0.05},
    "pp_change":  {"absolute": 0.02},
    "count":      {"exact": True},
    "label":      {"exact": True},
    "list":       {"set_match": True},
    "decline":    {"must_decline": True},
}
DECIMALS = {"dollars_bn": 1, "dollars_tn": 3, "ratio_pct": 2, "growth_pct": 2, "pp_change": 2}


def load_snapshot():
    con = duckdb.connect()
    con.execute(f"CREATE TABLE raw_financials AS SELECT * FROM read_csv_auto('{SNAPSHOT / 'raw_financials.csv'}')")
    con.execute(f"CREATE TABLE raw_institutions AS SELECT * FROM read_csv_auto('{SNAPSHOT / 'raw_institutions.csv'}')")
    return con


def expected_answer(con, q):
    t = q["type"]
    if t == "decline":
        return None
    rows = con.execute(q["sql"]).fetchall()
    if t == "list":
        return [r[0] for r in rows]
    value = rows[0][0]
    if t == "count":
        return int(value)
    if t == "label":
        return str(value)
    return round(float(value), DECIMALS[t] + 2)   # keep 2 extra digits; grading uses the tolerance


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "dev"
    src = Path(f"questions_{which}.yaml")
    spec = yaml.safe_load(src.read_text())
    con = load_snapshot()

    key = {
        "set": which,
        "questions_file": src.name,
        "questions_sha256": hashlib.sha256(src.read_bytes()).hexdigest(),
        "snapshot": str(SNAPSHOT),
        "answers": [],
    }
    for q in spec["questions"]:
        entry = {
            "id": q["id"],
            "category": q["category"],
            "type": q["type"],
            "expected": expected_answer(con, q),
            "tolerance": TOLERANCE[q["type"]],
        }
        if "aliases" in q:
            entry["aliases"] = q["aliases"]
        if q["type"] in DECIMALS:
            entry["display"] = round(entry["expected"], DECIMALS[q["type"]])
        key["answers"].append(entry)

    out = Path(f"answer_key_{which}.json")
    out.write_text(json.dumps(key, indent=2))
    print(f"Wrote {out} ({len(key['answers'])} answers). questions sha256: {key['questions_sha256'][:16]}…")
    if which == "dev":
        for a in key["answers"]:
            shown = a.get("display", a["expected"])
            print(f"  {a['id']}  {a['type']:<11} {shown}")


if __name__ == "__main__":
    main()
