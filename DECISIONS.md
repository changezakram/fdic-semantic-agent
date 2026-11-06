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
- 2026-11-04: App: code checks ranking claims (highest/lowest/best/led) against computed rankings before an answer is shown; code-computed scorecard with best values highlighted for directional metrics only
- 2026-11-04: App checks found while testing executive questions: concentration check (one bank driving a group's ratio is reported with the group's rate without it; Oklahoma noncurrent = MidFirst); result cap 50 -> 60 rows so all states fit; every percentage must trace to a governed value or a calculator result
- 2026-11-04: Semantic model v0.1.4: geography_type (states and DC vs territories); territories and Unknown kept out of rankings, charts and scorecards
- 2026-11-04: Executive questions benchmark (exec_questions.py, run_exec.py): 15 questions x 3, graded in code (headline, numbers, required and banned text, claim checks, chart). Baseline 40/45; 3 misses were over-strict checks, 2 real (industry growth from rounded or partial totals). Rule: totals include every filer and growth of totals comes from compare_to. Then 45/45. Not a held-out set
- 2026-11-04: Semantic model v0.1.5: us_region (Census divisions, e.g. New England) and organization_asset_band for peer groups; compare_to with one dimension returns the change for every group. Chart type decided in code by data shape: two-period change -> diverging bar (top 5 each way), more than 8 lines capped, series on scales 20x apart indexed to 100
- 2026-11-04: Sample questions limited to balance-sheet and credit measures that reconcile to call reports; ROA and efficiency samples removed

## 2026-11-05 — App extensions (semantic model v0.2.x)
All v0.2 changes are app-only. The frozen experiment (tag final-v1, semantic model v0.1.1, data/snapshot_2026Q2/) is unchanged; test_semantic_layer.py still passes 25/25 and 7/7 on the default layer. Extension metrics carry `extension: true` and exist only when the layer loads data/snapshot_2026Q2_ext/.
- 2026-11-05: Extended snapshot (02_fetch_more.py): 8 quarters of extra call-report fields (funding, deposit mix, capital, reserves, loan mix, noncurrent by type), FDIC structure history (841 events, 2024-2026) and Summary of Deposits 2025 and 2026 (about 76,000 branches each). Field names taken from FDIC's definition files, not memory
- 2026-11-05: v0.2.0 pro forma for mergers: acquired banks' history is attributed to the surviving bank (change codes 221, 223, 224, 811, through 2026-06-30; chains resolved). Reason: as-reported growth was inflated by acquisitions (three New England organizations showed 22-27% deposit growth as reported, -2.6% to +1.0% pro forma). Industry totals are unchanged by pro forma (tested)
- 2026-11-05: v0.2.0 quarterly ROA (default meaning of "ROA" in the app), net interest margin, cost of funds, cost of deposits, cost of interest-bearing deposits, from de-cumulated year-to-date fields. Within 0.03-0.07 points of FDIC's own quarterly ratios on average for 1,054 banks over $1B. Retires the aggregate-NIM out-of-scope item in the app
- 2026-11-05: Validation against one holding company's Q2 2026 earnings release: loans and allowance exact; deposits differ by intercompany cash; quarterly ROA 0.05-0.08 points above the release in normal quarters (bank vs holding company), NIM within 0.07 of the tax-equivalent margin. Decision: keep income ratios out of the app's sample questions
- 2026-11-05: Merger quarters flagged: quarterly income metrics in the quarter a merger closed and the next carry merger_quarter: true and a note; the app must caveat them and cannot call them best or worst. Found: one bank's merger-quarter ROA was 1.85% vs 0.94% in its release
- 2026-11-05: v0.2.1 capital and reserves: CET1, tier 1 risk-based, total risk-based and leverage ratios (bank level; group ratios rebuild risk-weighted assets per bank as tier 1 common / CET1 ratio), allowance to loans (no better direction), allowance to noncurrent loans. Tested: every multi-bank group's CET1 lies within its banks' range
- 2026-11-05: v0.2.2 deposit mix: uninsured (call-report estimate, includes collateralized and affiliate deposits; no better direction), brokered, non-interest-bearing, time and core deposit shares. Brokered exact and time deposits within $1M of the release
- 2026-11-05: v0.2.3 loan mix (shares of gross loans by call-report category), CRE concentration (construction + multifamily + non-owner-occupied nonresidential + unsecured CRE / total risk-based capital; CBLR banks: tier 1 + allowance) and construction concentration, noncurrent rate by loan type. Insured branches of foreign banks, which do not report the detail, are left out. CRE concentration within 1 point of the release's 317% once restated to bank-level capital
- 2026-11-05: Supervisory guideline levels (CRE 300%, construction 100%) are governed constants: answers on these metrics must state the level and must not call a bank above it in breach. Retires the loan-breakdown out-of-scope item in the app
- 2026-11-05: v0.2.4 deposit market share (SemanticLayer.market_share, app tool market_share): share, rank and branches by state, metro area or county, market HHI (1,800+ highly concentrated, 2023 DOJ/FTC Merger Guidelines), change since prior June 30, pro forma for mergers. Branch deposits equal call-report domestic deposits exactly for the test bank, and nationally within 0.4%. Retires the Summary of Deposits out-of-scope item in the app
- 2026-11-05: Market-share flags, enforced by answer checks: booked_at_few_offices (1-2 branches and 10%+ share: custody, internet or wholesale deposits; State Street holds 42% of Boston metro deposits at one office) and merged_since_prior (share changes can reflect deposits re-booked after a merger; one organization's home-county share fell 6.5 points while its total deposits fell 2.6%)
- 2026-11-05: Answer checks added: empty or cut-off final answers sent back; "worst change" claims judged on the change, not the level beside it; asset-band wording ("$5B to $50B") not read as a claim; headline names the leader when comparing named groups; concentration check also runs when results are grouped by state name (found in the full run: Rhode Island = Citizens, 1.07% without it)
- 2026-11-05: Executive benchmark extended to 28 questions (capital, deposit mix, CRE concentration, market share, branch footprint; E14 and E15 changed from required declines to answers). Full run 82/84 (98%); both misses were E20 (concentration check gap above), fixed and then 3/3. Not a held-out set
- 2026-11-05: Sample questions: 9 (deposit and loan gainers and losers, loan-to-deposit ratio and trend, noncurrent rates, credit by New England state, Boston deposit market share, a bank's county footprint, loan mix); each has a matching benchmark question
