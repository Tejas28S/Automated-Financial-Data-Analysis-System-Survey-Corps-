# Analysis Phase Implementation Plan

This plan is grounded only in the current repository state:

- Real extraction output inspected: `outputs/extractions/run_20260630_143052/`
- Base analysis folder inspected: `analysis phase cidecode hackathon/analysis phase cidecode hackathon/`
- Locked implementation rules inspected: `analysis phase cidecode hackathon/analysis phase cidecode hackathon/ANALYSIS_INSTRUCTIONS.md`

The old System Design / Stage 1 PDF documents are intentionally ignored for this task.

## Step 1 - Exact Input Contract

### Extraction Output Files On Disk

| Role | Actual file path | Rows | Columns | Status for analysis |
|---|---:|---:|---:|---|
| Authoritative eligible transaction set before post-load validation | `outputs/extractions/run_20260630_143052/clean_transactions.csv` | 2085 | 18 | Primary input. These are clean/non-flagged rows from extraction. |
| Supplementary review rows | `outputs/extractions/run_20260630_143052/flagged_transactions.csv` | 24 | 16 | Supplementary. Rows with `flag_reason == balance_mismatch` are excluded; non-balance flags remain eligible with lower confidence per `ANALYSIS_INSTRUCTIONS.md` lines 68-83. |
| Supplementary duplicate review rows | `outputs/extractions/run_20260630_143052/duplicates.csv` | 29 | 9 | Supplementary. Excluded from detection by default. Note actual filename is `duplicates.csv`, not `duplicate_transactions.csv`. |
| Extraction metadata | `outputs/extractions/run_20260630_143052/metadata.json` | n/a | keys | Supplementary run metadata: session, file stats, reconciliation, output file paths, duplicate count. |
| Extraction ledger | `outputs/extractions/run_20260630_143052/extraction_ledger.json` | 6 entries | keys | Supplementary per-source-file extraction receipt. |
| Summary report | `outputs/extractions/run_20260630_143052/extraction_summary_report.json` | n/a | keys | Supplementary totals: 2114 clean in report, 24 flagged, 29 duplicates, flags by reason. |

Important observed count mismatch: the CSV currently contains 2085 clean rows, while `metadata.json` / `extraction_summary_report.json` report 2114 clean rows. The analysis phase must treat the CSV row count as the executable input truth and preserve the metadata count as an audit field.

### Literal Schema Table

| File | Column | dtype sampled by pandas | Analysis mapping / note |
|---|---|---|---|
| `clean_transactions.csv` | `account_number` | `int64` | Map to canonical `account_id` and `account_number`. |
| `clean_transactions.csv` | `account_holder` | `str` | Often blank/NaN in sample; map to `account_holder`. |
| `clean_transactions.csv` | `ifsc_code` | `str` | Map to `ifsc_code`. |
| `clean_transactions.csv` | `Date` | `str` | Map to canonical `date`. |
| `clean_transactions.csv` | `Time` | `str` | Map to canonical `time`. |
| `clean_transactions.csv` | `Narration` | `str` | Map to canonical `narration`. |
| `clean_transactions.csv` | `Transaction_ID` | `float64` | External transaction ID candidate; mostly empty in sample. |
| `clean_transactions.csv` | `Reference_Number` | `float64` | Reference candidate; type varies from synthetic where it is `str`. Must read as string in ingestion. |
| `clean_transactions.csv` | `Transaction_Reference` | `str` | Map to canonical `reference_alt`; sometimes best reference. |
| `clean_transactions.csv` | `Cheque_Number` | `float64` | Preserve in raw payload; not currently canonical. |
| `clean_transactions.csv` | `Debit` | `float64` | Map to `debit_amount`. |
| `clean_transactions.csv` | `Credit` | `float64` | Map to `credit_amount`. |
| `clean_transactions.csv` | `Balance` | `float64` | Map to `balance`. |
| `clean_transactions.csv` | `Transaction_Type` | `str` | Preserve in raw payload; can derive direction. |
| `clean_transactions.csv` | `Bank_Name` | `str` | Map to `bank_name`. |
| `clean_transactions.csv` | `txn_id` | `str` | Canonical transaction ID. |
| `clean_transactions.csv` | `duplicate_of` | `float64` | Map to `duplicate_of_txn_id` if populated. |
| `clean_transactions.csv` | `is_reversed` | `bool` | Preserve as validation label only; detector 2 must not trust it. |
| `flagged_transactions.csv` | `Date` | `str` | Map to canonical `date`. |
| `flagged_transactions.csv` | `Time` | `str` | Map to canonical `time`. |
| `flagged_transactions.csv` | `Narration` | `str` | Map to canonical `narration`. |
| `flagged_transactions.csv` | `Transaction_ID` | `float64` | External transaction ID candidate. |
| `flagged_transactions.csv` | `Reference_Number` | `float64` | Reference candidate. |
| `flagged_transactions.csv` | `Transaction_Reference` | `object` | Map to `reference_alt`; object because long/mixed values appear. |
| `flagged_transactions.csv` | `Cheque_Number` | `float64` | Preserve in raw payload. |
| `flagged_transactions.csv` | `Debit` | `float64` | Map to `debit_amount`. |
| `flagged_transactions.csv` | `Credit` | `float64` | Map to `credit_amount`. |
| `flagged_transactions.csv` | `Balance` | `float64` | Map to `balance`. |
| `flagged_transactions.csv` | `Transaction_Type` | `str` | Preserve in raw payload. |
| `flagged_transactions.csv` | `Account_ID` | `int64` | Map to canonical `account_id` and `account_number`; note title differs from clean file. |
| `flagged_transactions.csv` | `Bank_Name` | `str` | Map to `bank_name`. |
| `flagged_transactions.csv` | `IFSC_Code` | `str` | Map to `ifsc_code`; note case differs from clean file. |
| `flagged_transactions.csv` | `flag_reason` | `str` | Drives eligibility and confidence. |
| `flagged_transactions.csv` | `mismatch_diagnosis` | `str` | Preserve in raw payload and excluded-row details. |
| `duplicates.csv` | `duplicate_row_number` | `int64` | Preserve; used to generate canonical duplicate txn ID if missing. |
| `duplicates.csv` | `original_row_number` | `int64` | Map to `duplicate_of_txn_id` as `source-row::<original_row_number>` if no real txn ID exists. |
| `duplicates.csv` | `account_number` | `int64` | Map to canonical account fields. |
| `duplicates.csv` | `date` | `str` | Map to canonical `date`. |
| `duplicates.csv` | `amount` | `float64` | Preserve; canonical amount still from debit/credit. |
| `duplicates.csv` | `debit` | `float64` | Map to `debit_amount`. |
| `duplicates.csv` | `credit` | `float64` | Map to `credit_amount`. |
| `duplicates.csv` | `narration` | `str` | Map to canonical `narration`. |
| `duplicates.csv` | `reason_flagged` | `str` | Map to `flag_reason`; duplicate rows excluded by source bucket. |

### Five-Row Samples

`clean_transactions.csv` sample:

| row | account_number | Date | Narration | Debit | Credit | Balance | Bank_Name | txn_id |
|---:|---:|---|---|---:|---:|---:|---|---|
| 1 | 763010062770 | 05/04/2017 | OS PAYTM 201704050068 0039490401 PG-0039490401 | 2500.00 | 0.00 | 32092.10 | Unknown Bank | `763010062770_000000` |
| 2 | 763010062770 | 05/04/2017 | ATL/0923572005/622018/+NETAJI 709515003503 SUBHASH PALACEDELHID | 1000.00 | 0.00 | 31092.10 | Unknown Bank | `763010062770_000001` |
| 3 | 763010062770 | 06/04/2017 | OS PAYTM 201704060068 0039557632 PG-0039557632 | 50.00 | 0.00 | 31042.10 | Unknown Bank | `763010062770_000002` |
| 4 | 763010062770 | 07/04/2017 | OS RELIANCEJIO 191625266070875 PG-0039583942 | 303.00 | 0.00 | 30739.10 | Unknown Bank | `763010062770_000003` |
| 5 | 763010062770 | 07/04/2017 | OS RELIANCEJIO 191625266234836 PG-0039585865 | 303.00 | 0.00 | 30436.10 | Unknown Bank | `763010062770_000004` |

`flagged_transactions.csv` sample:

| row | Account_ID | Date | Narration | Debit | Credit | Balance | flag_reason | mismatch_diagnosis |
|---:|---:|---|---|---:|---:|---:|---|---|
| 1 | 763010062770 | 25/05/2017 | ATL/0923572005/800001/+868DR 714511002248 MUKHARJEENAGARDELHID | 500.00 | 0.00 | 17671.00 | `balance_mismatch` | `direction_inverted` |
| 2 | 50100158077633 | 02/06/2018 | Long merged narration containing many transactions and dates | 0.00 | 7633.81 | 10633.81 | `narration_contains_multiple_transactions` | blank |
| 3 | 50100158077633 | 04/10/2018 | Long merged narration ending with STATEMENTSUMMARY | 0.00 | 17254.94 | 7194.94 | `balance_mismatch` | `missing_transaction` |
| 4 | 1357102000000198 | 13/04/2020 | UPI/010408242117/BharatpeMerchant | 72.00 | 0.00 | 2264.70 | `balance_mismatch` | `missing_transaction` |
| 5 | 1357102000000198 | 03/05/2020 | UPI/012411039856/Mr SANTOSH SOPAN SALEKAR | 44.00 | 0.00 | 3943.10 | `balance_mismatch` | `missing_transaction` |

`duplicates.csv` sample:

| row | duplicate_row_number | original_row_number | account_number | date | amount | debit | credit | narration | reason_flagged |
|---:|---:|---:|---:|---|---:|---:|---:|---|---|
| 1 | 1089 | 1087 | 1357102000000198 | 16/01/2021 | 23.60 | 23.60 | 0.00 | nfs/APPA BALWANT CHOWK, PU PUNE CITY MHIN | exact duplicate |
| 2 | 1307 | 1279 | 1357102000000198 | 22/03/2021 | 560000.00 | 0.00 | 560000.00 | COSBR52021032200877085 SHEWANI KRIPALDAS | exact duplicate |
| 3 | 1308 | 1280 | 1357102000000198 | 22/03/2021 | 2.33 | 0.00 | 2.33 | REF\0319\HPCL 0.75% CASHLESS I | exact duplicate |
| 4 | 1309 | 1281 | 1357102000000198 | 22/03/2021 | 120.00 | 120.00 | 0.00 | UPI/108109446318/BharatpeMerchant | exact duplicate |
| 5 | 1310 | 1282 | 1357102000000198 | 22/03/2021 | 1180.00 | 1180.00 | 0.00 | VISA-POS/GIRIJA CATERERS PUNE IND | exact duplicate |

### Schema Inconsistencies To Handle

- Duplicate file name: locked doc says `duplicate_transactions.csv` at `ANALYSIS_INSTRUCTIONS.md` lines 28-37; actual file is `duplicates.csv`.
- Clean uses `account_number`; flagged uses `Account_ID`; duplicates uses `account_number`.
- Clean uses `ifsc_code`; flagged uses `IFSC_Code`; duplicates has no IFSC.
- Clean/flagged financial columns use `Date`, `Debit`, `Credit`, `Balance`; duplicates uses lowercase `date`, `debit`, `credit`.
- Clean rows have `txn_id`; flagged and duplicate rows do not, so ingestion must generate deterministic IDs.
- There is no `doc_id`, `confidence_score`, or `extraction_tier` in the CSVs even though the locked doc lists them as expected baseline columns at lines 39-47.
- `Reference_Number` dtype varies: float in the real clean file, string in synthetic CSVs. Ingestion must force string reading.
- `flagged_transactions.csv` includes one non-balance flag, `narration_contains_multiple_transactions`, which is eligible with lower confidence under the locked rule.
- `metadata.json` summary says 2114 clean rows but `clean_transactions.csv` has 2085 rows. Do not synthesize missing rows; carry the discrepancy into `run_metadata.input_contract_warnings`.

## Step 2 - Base Model Analysis Folder

Base folder: `analysis phase cidecode hackathon/analysis phase cidecode hackathon/`

### Current Input Shape

The synthetic CSVs already use the same broad 18-column shape as `clean_transactions.csv`: `account_number`, `account_holder`, `ifsc_code`, `Date`, `Time`, `Narration`, `Transaction_ID`, `Reference_Number`, `Transaction_Reference`, `Cheque_Number`, `Debit`, `Credit`, `Balance`, `Transaction_Type`, `Bank_Name`, `txn_id`, `duplicate_of`, `is_reversed`.

The base engine also supports a directory input. `analysis_engine/ingest.py` lines 319-326 look for `clean_transactions.csv`, `flagged_transactions.csv`, `duplicate_transactions.csv`, and fall back to `duplicates.csv` if needed. Column aliases are defined in `ingest.py` lines 28-53 and already cover the real title/lowercase variants.

Mismatches vs real Step 1 schema:

- Synthetic cases are usually single flat CSV files; real extraction output is a directory containing clean, flagged, duplicate, metadata, ledger, summary, and statement JSONs.
- Synthetic rows include `txn_id`; real flagged/duplicate rows do not.
- Synthetic duplicate rows live in a flat file with `duplicate_of`; real duplicate evidence is a separate `duplicates.csv` with row numbers, not transaction IDs.
- Real flagged rows have `flag_reason` / `mismatch_diagnosis`; synthetic flat CSVs do not rely on that separate file shape.
- Real output has extraction metadata and statements JSONs; base ingestion reads `metadata.json` and statement JSON doc mapping in `ingest.py` lines 147-171, but does not yet expose all summary discrepancies in final output.
- Real run includes post-load balance problems: the existing smoke test loaded 2138 rows, initially eligible 2087, then balance validation newly excluded 262 more rows. That means real extraction ordering/duplicate continuity must be reviewed before trusting pattern 3 output.

### Existing Modules And Reuse

| File | Current behavior | Reuse decision |
|---|---|---|
| `analysis_engine/ingest.py` | Reads CSV/file or extraction directory, aliases headers, merges buckets, applies source bucket, eligibility, confidence, raw payload, generated txn IDs. | Reuse with small additions: warnings for count mismatch, preserve `mismatch_diagnosis`, force robust duplicate lineage from row numbers. |
| `analysis_engine/database.py` | Creates persisted SQLite schema for `transactions`, `accounts`, `documents`, `baseline_summary`, `balance_validation`, `counterparty_cache`, `possible_same_owner`; inserts normalized rows. | Reuse. Add optional audit warning table only if needed; otherwise keep warnings in JSON metadata. |
| `analysis_engine/baseline.py` | Computes runtime dataset stats and thresholds from eligible rows using quantiles and account aggregates. | Reuse for locked patterns 4-7. Remove/ignore extra-pattern thresholds from output contract. |
| `analysis_engine/balance.py` | Pattern 3 gatekeeper; walks chronological balances, excludes post-load mismatches, preserves duplicate continuity when possible. | Reuse, but review sorting/segment logic because real smoke test newly excluded 262 rows from supposedly clean input. |
| `analysis_engine/counterparties.py` | Deterministic narration parsing, optional Groq fallback, cache, ledger pair matching by reference/amount/date, possible same-owner extraction. | Reuse. Default real verification should run `--no-llm`; LLM fallback remains optional. |
| `analysis_engine/graph.py` | Pattern 8 graph construction as NetworkX `MultiDiGraph`; canonicalizes mirrored ledger pairs. | Reuse. Add graph construction finding/summary for pattern 8 so zero-result representation is not ambiguous. |
| `analysis_engine/models.py` | Defines `Finding`, `AnalysisResult`, and `PATTERN_CATALOG`. Currently includes patterns 1-21. | Must modify: catalog must expose only locked patterns 1-10 for this phase. |
| `analysis_engine/pipeline.py` | Orchestrates ingestion, SQLite, baseline, balance, counterparties, detectors, graph, scoring, output. Currently runs patterns 1-21. | Must modify: run only locked patterns 1-10; remove report/rich-report side effect from CLI path if output contract is analysis-only. |
| `analysis_engine/output.py` | Writes `analysis_results.json`; guarantees every pattern in `PATTERN_CATALOG` appears. | Reuse after catalog is trimmed to 10. |
| `analysis_engine/scoring.py` | Scores accounts by distinct pattern breadth, then total findings. | Reuse after catalog is trimmed; score should use only patterns 1-10. |
| `analysis_engine/cli.py` | Runs pipeline from CLI and writes `analysis_results.json`; also writes/copies rich reports. | Modify to keep analysis phase outputs only, or gate rich report behind a flag. Report rendering is out of scope per `ANALYSIS_INSTRUCTIONS.md` lines 213-215. |
| `analysis_engine/detectors/duplicates.py` | Pattern 1 duplicate cross-check using account, direction, amount bucket, date window, narration/reference/counterparty evidence. | Reuse. Its tolerances are config constants; acceptable as identity tolerances, but record them. |
| `analysis_engine/detectors/reversals.py` | Pattern 2 reversal detection using runtime reversal window plus amount/reference/narration evidence. | Reuse. Uses config amount tolerance and regex evidence; acceptable if recorded. |
| `analysis_engine/detectors/structuring.py` | Pattern 7 same-day clusters using baseline thresholds. | Reuse. |
| `analysis_engine/detectors/round_trip.py` | Pattern 4 time-respecting graph DFS with runtime window/retention. | Reuse. |
| `analysis_engine/detectors/transit.py` | Pattern 5 account throughput using baseline thresholds and graph counterparties. | Reuse. |
| `analysis_engine/detectors/accumulation.py` | Pattern 6 many-source inflow / little outflow using baseline thresholds and graph. | Reuse. |
| `analysis_engine/detectors/circular.py` | Pattern 9 NetworkX cycle detection over observed-account graph. | Reuse, but include runtime graph scope in details. |
| `analysis_engine/detectors/money_trail.py` | Pattern 10 FIFO trace for explicitly provided credit transaction IDs. | Reuse. |
| `analysis_engine/detectors/credit_to_cash.py`, `cross_statement.py`, `high_throughput.py`, `holding_accounts.py`, `hub_ranking.py`, `internal_flow_hub.py`, `low_value_testing.py`, `reversal_clusters.py`, `round_value_debits.py`, `shared_upi.py`, `suspicious_ranking.py` | Extra patterns 11-21. | Do not run or emit in locked analysis phase. Keep files in base folder, but remove from active pipeline/catalog. |
| `tests/test_all_build_cases.py`, `tests/test_heldout_cases.py`, `tests/test_pipeline.py` | Validate synthetic cases but hardcode a Windows project path at lines 8-9. | Modify path discovery before relying on tests on this Mac/repo. |

### Threshold Logic Status

For locked patterns 4-7, threshold logic is already runtime-derived in `baseline.py` lines 107-123:

- reversal window days from typical transaction gap.
- round-trip window and retention from typical gap and amount dispersion.
- transit ratio, volume, and transaction count from account quantiles.
- accumulation credit, outflow ratio, unique counterparty minimum from account stats and finalized counterparty stats.
- structuring amount/count/aggregate thresholds from amount and daily-count quantiles.

Hardcoded or config-fixed values that need treatment:

- Pattern 1 uses `duplicate_date_window_days`, `duplicate_amount_relative_tolerance`, `duplicate_narration_similarity` in `config.py` lines 15-17. These are identity/reconciliation tolerances, not fraud thresholds, but they must appear in `runtime_thresholds`.
- Pattern 2 uses `reversal_amount_relative_tolerance`, `reversal_narration_similarity`, and runtime `reversal_window_days` from `config.py` lines 19-21 and `baseline.py` line 109. Record all.
- Pattern 3 uses `balance_tolerance` from `config.py` line 12. Record it.
- Pattern 8 graph construction uses money epsilon and ledger-pair matching tolerances from config; record graph construction criteria in `graph_summary`.
- Pattern 9 uses `max_cycle_length` from `config.py` line 26. This is a computational safety bound; record it.
- Extra pattern thresholds in `config.py` lines 43-58 are out of scope for the locked 10 and should not appear in final analysis output.

## Step 3 - Exact Output Contract

The analysis phase final outputs should be:

| Output | Format | Purpose |
|---|---|---|
| `<analysis_output_dir>/analysis.db` | SQLite | Queryable persisted transaction, account, document, baseline, validation, counterparty, graph-support data. Already created by `database.py`. |
| `<analysis_output_dir>/analysis_results.json` | JSON | Primary report-phase contract. Must be complete enough for report/UI without rerunning detectors. |
| `<analysis_output_dir>/analysis_summary.txt` | text | Optional human-readable smoke summary. Do not treat as source of truth. |

Do not produce PDF/Excel/rich report in this phase unless explicitly requested later. The locked instructions put report rendering out of scope at lines 213-215.

### `analysis_results.json` Top-Level Shape

```json
{
  "run_metadata": {},
  "input_contract": {},
  "baseline_summary": {},
  "counterparty_resolution": {},
  "graph_summary": {},
  "balance_validation": {},
  "findings_by_pattern": {
    "1_duplicate_detection_cross_check": [],
    "2_failed_reversed_transaction_detection": [],
    "3_balance_consistency_validation": [],
    "4_round_trip_detection": [],
    "5_transit_pass_through_detection": [],
    "6_accumulation_account_detection": [],
    "7_structuring_smurfing_detection": [],
    "8_money_flow_graph_construction": [],
    "9_circular_flow_multi_hop_cycle_detection": [],
    "10_money_trail_tracing": []
  },
  "all_findings": [],
  "suspicious_accounts": [],
  "excluded_rows": {
    "balance_mismatch": [],
    "duplicate": [],
    "other": []
  },
  "possible_same_owner": [],
  "graph": {}
}
```

Zero-result patterns must appear as empty arrays. This is already guaranteed by `output.py` lines 25-30 and `models.py` lines 91-116, once `PATTERN_CATALOG` is reduced to 1-10.

### Common Finding Record Fields

Every detector finding must contain these fields, matching the current `Finding` dataclass in `models.py` lines 53-76 plus required detail keys:

| Field | Type | Required | Meaning |
|---|---|---|---|
| `finding_id` | string | yes | Stable hash/id generated by `Finding.__post_init__`. |
| `pattern_id` | integer | yes | 1-10 only. |
| `pattern_name` | string | yes | Name from locked pattern catalog. |
| `accounts` | array[string] | yes | Observed accounts involved. |
| `txn_ids` | array[string] | yes | Source transaction IDs or generated IDs involved. |
| `explanation` | string | yes | Plain-English deterministic explanation. |
| `confidence_tier` | string | yes | `high` or `low`; inherited from involved rows via `common.py` lines 12-60. |
| `details.runtime_thresholds` | object | required where thresholds apply | Runtime threshold values used. |
| `details.source_documents` | array[object] | yes | Transaction-to-document/page provenance from `common.py` lines 27-35. |
| `details.lower_confidence_flag_reasons` | array[string] | if applicable | Reasons attached when a finding touches eligible flagged rows. |

Pattern-specific required `details` fields:

| Pattern | Required detail fields |
|---|---|
| 1 duplicate cross-check | `reconciliation_category`, `date_difference_days`, `amount_difference_ratio`, `narration_similarity`, `same_reference`, `same_counterparty`, `runtime_thresholds` |
| 2 failed/reversed | `debit_amount`, `credit_amount`, `date_difference_days`, `source_label_cross_check`, `keyword_evidence`, `same_reference`, `same_counterparty`, `narration_similarity`, `runtime_thresholds` |
| 3 balance consistency | `expected_balance`, `actual_balance`, `difference`, `source_document`, `source_page`, `runtime_thresholds` |
| 4 round trip | `path`, `duration_days`, `edge_amounts`, `runtime_window_days`, `runtime_minimum_retention_ratio` |
| 5 transit/pass-through | `total_credit`, `total_debit`, `total_volume`, `throughput_ratio`, `counterparty_count`, `runtime_thresholds` |
| 6 accumulation | `total_credit`, `total_debit`, `outflow_to_inflow_ratio`, `unique_counterparty_count`, `observed_inbound_source_count`, `runtime_thresholds` |
| 7 structuring/smurfing | `direction`, `date`, `transaction_count`, `aggregate_amount`, `individual_amounts`, `runtime_thresholds` |
| 8 graph construction | A synthetic graph summary finding or top-level `graph_summary.pattern_8` with `node_count`, `edge_count`, `observed_account_count`, `resolved_edge_count`, `ledger_pair_count`, `unresolved_eligible_rows` |
| 9 circular flow | `cycle`, `edge_support`, `runtime_thresholds` containing `max_cycle_length` |
| 10 money trail | `source_credit_txn_id`, `credited_amount`, `pre_credit_balance`, `trace_status`, `traced_amount`, `remaining_amount`, `allocations` |

### Account-Level Risk Ranking

Use existing `scoring.py` behavior: rank by `distinct_pattern_count`, then `total_findings`, then `account_id` as deterministic tie-breaker. Each account row contains:

- `account_id`
- `distinct_pattern_count`
- `pattern_breakdown`
- `finding_ids`
- `total_findings`

Only locked patterns 1-10 roll into the score. Extra patterns 11-21 must not influence ranking.

### Excluded / Flagged / Duplicate Rows

The analysis output must summarize excluded rows and preserve traceability:

- `balance_mismatch`: includes source-excluded balance mismatches from extraction plus post-load balance mismatches found by pattern 3.
- `duplicate`: includes rows from actual `duplicates.csv`.
- `other`: includes any other excluded row category.

For each excluded row, keep: `txn_id`, `account_id`, `date`, `amount`, `exclusion_reason`, `source_document`, `source_page`, `source_file`, `source_row_number`, and `flag_reason` when available. Existing pipeline currently includes a subset at `pipeline.py` lines 305-334; expand it to include source file/row/flag reason.

## Step 4 - Working Plan

### A. Ingestion

1. Keep directory input as the primary real-data path:
   - Input: `outputs/extractions/run_20260630_143052/`
   - Existing code: `load_inputs()` in `analysis_engine/ingest.py` lines 290-345.

2. Reuse existing alias mapping:
   - `account_number` / `Account_ID` -> `account_id`
   - `Date` / `date` -> `date`
   - `Debit` / `debit` -> `debit_amount`
   - `Credit` / `credit` -> `credit_amount`
   - `Bank_Name` -> `bank_name`
   - `ifsc_code` / `IFSC_Code` -> `ifsc_code`
   - `duplicate_of` / `original_row_number` -> duplicate lineage

3. Add defensive transformations:
   - Force all CSV columns to string at read time where possible; parse amounts/dates using existing `utils.py` functions after alias mapping.
   - If `flagged_transactions.csv` lacks `txn_id`, generate deterministic IDs as current `_generated_txn_id()` already does at `ingest.py` lines 174-177.
   - If `duplicates.csv` lacks `txn_id`, generate deterministic IDs and set `duplicate_of_txn_id` from `original_row_number`.
   - Preserve `mismatch_diagnosis` in `raw_payload_json` and also expose it in excluded-row details.
   - Read `metadata.json` and `extraction_summary_report.json`; compare reported counts to actual CSV counts and store discrepancies in `run_metadata.input_contract_warnings`.

4. Eligibility after ingestion:
   - `clean_transactions.csv`: `source_bucket = clean`, eligible unless post-load balance validation excludes it.
   - `flagged_transactions.csv` with `flag_reason == balance_mismatch`: excluded.
   - `flagged_transactions.csv` with other flags: eligible with `confidence_tier = low`.
   - `duplicates.csv`: excluded with `exclusion_reason = suspected_duplicate`.

5. Post-load validation:
   - Run `validate_balances()` as current pipeline does at `pipeline.py` lines 111-127.
   - Treat large new exclusions as an audit warning, not a silent success. For the current real run, smoke test found 262 newly excluded post-load mismatches, so implementation must expose that count prominently.

### B. Build / Modify Order

1. `analysis_engine/models.py`
   - Reduce `PATTERN_CATALOG` to IDs 1-10 only.
   - Keep `Finding` and `AnalysisResult` structure.
   - Add `input_contract` field to `AnalysisResult` if we want a first-class top-level schema audit; otherwise include it inside `run_metadata`.

2. `analysis_engine/pipeline.py`
   - Remove imports and execution for detectors 11-21.
   - Keep execution order: ingest -> SQLite -> baseline -> balance -> recompute baseline -> counterparty -> finalize stats -> patterns 1,2,3,7 -> graph/pattern 8 -> patterns 4,5,6,9 -> pattern 10 -> scoring -> output.
   - Add pattern 8 graph construction summary to output so pattern 8 is represented even though it is structural.
   - Ensure scoring receives only findings for pattern keys 1-10.
   - Expand excluded-row entries with `source_file`, `source_row_number`, `flag_reason`, and `mismatch_diagnosis` from raw payload.

3. `analysis_engine/ingest.py`
   - Keep existing directory/file support.
   - Add metadata/summary count reconciliation warnings.
   - Ensure `duplicates.csv` row-number lineage is clear in generated IDs and `duplicate_of_txn_id`.
   - Confirm flagged non-balance rows remain eligible with low confidence.

4. `analysis_engine/baseline.py`
   - Keep runtime thresholds for locked patterns.
   - Remove extra pattern thresholds 11-21 from `baseline_summary.thresholds` or place them behind an inactive/debug namespace not emitted in final output.

5. `analysis_engine/detectors/common.py`
   - Keep confidence/provenance helper.
   - Ensure every finding details object gets `source_documents`.

6. Locked detector files:
   - `detectors/duplicates.py`: add `runtime_thresholds` into details.
   - `detectors/reversals.py`: add `runtime_thresholds` into details.
   - `balance.py`: add `runtime_thresholds.balance_tolerance` into pattern 3 details.
   - `detectors/circular.py`: add `runtime_thresholds.max_cycle_length` and graph scope into details.
   - `detectors/round_trip.py`, `transit.py`, `accumulation.py`, `structuring.py`, `money_trail.py`: reuse, only normalize details where needed.

7. `analysis_engine/output.py`
   - Keep JSON writing.
   - After catalog trim, verify only pattern keys 1-10 are emitted.
   - Add optional `analysis_summary.txt` writer if wanted, but no report rendering.

8. `analysis_engine/scoring.py`
   - Reuse. Confirm pattern breakdown contains no IDs 11-21.

9. `analysis_engine/cli.py`
   - Remove automatic `rich_report` generation/copy, or gate it behind `--write-rich-report`.
   - Keep `--trace-credits` and `--no-llm`.

10. Tests:
    - `tests/test_all_build_cases.py`
    - `tests/test_heldout_cases.py`
    - `tests/test_pipeline.py`
    - Replace hardcoded Windows `PROJECT_DIR` with `Path(__file__).resolve().parents[1]`.
    - Add an integration test using `outputs/extractions/run_20260630_143052/` that only checks ingestion/output shape, not fraud correctness.

### C. Threshold Generalization For Locked Patterns

| Pattern | Existing threshold source | Keep/change |
|---|---|---|
| 1 Duplicate cross-check | Config identity tolerances: date window, amount relative tolerance, narration similarity. | Keep as universal duplicate identity tolerances; record in `runtime_thresholds`. |
| 2 Failed/reversed | Runtime `reversal_window_days` from `typical_transaction_gap_days * reversal_window_multiplier`; config amount/narration tolerances. | Keep; record all. |
| 3 Balance consistency | Config `balance_tolerance = 1.0`. | Keep as universal arithmetic tolerance; record. Review real-run 262 post-load mismatches. |
| 4 Round-trip | Runtime `round_trip_window_days` and `round_trip_min_retention_ratio` from baseline amount dispersion/gaps. | Keep. |
| 5 Transit/pass-through | Runtime throughput ratio, total volume, transaction count quantiles from accounts. | Keep. |
| 6 Accumulation | Runtime credit minimum, outflow-to-inflow max, unique counterparty min after counterparty stats. | Keep. |
| 7 Structuring/smurfing | Runtime individual amount cutoff, daily count cutoff, collective amount cutoff. | Keep. |
| 8 Money flow graph | No fraud threshold; graph edge creation depends on eligible rows, resolved counterparties, ledger-pair criteria. | Keep; emit graph criteria and counts. |
| 9 Circular flow | Config `max_cycle_length` computational bound. | Keep as safety bound; record. |
| 10 Money trail | No suspicion threshold; FIFO traces requested credit IDs until amount is exhausted/statement ends/pre-credit balance reached. | Keep. |

### D. Verification

1. Fix test pathing first. The tests currently hardcode Windows paths in `tests/test_all_build_cases.py` lines 8-9, `tests/test_heldout_cases.py` lines 8-9, and `tests/test_pipeline.py` lines 8-10.

2. Run build-case tests with LLM disabled:
   - `pattern_01_round_trip/rt_case_01.csv`, `rt_case_02.csv`
   - `pattern_02_transit_layering/tl_case_01.csv`, `tl_case_02.csv`
   - `pattern_03_accumulation/ac_case_01.csv`, `ac_case_02.csv`
   - `pattern_04_structuring/st_case_01.csv`, `st_case_02.csv`
   - `pattern_06_duplicates/dup_case_01.csv`, `dup_case_02.csv`
   - `pattern_07_money_trail/mt_case_01.csv`, `mt_case_02.csv`
   - `pattern_08_aggregation/agg_case_01.csv`, `agg_case_02.csv`
   - `pattern_09_circular_flow/cf_case_01.csv`, `cf_case_02.csv`

3. Expected checks come from `docs/SYNTHETIC_BUILD_EXPECTATIONS.md`:
   - duplicates: specific suffixes for `19378026502785` and `9094834319063919`.
   - reversals: specific reversal credit IDs.
   - balance: zero unexplained findings in build cases.
   - round trips: specific account cycles.
   - transit: `721215922125` and `98598169111`.
   - accumulation: `42963560676` and `82127478352`.
   - structuring: required dates/accounts.
   - graph: canonical edge counts for aggregation cases.
   - circular: required 3- and 4-account cycles.
   - money trail: specific 500,000 credit traces.

4. Run held-out case tests once after build cases pass.

5. Only after synthetic passes, run the real extraction directory:
   - Input: `outputs/extractions/run_20260630_143052/`
   - Output: e.g. `outputs/analysis/run_20260630_143052/`
   - LLM disabled for first verification.

6. Real-data acceptance is shape/audit first:
   - `analysis_results.json` exists.
   - `analysis.db` exists.
   - `findings_by_pattern` has exactly 10 keys.
   - excluded counts match source + post-load validation.
   - counterparty resolution rate is printed.
   - post-load balance mismatch count is visible.
   - no pattern key 11-21 appears anywhere in score breakdown.

### E. Final Output Assembly

1. `pipeline.py` collects findings for pattern keys 1-10 only.
2. Pattern 8 creates top-level `graph_summary` and optionally a structural finding under `8_money_flow_graph_construction`.
3. `score_accounts()` ranks accounts from locked-pattern findings only.
4. Excluded rows are assembled from SQLite `eligible_for_detection = 0`.
5. `AnalysisResult` is constructed with:
   - run metadata and input contract warnings
   - baseline summary
   - counterparty metrics
   - suspicious accounts
   - findings by pattern
   - excluded rows
   - possible same-owner appendix
   - graph
   - balance validation
6. `output.py` writes `analysis_results.json`.
7. SQLite stays in `<analysis_output_dir>/analysis.db` for report/UI drill-down.

## Final File-By-File Change Summary

| File | Change |
|---|---|
| `analysis_engine/models.py` | Trim `PATTERN_CATALOG` to locked patterns 1-10; optionally add `input_contract`. |
| `analysis_engine/pipeline.py` | Stop running patterns 11-21; add pattern 8 summary; expand excluded-row details; ensure score uses only 1-10. |
| `analysis_engine/ingest.py` | Add input count/schema warnings; strengthen duplicate lineage and metadata reconciliation; preserve mismatch diagnosis. |
| `analysis_engine/baseline.py` | Emit only locked-pattern thresholds in final summary. |
| `analysis_engine/detectors/duplicates.py` | Add runtime threshold details. |
| `analysis_engine/detectors/reversals.py` | Add runtime threshold details. |
| `analysis_engine/balance.py` | Add runtime balance tolerance in finding details; review real post-load mismatch behavior. |
| `analysis_engine/detectors/circular.py` | Add runtime cycle bound and graph scope details. |
| `analysis_engine/output.py` | Ensure final JSON has exactly locked 10 pattern keys, including zero-result arrays. |
| `analysis_engine/scoring.py` | Reuse; verify no extra-pattern keys contribute. |
| `analysis_engine/cli.py` | Keep analysis outputs only by default; gate or remove rich report generation. |
| `tests/test_all_build_cases.py` | Replace hardcoded Windows path with repo-relative path. |
| `tests/test_heldout_cases.py` | Replace hardcoded Windows path with repo-relative path. |
| `tests/test_pipeline.py` | Replace hardcoded Windows path with repo-relative path. |

