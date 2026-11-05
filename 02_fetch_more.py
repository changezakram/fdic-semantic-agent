"""Fetch the extra FDIC data for the app (not the experiment).

Writes data/snapshot_2026Q2_ext/:
  fin_extra.csv        CERT, REPDTE + funding, deposit mix, capital, reserves, securities and loan-mix fields,
                       for the same 8 quarters as the main snapshot (2024Q3-2026Q2)
  merger_history.csv   institution-level structure events 2024-2026 with acquirer and acquired CERT
  sod_2025.csv, sod_2026.csv   Summary of Deposits, one row per branch (as of June 30)
  FIELD_CHECK.txt      how many banks have each field, per quarter (empty fields are flagged)
  FETCHED_AT.txt

The frozen experiment snapshot (data/snapshot_2026Q2) is not touched.
Field names come from FDIC's definition files (data/fdic_defs/*.yaml), not from memory.

Run:  python 02_fetch_more.py
"""
from __future__ import annotations

import csv
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

API = "https://api.fdic.gov/banks"
OUT = Path(__file__).parent / "data" / "snapshot_2026Q2_ext"
QUARTERS = ["20240930", "20241231", "20250331", "20250630", "20250930", "20251231", "20260331", "20260630"]

FIN_FIELDS = {
    "keys": ["CERT", "REPDTE"],
    # income and funding (quarterly versions match how banks report)
    "income_funding": ["NETINCQ", "ROA", "ROAQ", "ROE", "ROEQ", "NIMY", "NIMYQ", "INTINC", "EINTEXP", "EDEP",
                       "EDEPDOM", "ERNAST", "INTINCY", "INTINCYQ", "INTEXPY", "INTEXPYQ", "NONII", "NONIX",
                       "ELNATR"],
    # deposit mix
    "deposits": ["DEPDOM", "DEPINS", "DEPUNINS", "BRO", "DEPNI", "DEPNIDOM", "NTRTIME", "COREDEP"],
    # capital and reserves
    "capital": ["EQTOT", "RBCT1", "RBCT1C", "RBC1AAJ", "RBCT1CER", "IDT1RWAJR", "RBCRWAJ", "CBLRIND",
                "LNLSRES", "LNATRES"],
    # securities
    "securities": ["SC", "SCAF", "SCHA", "SCMV", "EQCCOMPI"],
    # credit: past due and nonaccrual
    "credit": ["P3LNLS", "P9LNLS", "NALNLS", "NCLNLS"],
    # loan mix
    "loan_mix": ["LNRE", "LNRECONS", "LNREMULT", "LNRENRES", "LNRENROW", "LNRENROT", "LNRERES", "LNRELOC",
                 "LNCOMRE", "LNCI", "LNCON", "LNCRCD", "LNAUTO", "LNCONOTH", "LNAG"],
    # noncurrent by loan type (noncurrent = 90+ days past due + nonaccrual)
    "noncurrent_by_type": ["NCRE", "NCCI", "NCCON",
                           "P9RECONS", "NARECONS", "P9REMULT", "NAREMULT", "P9RENRES", "NARENRES",
                           "P9RERES", "NARERES", "P9RELOC", "NARELOC", "P9CI", "NACI", "P9CON", "NACON",
                           "P9AG", "NAAG"],
    "staff": ["NUMEMP"],
}
HISTORY_FIELDS = ["TRANSNUM", "CHANGECODE", "CHANGECODE_DESC", "EFFDATE", "PROCDATE", "CERT", "INSTNAME",
                  "ACQ_CERT", "ACQ_INSTNAME", "OUT_CERT", "OUT_INSTNAME", "SUR_CERT", "SUR_INSTNAME", "ORG_ROLE_CDE",
                  "REPORT_TYPE"]
SOD_FIELDS = ["YEAR", "CERT", "RSSDID", "RSSDHCR", "NAMEFULL", "NAMEHCR", "UNINUMBR", "BRNUM", "NAMEBR",
              "CITYBR", "STALPBR", "CNTYNAMB", "STCNTYBR", "MSABR", "MSANAMB", "CSABR", "CSANAMBR", "BKMO",
              "BRSERTYP", "DEPSUMBR", "DEPSUM", "ASSET"]


def get_all(endpoint: str, filters: str, fields: list[str], page: int = 10000) -> list[dict]:
    rows, offset = [], 0
    while True:
        params = {"filters": filters, "fields": ",".join(fields), "limit": page, "offset": offset,
                  "format": "json"}
        for attempt in range(5):
            try:
                r = requests.get(f"{API}/{endpoint}", params=params, timeout=120)
                r.raise_for_status()
                break
            except requests.RequestException as e:
                if attempt == 4:
                    raise
                print(f"    retry {attempt + 1} after {e}")
                time.sleep(3 * (attempt + 1))
        body = r.json()
        batch = [d["data"] for d in body.get("data", [])]
        rows += batch
        total = body.get("meta", {}).get("total", 0)
        offset += len(batch)
        if not batch or offset >= total:
            return rows


def save(path: Path, rows: list[dict], fields: list[str]):
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"  saved {path.name}: {len(rows):,} rows")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    fields = [f for group in FIN_FIELDS.values() for f in group]

    # 1. call-report fields, 8 quarters
    print("Financials (extra fields)")
    fin = []
    for q in QUARTERS:
        part = get_all("financials", f"REPDTE:{q}", fields)
        print(f"  {q}: {len(part):,} banks")
        fin += part
    save(OUT / "fin_extra.csv", fin, fields)

    # field check: how many banks report each field, per quarter
    lines = ["Banks with a value, per field and quarter (0 = field missing or not reported)", ""]
    lines.append("FIELD".ljust(12) + "".join(q[:6].rjust(9) for q in QUARTERS))
    empty = []
    for f in fields[2:]:
        counts = [sum(1 for r in fin if str(r.get("REPDTE")) == q and r.get(f) not in (None, "")) for q in QUARTERS]
        lines.append(f.ljust(12) + "".join(str(c).rjust(9) for c in counts))
        if counts[-1] == 0:
            empty.append(f)
    lines += ["", "EMPTY in 2026Q2: " + (", ".join(empty) if empty else "none")]
    (OUT / "FIELD_CHECK.txt").write_text("\n".join(lines) + "\n")
    print("  field check written" + (f"; EMPTY: {', '.join(empty)}" if empty else "; all fields have data"))

    # 2. structure history: institution-level events since 2024
    print("Structure history (mergers)")
    hist = []
    for y in (2024, 2025, 2026):
        part = get_all("history", f"EFFYEAR:{y} AND ACQ_CERT:[1 TO *] AND OUT_CERT:[1 TO *]", HISTORY_FIELDS)
        if not part:     # range filter not accepted: take the year and keep acquirer-acquired pairs here
            part = [r for r in get_all("history", f"EFFYEAR:{y}", HISTORY_FIELDS)
                    if r.get("ACQ_CERT") and r.get("OUT_CERT")]
        print(f"  {y}: {len(part):,} events")
        hist += part
    save(OUT / "merger_history.csv", hist, HISTORY_FIELDS)

    # 3. Summary of Deposits, branch level
    print("Summary of Deposits (branches)")
    for y in (2025, 2026):
        part = get_all("sod", f"YEAR:{y}", SOD_FIELDS)
        save(OUT / f"sod_{y}.csv", part, SOD_FIELDS)

    # 4. a check against a known published figure (Beacon Bank & Trust, CERT 17798, 2026Q2)
    b = next((r for r in fin if str(r.get("CERT")) == "17798" and str(r.get("REPDTE")) == "20260630"), None)
    if b:
        print("Check, Beacon Bank & Trust 2026Q2:",
              f"nonaccrual {b.get('NALNLS')} (release: 152,650), 90+ days {b.get('P9LNLS')} (release: 7,785),",
              f"quarterly ROA {b.get('ROAQ')}, quarterly NIM {b.get('NIMYQ')}")
    (OUT / "FETCHED_AT.txt").write_text(datetime.now(timezone.utc).isoformat() + "\n")
    print(f"Done. Files in {OUT}")


if __name__ == "__main__":
    sys.exit(main())
