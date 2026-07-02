# Analysis Phase Explaination

This document explains what has happened so far in the analysis phase of the
forensic bank statement system. It reflects the current repository state after
the analysis-phase implementation and the latest fix-plan updates.

The analysis phase starts after extraction has already produced cleaned,
flagged, duplicate, metadata, and ledger outputs. Its job is not to re-extract
transactions. Its job is to take extraction output, normalize it into a stable
analysis database, run deterministic fraud-pattern detectors, build a money-flow
graph, rank suspicious accounts, and write reportable outputs for the later
report phase.

## Current Status

The analysis phase is implemented under:

```text
analysis/analysis_engine/
```

The Streamlit interface in `app.py` can now:

- run extraction
- automatically run analysis after extraction succeeds
- upload a final extracted CSV directly for analysis
- show analysis summary metrics
- show a graph view
- show priority findings
- show all scored patterns
- show lead-only LLM/ML findings
- download `analysis_results.json`, `analysis_summary.txt`, and `analysis.db`

The latest real-data validation run completed successfully against:

```text
outputs/extractions/run_20260701_123703
```

Validation output was written to:

```text
analysis/outputs/run_fix_plan_validation_20260701_r2/
```

Important generated files:

```text
analysis/outputs/run_fix_plan_validation_20260701_r2/analysis_results.json
analysis/outputs/run_fix_plan_validation_20260701_r2/analysis_summary.txt
analysis/outputs/run_fix_plan_validation_20260701_r2/analysis.db
```

## Main Design Decision

The most important architecture decision is:

```text
source_account_id is the analysis grouping key.
```

That means the analysis phase groups transactions by the stable per-statement
account identifier assigned by extraction, not by extracted account number,
holder name, or bank text. Extracted account number, holder name, bank name, and
IFSC remain metadata for display and counterparty clues. They are not used to
silently merge account identities.

This matters because OCR or table extraction can misread account numbers or
holder names. The extraction loop knows which source file/sheet each transaction
came from, so `source_account_id` is safer for grouping.

## Input Contract

The analysis phase accepts either:

- an extraction output folder
- a single final extracted CSV

For an extraction output folder, the expected files are:

```text
clean_transactions.csv
flagged_transactions.csv
duplicate_transactions.csv
metadata.json
extraction_summary_report.json
```

If the duplicate file is called `duplicates.csv`, ingestion can handle that as a
fallback.

The current ingestion code is:

```text
analysis/analysis_engine/ingest.py
```

It reads the files, normalizes columns, labels eligibility, reconciles counts,
and produces a `NormalizedInput` object containing:

- `transactions`
- `input_manifest`
- `metadata`
- `input_contract`

## Latest Real Input Counts

For the validation input:

```text
outputs/extractions/run_20260701_123703
```

the analysis input contract recorded:

| Field | Value |
|---|---:|
| Clean CSV rows | 1306 |
| Flagged CSV rows | 22 |
| Duplicate rows | 29 |
| Metadata clean rows | 1335 |
| Summary report clean rows | 1335 |
| Eligible rows | 1307 |
| Excluded rows | 50 |
| Balance mismatch excluded count | 21 |

Count reconciliation status:

```text
ok_reconciled_by_duplicates
```

Meaning:

```text
clean CSV rows (1306) + duplicate rows (29) = metadata clean_rows (1335)
```

This resolved the earlier confusion where the metadata count included duplicate
rows while the final clean CSV excluded them.

## Eligibility Rules

Rows are split into eligible and excluded rows before detectors run.

Eligible rows:

- clean rows
- flagged rows that are not balance mismatch rows

Excluded rows:

- duplicate bucket rows
- extraction balance mismatch rows
- any rows explicitly marked with an exclusion reason

For the latest validation run:

| Source bucket | Rows |
|---|---:|
| clean | 1306 |
| flagged | 22 |
| duplicate | 29 |

Excluded reason counts:

| Reason | Rows |
|---|---:|
| suspected_duplicate | 29 |
| extraction_error_balance_mismatch | 21 |

## Balance Handling

Balance mismatch is no longer an analysis detector pattern.

Earlier analysis code had a balance validation detector, but it was wrong for
the final architecture because balance validation belongs to extraction, where
each statement is processed in isolation. Running balance checks again after
analysis merging can create false mismatches if rows from different accounts are
compared.

Current behavior:

- balance mismatch rows stay excluded
- analysis reads the count from extraction flags
- analysis writes a data-quality summary line
- balance mismatch never contributes to suspicion score
- balance mismatch never appears in `findings_by_pattern`

Latest validation summary:

```text
21 transactions were excluded from analysis due to balance inconsistencies identified during extraction.
```

## Database Layer

The analysis phase persists normalized data into SQLite:

```text
analysis.db
```

Database code:

```text
analysis/analysis_engine/database.py
```

Important persisted concepts:

- transactions
- accounts
- graph nodes/edges
- findings
- baseline statistics
- possible same-owner records

Important transaction fields include:

- `txn_id`
- `doc_id`
- `account_id`
- `account_id_normalized`
- `source_account_id`
- `date`
- `time`
- `narration`
- `narration_normalized`
- `reference`
- `reference_alt`
- `debit_amount`
- `credit_amount`
- `balance`
- `account_number`
- `account_holder`
- `bank_name`
- `ifsc_code`
- `confidence_score`
- `extraction_tier`
- `flag_reason`
- `duplicate_of_txn_id`
- `source_bucket`
- `eligible_for_detection`
- `confidence_tier`
- `exclusion_reason`
- `source_file`
- `source_row_number`
- `source_page`
- `counterparty_account`
- `counterparty_ifsc`
- `counterparty_name_raw`
- `counterparty_resolution_method`
- `counterparty_resolution_confidence`
- `ledger_pair_id`
- `raw_payload_json`

## Pipeline Order

The orchestration is implemented in:

```text
analysis/analysis_engine/pipeline.py
```

Current execution order:

1. Load and normalize extraction input.
2. Reconcile extraction counts.
3. Persist normalized transactions to SQLite.
4. Compute baseline statistics.
5. Build balance data-quality summary.
6. Resolve counterparties.
7. Run capped optional LLM counterparty assist.
8. Finalize counterparty statistics.
9. Run single-account detectors.
10. Build money-flow graph.
11. Run graph detectors.
12. Run cross-account detectors.
13. Auto-trigger money-trail tracing.
14. Run account ranking.
15. Run LLM anomaly leads.
16. Run ML ensemble anomaly leads.
17. Generate deterministic/LLM narration.
18. Score suspicious accounts.
19. Build excluded-row report.
20. Assemble `AnalysisResult`.
21. Persist findings.
22. Write JSON and text outputs.

## Baseline Statistics

Baseline statistics are computed in:

```text
analysis/analysis_engine/baseline.py
```

The baseline gives detectors runtime thresholds instead of hardcoded dataset
values. The analysis phase uses runtime statistics such as:

- row counts
- account counts
- date range
- positive transaction amount quantiles
- median/P75/P90/P95/P99 values
- per-account transaction counts
- per-account credit/debit/volume totals
- throughput ratios
- unique counterparty counts
- typical transaction gaps
- dense activity thresholds

Latest validation baseline:

| Metric | Value |
|---|---:|
| Total rows stored | 1357 |
| Eligible rows | 1307 |
| Excluded rows | 50 |
| Accounts | 3 |

## Counterparty Resolution

Counterparty resolution is implemented in:

```text
analysis/analysis_engine/counterparties.py
analysis/analysis_engine/llm_resolution.py
```

Purpose:

```text
Decide who a transaction was sent to or received from, when possible.
```

Resolution can come from:

- exact reference / UPI / account match
- amount-date mirror match
- narration similarity match
- optional LLM-inferred match
- unresolved

Each resolved edge stores:

- `counterparty_resolution_method`
- `counterparty_resolution_confidence`

Current confidence policy:

| Method | Confidence |
|---|---:|
| exact reference / UPI / account match | 1.0 |
| amount-date mirror match | 0.85 |
| narration similarity match | 0.65 |
| LLM inferred | 0.40 |
| unresolved | 0.0 |

Strong graph-dependent findings are downgraded to weak if the weakest edge used
by the finding falls below:

```text
strong_counterparty_confidence_min = 0.65
```

Latest validation counterparty metrics:

| Metric | Value |
|---|---:|
| Eligible rows | 1307 |
| Resolved counterparty rows | 543 |
| Resolution rate | 41.5455% |
| exact_reference_or_upi_match | 11 |
| narration_similarity_match | 532 |
| unresolved | 764 |
| ledger_pair_count | 0 |
| LLM calls | 0 |
| LLM status | disabled_no_key |

## Money-Flow Graph

Graph construction is implemented in:

```text
analysis/analysis_engine/graph.py
```

The graph is a directed `networkx.MultiDiGraph`.

Nodes:

- observed source accounts
- resolved counterparties

Edges:

- transaction amount
- transaction direction
- transaction IDs
- dates
- source document
- counterparty method
- counterparty confidence
- edge source

Latest validation graph:

| Metric | Value |
|---|---:|
| Nodes | 449 |
| Edges | 543 |

Pattern 6 stores graph construction as structural evidence. It is present in
output, but it is not counted as a suspicious-account scoring hit.

## Finding Record Shape

All detector results use the `Finding` dataclass in:

```text
analysis/analysis_engine/models.py
```

Every finding contains:

| Field | Meaning |
|---|---|
| `finding_id` | stable hash ID for the finding |
| `pattern_id` | numeric pattern ID |
| `pattern_name` | pattern name from catalog |
| `accounts` | involved account IDs |
| `txn_ids` | involved transaction IDs |
| `explanation` | deterministic explanation |
| `narration` | operator-facing explanation |
| `narration_validation` | whether narration was verified or fallback used |
| `confidence_tier` | extraction/data confidence, usually high or low |
| `evidence_strength` | weak, strong, structural, composite, or lead |
| `detection_method` | deterministic, graph, ml, llm, etc. |
| `details` | pattern-specific evidence and thresholds |

`details.runtime_thresholds` records threshold values used by the detector
where possible.

## Evidence Strength

Evidence strength is separate from confidence tier.

`confidence_tier` means:

```text
How reliable was the source row / extraction data?
```

`evidence_strength` means:

```text
How strong is this behavior as forensic evidence?
```

Current evidence categories:

- `weak`
- `strong`
- `structural`
- `composite`
- `lead`

Strong evidence gets more score breadth. Weak evidence still matters, but it
does not dominate rankings alone.

## Final Pattern Catalog

The current final pattern catalog is defined in:

```text
analysis/analysis_engine/models.py
```

| Pattern | Name | Evidence strength | Base weight | Score role |
|---:|---|---|---:|---|
| 1 | duplicate_verification | weak | 0.6 | scored |
| 2 | failed_reversed_transaction_detection | weak | 0.6 | scored |
| 3 | pass_through_routing_account | weak | 0.9 | scored |
| 4 | fund_pooling_account | weak | 0.8 | scored |
| 5 | structuring_smurfing_detection | strong | 1.8 | scored |
| 6 | money_flow_graph_construction | structural | 0.3 | not scored as suspicion hit |
| 7 | circular_flow_multi_hop_cycle_detection | strong | 1.9 | scored |
| 8 | money_trail_tracing | strong | 2.2 | scored |
| 9 | credit_to_cash_out_chains | weak | 1.0 | scored |
| 10 | cross_statement_links | strong | 1.6 | scored |
| 11 | balance_parking_account | weak | 0.7 | scored |
| 12 | hub_ranking | weak | 0.6 | scored |
| 13 | low_value_account_testing | strong | 1.4 | scored |
| 14 | reversal_clusters | weak | 0.8 | scored |
| 15 | round_value_debit_patterns | weak | 0.7 | scored |
| 16 | shared_upi_identifiers | strong | 1.5 | scored |
| 17 | round_trip_detection | strong | 2.0 | scored |
| 18 | dormant_reactivation | strong | 1.7 | scored |
| 19 | first_contact_large_transfer | strong | 1.5 | scored |
| 21 | suspicious_account_ranking | composite | not applicable | ranking output |
| 22 | llm_investigated_anomalies | lead | not applicable | lead only |
| 23 | ml_ensemble_anomaly_lead | lead | not applicable | lead only |

There is no Pattern 20 in the final catalog.

## Pattern 1: Duplicate Verification

Source:

```text
analysis/analysis_engine/detectors/duplicates.py
```

Purpose:

```text
Verify extraction's duplicate bucket instead of blindly trusting it.
```

It compares transactions using:

- date window
- amount tolerance
- same reference
- narration similarity
- same counterparty same day

The important fix is the 2-of-3 corroboration rule. A pair must satisfy at
least two of:

- same reference
- narration similarity above threshold
- same counterparty same day

Recurring payment exclusion was added so routine monthly/regular payments do
not become duplicate findings.

Duplicate result categories:

- `confirmed_extraction_duplicate`
- `missed_by_extraction`
- `possible_extraction_false_positive`

Latest validation count:

```text
33 findings
```

## Pattern 2: Failed/Reversed Transaction Detection

Source:

```text
analysis/analysis_engine/detectors/reversals.py
```

Purpose:

```text
Find debit transactions followed by near-equal credits that look like reversals.
```

Uses:

- reversal amount tolerance
- reversal narration similarity
- runtime settlement window
- extraction reversal labels where available

Latest validation count:

```text
0 findings
```

## Pattern 3: Pass-Through / Routing Account

Sources:

```text
analysis/analysis_engine/detectors/transit.py
analysis/analysis_engine/detectors/high_throughput.py
```

This merges the old transit and high-throughput concepts.

Purpose:

```text
Find accounts where money flows in and out quickly, or where the account acts
like a routing layer.
```

Signals include:

- high throughput ratio
- low retained balance/volume
- large in-out movement
- high activity compared with baseline
- graph counterparties

Latest validation count:

```text
1 finding
```

## Pattern 4: Fund Pooling Account

Source:

```text
analysis/analysis_engine/detectors/accumulation.py
```

Purpose:

```text
Find accounts receiving money from multiple sources and accumulating/pooling it.
```

Signals include:

- many inbound counterparties
- high credit volume
- retained funds
- runtime high-volume thresholds

Latest validation count:

```text
0 findings
```

## Pattern 5: Structuring / Smurfing

Source:

```text
analysis/analysis_engine/detectors/structuring.py
```

Purpose:

```text
Find repeated smaller transactions that appear deliberately split.
```

Signals include:

- repeated credits/debits
- same-day or dense clusters
- runtime amount thresholds
- minimum cluster size

This is strong evidence because transaction splitting is harder to explain as
ordinary behavior when clustered.

Latest validation count:

```text
5 findings
```

## Pattern 6: Money-Flow Graph Construction

Sources:

```text
analysis/analysis_engine/graph.py
analysis/analysis_engine/pipeline.py
```

Purpose:

```text
Record the fact that the graph was built and preserve its structural summary.
```

This pattern always appears when graph construction succeeds.

It includes:

- node count
- edge count
- edge source counts
- graph transaction IDs

It is structural evidence. It does not directly increase suspicious account
ranking.

Latest validation count:

```text
1 finding
```

## Pattern 7: Circular Flow

Source:

```text
analysis/analysis_engine/detectors/circular.py
```

Purpose:

```text
Find money moving through a cycle across multiple accounts.
```

Signals include:

- directed graph cycles
- maximum cycle length
- edge transaction IDs
- weakest counterparty confidence across used edges

If edge confidence is below threshold, the strong finding is downgraded.

Latest validation count:

```text
0 findings
```

## Pattern 8: Money Trail Tracing

Source:

```text
analysis/analysis_engine/detectors/money_trail.py
```

Purpose:

```text
Trace significant credits into later debits using FIFO allocation.
```

Current triggers:

- auto-trigger for runtime significant credits
- auto-trigger for credits in accounts already hit by strong patterns
- manual credit transaction IDs from UI/CLI

Trace output includes:

- source credit transaction ID
- credited amount
- pre-credit balance
- allocations into later debits
- traced amount
- remaining amount
- trace status
- trigger reason

Latest validation count:

```text
100 findings
```

The cap is controlled by:

```text
money_trail_auto_max_credits = 100
```

## Pattern 9: Credit-To-Cash-Out Chains

Source:

```text
analysis/analysis_engine/detectors/credit_to_cash.py
```

Purpose:

```text
Find large credits followed by cash/ATM-like withdrawals.
```

Signals include:

- large credit compared with runtime amount threshold
- cash-like debit narration
- debit within configured time window

Latest validation count:

```text
0 findings
```

## Pattern 10: Cross-Statement Links

Source:

```text
analysis/analysis_engine/detectors/cross_statement.py
```

Purpose:

```text
Find matched debit-credit links across or within statements.
```

Signals include:

- same date
- matching amount
- different accounts
- reference match or resolved counterparty link
- same-document flag
- counterparty confidence

This pattern can identify internal flow between source accounts.

Latest validation count:

```text
0 findings
```

## Pattern 11: Balance Parking Account

Source:

```text
analysis/analysis_engine/detectors/holding_accounts.py
```

Purpose:

```text
Find accounts where large balances/value are parked for a suspicious duration.
```

This is weak evidence because holding funds can be legitimate without other
signals.

Latest validation count:

```text
0 findings
```

## Pattern 12: Hub Ranking

Source:

```text
analysis/analysis_engine/detectors/hub_ranking.py
```

Purpose:

```text
Find graph hubs with many counterparties or high graph centrality.
```

Signals include:

- in-degree
- out-degree
- total degree
- unique counterparties
- centrality
- volume

This is weak evidence because a hub can be legitimate unless corroborated.

Latest validation count:

```text
0 findings
```

## Pattern 13: Low-Value Account Testing

Source:

```text
analysis/analysis_engine/detectors/low_value_testing.py
```

Purpose:

```text
Detect small test transfers used to probe accounts before larger movement.
```

Signals include:

- low amount ceiling
- repeated low-value pairs
- reciprocal behavior
- graph links

Latest validation count:

```text
0 findings
```

## Pattern 14: Reversal Clusters

Source:

```text
analysis/analysis_engine/detectors/reversal_clusters.py
```

Purpose:

```text
Find repeated clusters of reversal-like transactions.
```

Signals include:

- repeated debit-credit reversal pairs
- account-level clustering
- minimum pair count

Latest validation count:

```text
0 findings
```

## Pattern 15: Round-Value Debit Patterns

Source:

```text
analysis/analysis_engine/detectors/round_value_debits.py
```

Purpose:

```text
Find repeated round-value debits.
```

Signals include:

- debit values divisible by configured round divisor
- minimum repeat count
- account grouping

This is weak evidence because normal payments can also be round numbers.

Latest validation count:

```text
2 findings
```

## Pattern 16: Shared UPI Identifiers

Source:

```text
analysis/analysis_engine/detectors/shared_upi.py
```

Purpose:

```text
Find UPI/VPA identifiers shared across accounts or reused suspiciously.
```

This is strong evidence because shared payment identifiers can connect accounts
that otherwise look separate.

Latest validation count:

```text
0 findings
```

## Pattern 17: Round-Trip Detection

Source:

```text
analysis/analysis_engine/detectors/round_trip.py
```

Purpose:

```text
Find money leaving an account and returning to the origin through one or more hops.
```

Signals include:

- graph path from origin back to origin
- maximum hop count
- date window
- amount tolerance
- weakest edge confidence

If the weakest edge confidence is below threshold, the evidence is downgraded.

Latest validation count:

```text
0 findings
```

## Pattern 18: Dormant Reactivation

Source:

```text
analysis/analysis_engine/detectors/dormant_reactivation.py
```

Purpose:

```text
Find accounts that were quiet/dormant and then suddenly receive large funds
followed by rapid outflow.
```

Signals include:

- dormant gap computed from runtime account gaps
- large credit threshold from transaction quantiles
- outbound movement within configured window
- minimum outflow ratio

Latest validation count:

```text
0 findings
```

## Pattern 19: First-Contact Large Transfer

Source:

```text
analysis/analysis_engine/detectors/first_contact.py
```

Purpose:

```text
Find the first observed transfer between two accounts/counterparties when that
first contact is unusually large.
```

Signals include:

- first observed edge between a pair
- large amount compared with runtime threshold
- graph edge confidence

Latest validation count:

```text
44 findings
```

## Pattern 21: Suspicious Account Ranking

Sources:

```text
analysis/analysis_engine/detectors/suspicious_ranking.py
analysis/analysis_engine/scoring.py
```

Purpose:

```text
Convert detector findings into account-level ranking.
```

Pattern 21 is composite. It is not used as an input to its own score.

It creates findings for top suspicious accounts and writes full account ranking
to:

```text
suspicious_accounts[]
```

Latest validation count:

```text
3 findings
```

## Pattern 22: LLM-Investigated Anomalies

Source:

```text
analysis/analysis_engine/anomaly_investigator.py
```

Purpose:

```text
Generate lead-only LLM investigation notes when deterministic evidence justifies it.
```

It does not contribute to score.

It can trigger from:

- strong deterministic findings
- manually supplied anomaly account IDs

If no LLM key is available, it returns a status/lead record instead of failing
the pipeline.

Latest validation:

```text
1 finding/status row
LLM status: disabled_no_key
```

## Pattern 23: ML Ensemble Anomaly Lead

Source:

```text
analysis/analysis_engine/detectors/ml_ensemble.py
```

Purpose:

```text
Find anomaly leads using account/graph features, but only as lead-only evidence.
```

Models/signals:

- Isolation Forest
- Local Outlier Factor
- robust MAD / HBOS-style score

Rules:

- account must be flagged by at least two independent models
- accounts already covered by strong deterministic findings are skipped
- Pattern 23 never contributes to account score

Latest validation count:

```text
0 findings
```

## Account Scoring

Scoring is implemented in:

```text
analysis/analysis_engine/scoring.py
```

Account score formula:

```text
total_score =
    breadth_component
  + weighted_severity
  + value_component
  + centrality_bonus
```

Breadth formula:

```text
breadth_component =
    strong_pattern_count * 150
  + weak_pattern_count * 40
```

Weighted severity:

```text
base_weight_for_pattern * confidence_multiplier
```

Confidence multiplier:

| Confidence tier | Multiplier |
|---|---:|
| high | 1.0 |
| low | 0.5 |

Patterns excluded from account scoring:

- Pattern 6, because graph construction is structural
- Pattern 21, because it is the ranking result
- Pattern 22, because it is LLM lead-only
- Pattern 23, because it is ML lead-only

The scorer also adds:

- value component from amount/volume/credit/debit fields in finding details
- centrality bonus when graph centrality fields exist

## Latest Validation Ranking

Top accounts in the latest validation output:

| Rank | Account | Total score | Distinct patterns | Strong patterns | Weak patterns | Total findings |
|---:|---|---:|---:|---:|---:|---:|
| 1 | acct_003 | 1119.2876 | 6 | 3 | 3 | 130 |
| 2 | acct_001 | 604.0895 | 4 | 2 | 2 | 39 |
| 3 | acct_002 | 404.9417 | 2 | 2 | 0 | 16 |

For `acct_003`, the contributing pattern breakdown was:

| Pattern | Finding count | Evidence |
|---|---:|---|
| 1_duplicate_verification | 32 | weak |
| 3_pass_through_routing_account | 1 | weak |
| 5_structuring_smurfing_detection | 5 | strong |
| 8_money_trail_tracing | 69 | strong |
| 15_round_value_debit_patterns | 1 | weak |
| 19_first_contact_large_transfer | 22 | strong |

## Narration and LLM Handling

Narration code:

```text
analysis/analysis_engine/narration.py
analysis/analysis_engine/llm_client.py
```

Every finding has deterministic `explanation`.

The `narration` field is the operator-facing version:

- if LLM is unavailable, narration equals deterministic explanation
- if LLM is available, it may rewrite the explanation
- LLM narration is validated before being accepted
- unsupported account/date/large-number claims cause fallback to template

LLM status is recorded in:

```text
run_metadata.llm_status
counterparty_resolution.llm_status
run_metadata.narration_summary
```

Latest validation:

```text
llm_status = disabled_no_key
```

## Output Contract

Output is written by:

```text
analysis/analysis_engine/output.py
```

Primary JSON:

```text
analysis_results.json
```

Human summary:

```text
analysis_summary.txt
```

SQLite database:

```text
analysis.db
```

`analysis_results.json` contains:

- `run_metadata`
- `input_contract`
- `baseline_summary`
- `counterparty_resolution`
- `suspicious_accounts`
- `findings_by_pattern`
- `all_findings`
- `excluded_rows`
- `possible_same_owner`
- `balance_validation`
- `graph_summary`
- `graph`

All pattern keys are always present in `findings_by_pattern`, even when a
pattern has zero findings.

## Latest Validation Pattern Counts

From:

```text
analysis/outputs/run_fix_plan_validation_20260701_r2/analysis_results.json
```

| Pattern key | Findings |
|---|---:|
| 1_duplicate_verification | 33 |
| 2_failed_reversed_transaction_detection | 0 |
| 3_pass_through_routing_account | 1 |
| 4_fund_pooling_account | 0 |
| 5_structuring_smurfing_detection | 5 |
| 6_money_flow_graph_construction | 1 |
| 7_circular_flow_multi_hop_cycle_detection | 0 |
| 8_money_trail_tracing | 100 |
| 9_credit_to_cash_out_chains | 0 |
| 10_cross_statement_links | 0 |
| 11_balance_parking_account | 0 |
| 12_hub_ranking | 0 |
| 13_low_value_account_testing | 0 |
| 14_reversal_clusters | 0 |
| 15_round_value_debit_patterns | 2 |
| 16_shared_upi_identifiers | 0 |
| 17_round_trip_detection | 0 |
| 18_dormant_reactivation | 0 |
| 19_first_contact_large_transfer | 44 |
| 21_suspicious_account_ranking | 3 |
| 22_llm_investigated_anomalies | 1 |
| 23_ml_ensemble_anomaly_lead | 0 |

## Streamlit Interface Changes

Updated file:

```text
app.py
```

The interface now supports:

- normal extraction upload
- automatic analysis after extraction
- analysis-only CSV upload in sidebar
- manual Pattern 22 account IDs
- manual Pattern 8 money-trail credit transaction IDs
- analysis output folder display
- graph visualization with account focus and search
- priority findings
- scored pattern expanders
- raw evidence expanders
- suspicious account table
- LLM and ML lead-only section
- analysis output downloads

Priority finding keys in UI:

```text
17_round_trip_detection
8_money_trail_tracing
15_round_value_debit_patterns
```

## Tests and Verification

Python syntax compile check passed:

```text
python3 -m py_compile app.py $(rg --files analysis/analysis_engine analysis/tests | tr '\n' ' ')
```

Real pipeline validation passed:

```text
input:  outputs/extractions/run_20260701_123703
output: analysis/outputs/run_fix_plan_validation_20260701_r2
```

The output JSON was checked for:

- final pattern keys
- `evidence_strength`
- `narration`
- `narration_validation`
- `llm_status`
- graph edges
- balance validation summary
- `source_account_id` in input manifest/contract

`pytest` did not run because this machine currently lacks pytest:

```text
No module named pytest
```

The synthetic test files were updated to use the final pattern keys, but they
still need to be executed after installing pytest.

## Important Files

Core orchestration:

```text
analysis/analysis_engine/pipeline.py
```

Input normalization:

```text
analysis/analysis_engine/ingest.py
```

Database:

```text
analysis/analysis_engine/database.py
```

Models and pattern catalog:

```text
analysis/analysis_engine/models.py
```

Configuration:

```text
analysis/analysis_engine/config.py
```

Baseline:

```text
analysis/analysis_engine/baseline.py
```

Counterparty resolution:

```text
analysis/analysis_engine/counterparties.py
analysis/analysis_engine/llm_resolution.py
```

Graph:

```text
analysis/analysis_engine/graph.py
```

Scoring:

```text
analysis/analysis_engine/scoring.py
```

Output:

```text
analysis/analysis_engine/output.py
```

Narration:

```text
analysis/analysis_engine/narration.py
analysis/analysis_engine/llm_client.py
```

Streamlit UI:

```text
app.py
```

Pattern documentation:

```text
ANALYSIS_PATTERN_LOGIC_AND_SCORING.md
```

This explanation document:

```text
ANALYSIS_PHASE_EXPLAINATION.md
```

## Current Caveats

1. `pytest` is not installed, so automated test execution is pending.
2. LLM status is currently `disabled_no_key` unless `GROQ_API_KEY` or
   `GROQ_API_KEYS` is configured.
3. Pattern 8 can produce many traces because it auto-triggers and is capped by
   `money_trail_auto_max_credits`.
4. Pattern 22 and Pattern 23 are lead-only and deliberately excluded from score.
5. Pattern 6 is structural and deliberately excluded from score.
6. `account_number` still exists as metadata/display/fallback ingestion support,
   but detectors, graph, and scoring use the stable analysis account ID.
7. Generated validation outputs under `analysis/outputs/` are large and should
   be treated as run artifacts, not source code.

## What Is Ready for the Report Phase

The report phase can now consume:

```text
analysis_results.json
analysis_summary.txt
analysis.db
```

The safest report source is `analysis_results.json`, because it includes:

- complete pattern outputs
- zero-result patterns
- account ranking
- evidence strength
- raw details
- narration
- graph summary and graph data
- excluded row summaries
- balance data-quality summary
- LLM status

The report phase should not recalculate fraud logic. It should present and
explain the analysis outputs.

