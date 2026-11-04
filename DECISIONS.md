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
- 2026-11-03: Arm C compiler (semantic_layer.py) passes 25/25 dev answers and 7/7 refusals; tools: query_metrics + list_values
- 2026-11-03: Semantic model v0.1.1 (added derived_terms glossary text). Prompts v1: A = schema; B = schema + glossary; C = same glossary + query_metrics/list_values. All arms end with structured final_answer
- 2026-11-03: Smoke test (D01, D08, D21, D29; 1 rep): A 1/4, B 4/4, C 4/4. Harness verified end to end
- 2026-11-03: Smoke test (D01, D08, D21, D29; 1 rep): A 1/4, B 4/4, C 4/4. Harness verified end to end
- 2026-11-03: Grader v2 (rounding-aware numbers, normalized org names, missing_value outcome). Fixes 2 false negatives found in baseline review (D09 Arm A x3, D25 Arm B x1); baseline re-graded, originals kept
- 2026-11-03: Added hard question sets (8 dev + 8 held-out, multi-rule, incl. 1 beyond-layer and 1 decline each), written after baseline showed B = C, before any tuning. Answers cross-checked via semantic layer (0 mismatches)
- questions_hard_heldout.yaml sha256: 543ac2688d1837acb25be6141434fe102aafdfad20cfa1822e413c1348b3008d
- 2026-11-03: Hard dev baseline (8 q x 3 x 3): A 50%, B 100%, C 83% (C: HD03 beyond-layer 0/3 with 2 declines, HD07 2/3)
- 2026-11-03: Grader v3: dollar answers in the governed format ($B 1 decimal, $T 2 decimals) accepted when equal to expected rounded that way. Found in review: Arm C reported $0.305B as $0.3B per the glossary. Both runs re-graded
- 2026-11-03: FROZEN for final run: model claude-haiku-5-5, prompts v1, semantic model v0.1.1, grader v3, max 12 turns, API default temperature, 3 reps. Ablations deferred to a follow-up (asymmetric degradation of B rejected as biased)
- 2026-11-04: Semantic model v0.1.2 for the analytics app: holding-company names shared by different companies get a state suffix (e.g. INDEPENDENT BANK CORP (MA) vs (MI)). Experiment results remain on v0.1.1 (tag final-v1)
- 2026-11-04: Semantic model v0.1.3: organization_size_class (holding-company total assets). App: rankings computed in code and attached to grouped results; no rankings from cut-off results; charts can list items to show. Found in benchmarking tests: model mis-ranked ROA; chart showed excluded peers
