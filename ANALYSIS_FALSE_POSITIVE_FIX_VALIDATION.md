# Analysis False-Positive Fix Validation

Generated after the systemic Analysis Phase false-positive fixes on 2026-07-02.

## What Was Fixed

- `confidence_tier` is now derived from `evidence_strength` through one canonical mapping.
- Patterns 10 and 17 no longer accept common amount/date coincidence as standalone evidence.
- Pattern 8 money-trail auto-trigger now requires higher-signal context or a stricter runtime extreme-credit threshold, and common repeated amounts are guarded.
- Pattern 9 credit-to-cash now uses account-relative thresholds and excludes recurring cash habits.
- Pattern 11 now surfaces representative evidence instead of dumping all account transaction IDs.
- Pattern 1 duplicate verification was regression-tested with one true duplicate and one extraction false positive.

## Real Extraction Validation

Input:

- `outputs/extractions/run_20260701_233316`

Before:

- `analysis/outputs/run_20260701_233748/analysis_results.json`

After:

- `analysis/outputs/run_20260701_233316_fixed_validation/analysis_results.json`

| Pattern | Before | After |
|---|---:|---:|
| 1_duplicate_verification | 7 | 7 |
| 2_failed_reversed_transaction_detection | 1 | 1 |
| 3_pass_through_routing_account | 0 | 0 |
| 4_fund_pooling_account | 0 | 0 |
| 5_structuring_smurfing_detection | 3 | 3 |
| 6_money_flow_graph_construction | 1 | 1 |
| 7_circular_flow_multi_hop_cycle_detection | 14 | 14 |
| 8_money_trail_tracing | 100 | 9 |
| 9_credit_to_cash_out_chains | 273 | 0 |
| 10_cross_statement_links | 116 | 55 |
| 11_balance_parking_account | 1 | 1 |
| 12_hub_ranking | 0 | 0 |
| 13_low_value_account_testing | 0 | 0 |
| 14_reversal_clusters | 0 | 0 |
| 15_round_value_debit_patterns | 0 | 0 |
| 16_shared_upi_identifiers | 4 | 4 |
| 17_round_trip_detection | 111 | 3 |
| 18_dormant_reactivation | 0 | 0 |
| 19_first_contact_large_transfer | 129 | 129 |
| 21_suspicious_account_ranking | 4 | 4 |
| 22_llm_investigated_anomalies | 1 | 1 |
| 23_ml_ensemble_anomaly_lead | 0 | 0 |

## Key Quality Checks

| Check | Before | After |
|---|---:|---:|
| `confidence_tier` / `evidence_strength` mismatches | 288 | 0 |
| Pattern 8 auto money-trail findings | 100 | 9 |
| Pattern 9 cash-out findings | 273 | 0 |
| Pattern 10 cross-statement findings | 116 | 55 |
| Pattern 17 round-trip findings | 111 | 3 |
| Pattern 11 surfaced txn IDs | 2943 | 10 |

Remaining Pattern 10 and Pattern 17 findings are now corroborated:

- Pattern 10: `29 reference_match`, `26 narration_match`
- Pattern 17: `3 narration_match`

Pattern 8 final trigger mix:

- `7 auto_strong_account_credit`
- `2 auto_extreme_credit`

Pattern 8 final chain specificity:

- `5 specific_follow_on_debits`
- `4 overlaps_other_patterns`

## Synthetic Regression Results

Commands run:

```bash
python3 analysis/tests/test_priority_pattern_edge_cases.py
python3 analysis/tests/test_pipeline.py
python3 analysis/tests/test_all_build_cases.py
python3 analysis/tests/test_heldout_cases.py
```

Results:

- Priority edge-case tests: passed
- Pipeline smoke test: passed
- Build-case validation: `37/37` passed
- Held-out validation: `19/19` passed

## Consolidated CHANGES.md Implementation Validation

Implemented from `/Users/tejas.m.s/Downloads/CHANGES.md`:

- Confirmed existing `source_account_id` plumbing remains the analysis join key for extracted statements.
- Confirmed Money Trail auto-trigger remains active for significant runtime credits and flagged-account credits.
- Replaced broad pattern-count scoring with tiered scoring:
  - Tier 1 structural evidence carries the highest weight.
  - Tier 2 behavioral evidence can independently rank an account.
  - Tier 3 evidence is weak/corroborating only and cannot independently rank an account.
  - ML and LLM anomaly leads sit below Tier 3 and do not independently flag accounts.
  - Overlapping findings on the same transactions are reduced to avoid double counting.
- Added numeric `confidence_score` to findings and graph edges, with `confidence_tier` derived from the score.
- Added relationship reconstruction outputs:
  - `case_structure`
  - `cluster_summaries`
  - `case_summary`
  - `network_graph_for_display`
  - `weak_signal_accounts`
- Tightened LLM status reporting to the requested states:
  - `active`
  - `disabled_no_key`
  - `partial_failure`
  - `all_keys_exhausted`
- Added the requested graph output structure:
  - `graphs/ui_graphs/money_flow_network_3d.json`
  - `graphs/ui_graphs/balance_graph_all_accounts.json`
  - `graphs/ui_graphs/money_trail_all_accounts.json`
  - `graphs/ui_graphs/sankey_flow.json`
  - `graphs/report_graphs/account_interconnection_graph.png`
  - `graphs/report_graphs/suspicious_timeline.png`
  - `graphs/report_graphs/fraud_pattern_summary.png`

### Implementation Notes

- Pattern 19 detection logic was not restricted; its scoring influence is reduced by assigning it to Tier 3.
- Cross-statement links now carry `link_scope` metadata:
  - `intra_statement_link` is Tier 3.
  - genuine cross-document reference matches are Tier 1.
- Pattern 5 and Pattern 13 remain merged under the existing pass-through/routing detector output.
- Pattern 6 and Pattern 14 remain separately represented as `fund_pooling_account` and `balance_parking_account`.
- The report Account Interconnection Graph is now rendered from `network_graph_for_display` and `case_structure` metadata first, with a fallback to the raw graph if older results do not contain those fields.

### Money-Trail Case Score Validation After Tiered Scoring

Commands run:

```bash
python3 -m analysis.analysis_engine.cli --input analysis/synthetic_cases/pattern_07_money_trail/mt_case_01.csv --output-dir analysis/outputs/mt_case_01_changes_final --no-llm
python3 -m analysis.analysis_engine.cli --input analysis/synthetic_cases/pattern_07_money_trail/mt_case_02.csv --output-dir analysis/outputs/mt_case_02_changes_final --no-llm
python3 -m analysis.analysis_engine.cli --input analysis/synthetic_cases/pattern_07_money_trail/mt_case_03.csv --output-dir analysis/outputs/mt_case_03_changes_final --no-llm
python3 -m analysis.analysis_engine.cli --input analysis/synthetic_cases/pattern_07_money_trail/mt_case_04.csv --output-dir analysis/outputs/mt_case_04_changes_final --no-llm
```

| Case | Ranked Accounts | Top Fraud Account / Score | Previously Tracked Innocent Account | Innocent Ranked? | Score Gap |
|---|---:|---|---|---|---:|
| `mt_case_01` | 3 | `3545244589369467` / `230.0701` | `959195047945` | No | `230.0701` |
| `mt_case_02` | 4 | `47569602855` / `175.6859` | `68578940765363` | No | `175.6859` |
| `mt_case_03` | 4 | `95599894774085` / `404.8760` | `4250634202` | Yes, due to Pattern 17 + Pattern 19 | `312.6760` |
| `mt_case_04` | 3 | `97432838969` / `210.7877` | `33631771149450` | No | `210.7877` |

The Pattern 16/19-only ranking leak remains removed. The remaining `mt_case_03`
tracked account is still ranked because it has Pattern 17 round-trip evidence,
not because weak-only evidence can independently push it into the ranking.

### Graph Output Validation

Validation run:

```bash
python3 -m analysis.analysis_engine.cli --input analysis/synthetic_cases/pattern_07_money_trail/mt_case_01.csv --output-dir analysis/outputs/mt_case_01_changes_validation --no-llm
```

All seven requested graph outputs were generated and non-empty:

| Output | Size |
|---|---:|
| `graphs/ui_graphs/money_flow_network_3d.json` | `146446` bytes |
| `graphs/ui_graphs/balance_graph_all_accounts.json` | `78579` bytes |
| `graphs/ui_graphs/money_trail_all_accounts.json` | `41558` bytes |
| `graphs/ui_graphs/sankey_flow.json` | `42` bytes |
| `graphs/report_graphs/account_interconnection_graph.png` | `1343124` bytes |
| `graphs/report_graphs/suspicious_timeline.png` | `230892` bytes |
| `graphs/report_graphs/fraud_pattern_summary.png` | `158226` bytes |

`sankey_flow.json` is intentionally tiny on this validation case because all
detected nodes belong to one connected cluster, so there are no cross-cluster
aggregate flows to list.

### LLM Status Validation

The LLM status state machine was checked without making live calls or printing
any API keys:

| Simulated client state | Reported `llm_status` |
|---|---|
| no loaded keys | `disabled_no_key` |
| all keys active | `active` |
| one active key and one failed key | `partial_failure` |
| all attempted keys invalid | `all_keys_exhausted` |
| all attempted keys rate-limited | `all_keys_exhausted` |

### Regression Results After CHANGES.md Implementation

Commands run:

```bash
python3 -m py_compile analysis/analysis_engine/models.py analysis/analysis_engine/detectors/money_trail.py analysis/analysis_engine/scoring.py analysis/analysis_engine/case_structure.py analysis/analysis_engine/graph_generator.py analysis/analysis_engine/pipeline.py analysis/analysis_engine/graph.py analysis/analysis_engine/llm_client.py analysis/analysis_engine/detectors/ml_ensemble.py analysis/analysis_engine/detectors/cross_statement.py analysis/tests/test_priority_pattern_edge_cases.py
python3 analysis/tests/test_priority_pattern_edge_cases.py
python3 analysis/tests/test_pipeline.py
python3 analysis/tests/test_all_build_cases.py
python3 analysis/tests/test_heldout_cases.py
```

Results:

- Priority edge-case tests: passed
- Pipeline smoke test: passed
- Build-case validation: `37/37` passed
- Held-out validation: `19/19` passed

## Duplicate Verification Finding

The real extraction run still has:

- Pattern 1 categories: `7 possible_extraction_false_positive`

The new synthetic regression confirms analysis can distinguish:

- a genuine duplicate pair, which is confirmed
- an extraction-flagged non-duplicate pair, which is reclassified as false positive

So the 0% confirmation rate in this real run is most likely extraction over-flagging, not an Analysis Phase duplicate-verification bug.

## Follow-Up Validation: Cross-Statement, Cash-Out, And Money-Trail Gating

Follow-up run:

- Input extraction: `outputs/extractions/run_20260701_233316`
- Output run: `analysis/outputs/run_20260701_233316_followup_validation`
- Row basis: same 9816-row real extraction run
- LLM mode: disabled for deterministic detector validation

### Remaining Issues Addressed

| Issue | Root Cause | Fix |
|---|---|---|
| Pattern 10 still trusted fake corroboration | Resolver method names like `exact_reference_or_upi_match` and `narration_similarity_match` were treated as pair-level proof even when the two matched rows did not share the same reference, VPA, or meaningful narration tokens. | Pattern 10 now verifies shared references/VPAs directly and requires non-generic narration-token overlap before accepting narration corroboration. Amount/date mirror links on common amounts are blocked unless there is real corroboration. |
| Pattern 9 dropped from 273 to 0 | The credit floor was too high for real account distributions and the account-wide recurring cash habit exclusion suppressed high-ratio credit-to-cash chains. | Pattern 9 now uses the account p75 credit floor, excludes opening/balance-forward credits from floor/candidate selection, and lets high-ratio chains survive the recurring cash habit filter when withdrawal ratio is at least `0.70`. |
| Pattern 8 needed auditable gating provenance | Auto-selected money-trail findings had trigger reasons but not explicit gating reasons explaining why the credit was allowed into tracing. | Pattern 8 now records `gated_reason` for manual, prior-pattern-overlap, and extreme-rare-credit paths. |

### Follow-Up Counts

| Pattern | Previous Fixed Validation | Follow-Up Validation |
|---|---:|---:|
| 8_money_trail_tracing | 9 | 9 |
| 9_credit_to_cash_out_chains | 0 | 5 |
| 10_cross_statement_links | 55 | 0 |
| 17_round_trip_detection | 3 | 0 |
| Total findings | 232 | 179 |

Pattern 10 is now zero on this real run because the previous 55 links did not survive direct evidence checks. The earlier examples had mismatched references such as `577356133501` versus `27014275`, or unrelated narrations such as cheque clearing versus cash deposit; those are now rejected instead of reported.

### Spot Checks

Pattern 9 now includes the previously missed `acct_001_000132` candidate:

- Credit: `acct_001_000132`, `2022-09-08`, credit `18500.00`, narration `UPI/CR/5964463574/.../ravi61@ybl`
- Follow-on ATM debits within the 3-day detector window:
  - `acct_001_000136`, `2022-09-10`, debit `3499.00`
  - `acct_001_000138`, `2022-09-10`, debit `3499.00`
  - `acct_001_000139`, `2022-09-10`, debit `7800.00`
- ATM total: `14798.00`
- Withdrawal ratio: `0.7999`
- Runtime thresholds: `credit_min=18500.0`, `global_credit_min=5200.0`, `account_credit_quantile=0.75`, `window_days=3`, `high_ratio_override=0.70`
- Recurring cash habit was detected, but not excluded because the ratio exceeded the high-ratio override.

Pattern 8 follow-up findings now carry `gated_reason`:

- `auto_extreme_credit` with `gated_reason=extreme_credit_with_rare_amount`
- `auto_strong_account_credit` with `gated_reason=credit_txn_seen_in_prior_pattern_on_strong_account`
- Manual traces use `gated_reason=manual_investigator_requested_credit`

Pattern 10 follow-up result:

- `10_cross_statement_links`: `0`
- No findings remain with unsupported `reference_match` or `narration_match` corroboration.

### Regression Results After Follow-Up Fix

Commands run:

```bash
python3 -m py_compile analysis/analysis_engine/detectors/credit_to_cash.py
python3 analysis/tests/test_priority_pattern_edge_cases.py
python3 analysis/tests/test_pipeline.py
python3 analysis/tests/test_all_build_cases.py
python3 analysis/tests/test_heldout_cases.py
python3 -m analysis.analysis_engine.cli --input outputs/extractions/run_20260701_233316 --output-dir analysis/outputs/run_20260701_233316_followup_validation --no-llm
```

Results:

- Priority edge-case tests: passed
- Pipeline smoke test: passed
- Build-case validation: `37/37` passed
- Held-out validation: `19/19` passed
- Real 9816-row follow-up run: completed successfully

## Follow-Up Validation: Shared UPI, First Contact, And Ranking Guardrail

Follow-up patch scope:

- Pattern 16: `analysis/analysis_engine/detectors/shared_upi.py`
- Pattern 19: `analysis/analysis_engine/detectors/first_contact.py`
- Account ranking/scoring: `analysis/analysis_engine/scoring.py`
- Regression tests: `analysis/tests/test_priority_pattern_edge_cases.py`

### Fixes Applied

| Area | Problem | Fix |
|---|---|---|
| Pattern 16 shared UPI | Service and merchant VPAs such as `airtel.recharge@airtel` were treated like shared personal ownership signals. | Added service/merchant/biller VPA suppression and commonality suppression when a VPA appears across too many accounts. Small non-service subsets of 2-3 accounts still fire. |
| Pattern 19 first contact | Recurring monthly large transfers from similarly structured counterparty IDs were counted as separate first-contact events. | Pattern 19 now collects candidates first and suppresses recurring monthly-like clusters using runtime gap, day-of-month, amount-tolerance, and counterparty-structure checks. |
| Suspicious account ranking | Accounts could rank from broad/noisy Pattern 16 and/or 19 evidence alone. | Scoring now requires at least one fraud-specific pattern before an account enters `suspicious_accounts` or Pattern 21 ranking. Qualifying patterns: `3, 5, 7, 8, 9, 10, 11, 13, 17, 18`. |

### Money-Trail Case Validation

Commands run:

```bash
python3 -m analysis.analysis_engine.cli --input analysis/synthetic_cases/pattern_07_money_trail/mt_case_01.csv --output-dir analysis/outputs/mt_case_01_noise_guardrail --no-llm
python3 -m analysis.analysis_engine.cli --input analysis/synthetic_cases/pattern_07_money_trail/mt_case_02.csv --output-dir analysis/outputs/mt_case_02_noise_guardrail --no-llm
python3 -m analysis.analysis_engine.cli --input analysis/synthetic_cases/pattern_07_money_trail/mt_case_03.csv --output-dir analysis/outputs/mt_case_03_noise_guardrail --no-llm
python3 -m analysis.analysis_engine.cli --input analysis/synthetic_cases/pattern_07_money_trail/mt_case_04.csv --output-dir analysis/outputs/mt_case_04_noise_guardrail --no-llm
```

| Case | Pattern 16 Count | Pattern 19 Count | Ranked Accounts | Innocent Account Result |
|---|---:|---:|---:|---|
| `mt_case_01` | 0 | 23 | 4 | `959195047945` removed from ranking |
| `mt_case_02` | 1 | 17 | 4 | `68578940765363` removed from ranking |
| `mt_case_03` | 1 | 27 | 5 | `4250634202` still ranks, but due to Pattern 17, not Pattern 16/19-only leakage |
| `mt_case_04` | 1 | 21 | 4 | `33631771149450` removed from ranking |

Remaining Pattern 16 findings in cases 02-04 are small, non-service, two-account shared identifiers:

- `mt_case_02`: `39062252019@sbin0960001.ifsc.npci`
- `mt_case_03`: `80275595460@mahb0601820.ifsc.npci`
- `mt_case_04`: `8669887383030@cnrb0893218.ifsc.npci`

The `mt_case_03` innocent-account exception is outside this patch scope. It ranks because Pattern 17 reports two round-trip findings involving `4250634202` and `95599894774085`, using the pair `95599894774085_000070` / `95599894774085_000071` for matching `230000.00` debit/credit entries on `2025-06-15`. No Pattern 16/19-only ranking leak remains for that account.

### Unresolved Counterparty Spot Check

For `mt_case_01`, the unresolved counterparty rate remains:

- Eligible rows: `488`
- Unresolved rows: `72`
- Unresolved percentage: `14.75%`

Sample composition:

- `64` rows are ordinary non-counterparty or weak-counterparty families such as SMS/MAB charges and NACH EMI debits.
- `8` rows are ATM withdrawals.

The sample did not show an obvious missed fraud-transfer parser gap; most unresolved rows are legitimately low-context service/fee/ATM/NACH rows.

### Regression Results After Noise Guardrail Fix

Commands run:

```bash
python3 -m py_compile analysis/analysis_engine/detectors/shared_upi.py analysis/analysis_engine/detectors/first_contact.py analysis/analysis_engine/scoring.py analysis/tests/test_priority_pattern_edge_cases.py
python3 analysis/tests/test_priority_pattern_edge_cases.py
python3 analysis/tests/test_pipeline.py
python3 analysis/tests/test_all_build_cases.py
python3 analysis/tests/test_heldout_cases.py
```

Results:

- Priority edge-case tests: passed
- Pipeline smoke test: passed
- Build-case validation: `37/37` passed
- Held-out validation: `19/19` passed
