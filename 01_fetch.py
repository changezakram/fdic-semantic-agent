"""Step 1: Download public FDIC call-report data into a local DuckDB file.

Pulls quarterly financials for every FDIC-insured bank plus basic bank details
(name, state, charter class) from the FDIC BankFind API. No API key needed.

    python 01_fetch.py                      # last 8 quarters ending 2026-06-30
    python 01_fetch.py --end 20260930 --quarters 12

Writes fdic.duckdb with two raw tables, kept exactly as the FDIC publishes them:
  raw_financials   one row per bank per quarter (dollar amounts in $ thousands)
  raw_institutions one row per bank
"""
import argparse
import time

import duckdb
import pandas as pd
import requests

API = "https://api.fdic.gov/banks"
HEADERS = {"User-Agent": "personal research project (public data)"}
PAGE = 10000

# Raw call-report fields. Income fields are YEAR-TO-DATE in the FDIC data.
FIN_FIELDS = [
    "CERT", "REPDTE",
    "ASSET", "DEP", "DEPDOM", "LNLSGR", "LNLSNET", "EQ",          # balance sheet ($000)
    "INTINC", "EINTEXP", "NIM", "NONII", "NONIX", "ELNATR", "NETINC",  # income, YTD ($000)
    "NTLNLS", "NCLNLS",                                            # charge-offs (YTD), noncurrent loans
    "ROA", "ROE", "NIMY", "EEFFR",                                 # ratios (%, YTD annualized)
    "RBC1AAJ",                                                     # Tier 1 leverage ratio (%)
]
# Bank-level fields, including those for the ontology (holding company, regulator, geography).
# The API silently drops field names it doesn't recognize; the script reports any that come back missing.
INST_FIELDS = ["CERT", "NAME", "CITY", "STALP", "STNAME", "BKCLASS", "ACTIVE", "ESTYMD",
               "NAMEHCR", "RSSDHCR", "RSSDID", "REGAGNT", "CHRTAGNT", "SPECGRPN", "CB", "FDICREGN"]


def quarter_ends(end, n):
    """Return n quarter-end dates (YYYYMMDD strings) ending at `end`."""
    ends = pd.date_range(end=pd.Timestamp(end), periods=n, freq="QE")
    return [d.strftime("%Y%m%d") for d in ends]


def fetch(endpoint, filters, fields):
    rows, offset = [], 0
    while True:
        params = {"filters": filters, "fields": ",".join(fields), "limit": PAGE,
                  "offset": offset, "format": "json", "sort_by": "CERT", "sort_order": "ASC"}
        r = requests.get(f"{API}/{endpoint}", params=params, headers=HEADERS, timeout=120)
        r.raise_for_status()
        body = r.json()
        batch = [item.get("data", item) for item in body.get("data", [])]
        rows.extend(batch)
        total = (body.get("meta") or {}).get("total", len(rows))
        offset += PAGE
        if not batch or offset >= total:
            break
        time.sleep(0.5)
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--end", default="20260630", help="last quarter-end, YYYYMMDD")
    ap.add_argument("--quarters", type=int, default=8)
    args = ap.parse_args()

    frames = []
    for q in quarter_ends(args.end, args.quarters):
        df = fetch("financials", f"REPDTE:{q}", FIN_FIELDS)
        print(f"  {q}: {len(df):,} banks")
        if not df.empty:
            frames.append(df)
    fin = pd.concat(frames, ignore_index=True)
    missing = [f for f in FIN_FIELDS if f not in fin.columns]
    if missing:
        print(f"  Note: the API did not return these fields: {missing}")
    fin = fin[[c for c in FIN_FIELDS if c in fin.columns]]

    certs = set(fin["CERT"])
    inst = fetch("institutions", "ACTIVE:1 OR ACTIVE:0", INST_FIELDS)
    inst = inst[inst["CERT"].isin(certs)][[c for c in INST_FIELDS if c in inst.columns]]
    missing_inst = [f for f in INST_FIELDS if f not in inst.columns]
    if missing_inst:
        print(f"  Note: these bank-level fields were not returned: {missing_inst}")

    con = duckdb.connect("fdic.duckdb")
    con.execute("CREATE OR REPLACE TABLE raw_financials AS SELECT * FROM fin")
    con.execute("CREATE OR REPLACE TABLE raw_institutions AS SELECT * FROM inst")
    print(f"Done. raw_financials: {len(fin):,} rows; raw_institutions: {len(inst):,} banks.")
    print(con.execute("SELECT REPDTE, COUNT(*) AS banks FROM raw_financials GROUP BY 1 ORDER BY 1").df())


if __name__ == "__main__":
    main()
