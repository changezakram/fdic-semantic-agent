## 2026-11-02 — Data snapshot
- Snapshot: 8 quarters 2024Q3–2026Q2, fetched 2026-11-02 (data/snapshot_2026Q2/)
- All 22 financial fields returned; RSSDID not returned (not needed)
- 57 CERTs file call reports but have no FDIC institutions record (verified one-by-one; control CERT 14 works).
  2026Q2: 56 banks, $30.3B, 0.11% of industry assets, largest $11B.
  Rule: keep in industry totals; bucket as "Unknown" in bank-attribute breakdowns.
- Population rule: a bank is in a quarter's population if it filed that quarter (not the ACTIVE flag).
