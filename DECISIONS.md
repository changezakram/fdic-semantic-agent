## 2026-11-02 — Data snapshot
- Snapshot: 8 quarters 2024Q3–2026Q2, fetched 2026-11-02 (data/snapshot_2026Q2/)
- All 22 financial fields returned; RSSDID not returned (not needed)
- 57 CERTs file call reports but have no FDIC institutions record (verified one-by-one; control CERT 14 works).
  2026Q2: 56 banks, $30.3B, 0.11% of industry assets, largest $11B.
  Rule: keep in industry totals; bucket as "Unknown" in bank-attribute breakdowns.
- Population rule: a bank is in a quarter's population if it filed that quarter (not the ACTIVE flag).

## 2026-11-03 — Semantic model decisions (v0.1.0) and question sets
- Community bank = FDIC designation (CB = 1); "under $10B" is a separate size class
- Income questions default to the quarter; YTD only when asked
- ROA / charge-off rate on quarter-end balances (FDIC uses averages; noted in article)
- Aggregate industry NIM out of scope (governed decline)
- Growth uses each quarter's full filer population, not a matched panel
- Bank attributes as of Nov 2026 for all quarters; size class per quarter
- Dev set: 30 questions (25 in scope, 5 out of scope). Held-out: 12, LOCKED until final run
- Answer key: hand-written SQL on raw tables, cross-checked against the semantic base view (0 mismatches)
- questions_heldout.yaml sha256: 967a5e19ff85ee7b8e6e54f5c53f785b6894ca5efd357b664a6c3bfb748f7cd1
