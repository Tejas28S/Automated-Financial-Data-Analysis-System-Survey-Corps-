# Analysis Phase Implementation Plan

This plan was frozen after reading all of `ANALYSIS_INSTRUCTIONS.md`, inspecting
the extraction implementation and its latest complete output, and inspecting only
synthetic build cases `01` and `02`. Held-out cases, supplementary case `04`
files, and the ground-truth files remain unread until detector implementation is
complete.

## 1. Inputs and compatibility boundary

The analysis core will consume one normalized transaction contract. A dedicated
ingestion boundary will adapt inputs without leaking file-specific behavior into
detectors:

- Contract layout: `clean_transactions.csv`, `flagged_transactions.csv`, and
  `duplicate_transactions.csv`.
- Current extraction layout: `clean_transactions.csv`,
  `flagged_transactions.csv`, and the audit-summary `duplicates.csv`.
- Supplied synthetic layout: one flat CSV per test case, with extraction labels
  carried by `duplicate_of` and `is_reversed` columns.

Column aliases will be matched case-insensitively by semantic role. Unknown
columns are retained in `raw_payload_json`; missing optional columns are null,
not invented. Required semantic fields are account, date, debit, and credit.
Transaction IDs are preserved when unique and deterministically disambiguated
when repeated. Statement JSON files and metadata are used, when present, to
recover document provenance. No extraction file is modified.

## 2. Package and module layout

```
analysis_engine/
  config.py              configurable universal limits and numerical tolerances
  models.py              Finding and AnalysisResult structures
  ingest.py              schema inspection, aliases, merge, labels, provenance
  database.py            persisted SQLite schema, indexes, and query helpers
  baseline.py            dataset/account statistics and runtime thresholds
  balance.py             balance gatekeeper and validation audit
  counterparties.py      deterministic parser, optional cached LLM fallback
  graph.py               reusable canonical MultiDiGraph construction
  detectors/
    duplicates.py        pattern 1
    reversals.py         pattern 2
    round_trip.py        pattern 4
    transit.py           pattern 5
    accumulation.py      pattern 6
    structuring.py       pattern 7
    circular.py          pattern 9
    money_trail.py       pattern 10 and explicit trace API
  scoring.py             distinct-pattern account ranking and breakdown
  output.py              consolidated structured JSON serialization
  pipeline.py            prescribed orchestration only
  cli.py                 reproducible command-line entry point
tests/
  ...                    standard-library unittest suite
```

Pattern 3 is implemented by `balance.py`; pattern 8 is implemented by
`graph.py`. They are not duplicated under `detectors/` merely for naming
symmetry.

## 3. Prescribed build order

1. **Merge and label**
   - Inspect headers before normalization.
   - Add `source_bucket`, `eligible_for_detection`, `confidence_tier`,
     `exclusion_reason`, source file, source row, document, and raw payload.
   - Exclude every extraction `balance_mismatch` and every duplicate-bucket row.
   - Keep other flagged rows eligible at low confidence with their reasons.
   - Generate a separate audit record for every excluded row.

2. **Persist SQLite**
   - Create `transactions`, `accounts`, `documents`, `baseline_summary`,
     `balance_validation`, `counterparty_cache`, and `possible_same_owner`.
   - Index transactions by account, date, and eligibility as required, plus
     reference fields used to canonicalize mirrored transfers.
   - All later stages read from SQLite only.

3. **Baseline and balance gate**
   - Compute preliminary amount, date, volume, account, and settlement statistics.
   - Walk account ledgers deterministically. Duplicates may maintain balance-chain
     continuity but never become detection-eligible. Pre-excluded balance errors
     break a chain and cannot contaminate the next row.
   - Mark newly discovered eligible-row inconsistencies as excluded and record a
     pattern-3 finding.
   - Recompute the final baseline from the resulting eligible population.
   - Derive every patterns 4–7 threshold here: temporal window, amount-retention
     tolerance, throughput/volume cutoffs, accumulation ratios/connectivity, and
     structuring amount/count/aggregate cutoffs.

4. **Counterparty resolution**
   - Deterministically recognize NEFT, IMPS, RTGS, UPI, ATM, POS, CHQ, CLG, ECS,
     and NACH; parse IFSCs, account-like values, and VPAs.
   - Prefer exact known-account and exact unique-holder evidence, then canonical
     debit/credit ledger-pair evidence, then direction-aware narration tokens.
   - Never resolve ambiguous evidence.
   - Call the optional LLM resolver only for nonempty, nongeneric unresolved text.
     Cache by normalized narration pattern in SQLite before any call.
   - Store the four required columns and report the measured resolution rate.
   - Cross-reference, but never merge, multiple accounts sharing a normalized
     counterparty name.
   - Finalize unique-counterparty account statistics and threshold values before
     any detector executes.

5. **Detectors and graph**
   - Pattern 1 independently groups same-account/same-amount candidates within a
     configurable adjacent-date window and compares normalized narrations and
     references. It reconciles confirmed, missed, and unsupported extraction
     duplicate flags.
   - Pattern 2 detects a debit followed by a near-equal credit within the
     baseline-derived settlement window, using reference/narration evidence and
     never relying on `is_reversed` as detection logic.
   - Pattern 3 emits only unexplained balance-chain failures; account-level
     validation metrics remain retrievable even when no finding fires.
   - Pattern 7 groups same-account/same-direction short-window transactions and
     applies only baseline-derived individual, count, and aggregate cutoffs.
   - Pattern 10 exposes `trace_credit(txn_id)` and traces subsequent eligible
     debits FIFO, preserving allocations and stopping when the credited amount is
     exhausted. Pipeline traces are produced only for explicitly requested credit
     IDs, so ordinary credits do not become suspicion signals.
   - Pattern 8 builds one reusable directed `MultiDiGraph`. Mirrored ledger rows
     become one edge with both transaction IDs; debit rows are canonical. Credit
     rows create edges only when no mirrored debit exists.
   - Patterns 4, 5, 6, and 9 consume that same graph. Cycle algorithms operate on
     observed accounts for bounded, explainable performance; the full graph still
     retains resolved external nodes for visualization.

6. **Confidence and scoring**
   - Every finding contains pattern ID/name, accounts, exact transaction IDs,
     plain-English explanation, details, and lowest contributing confidence.
   - Low-confidence findings list each contributing extraction flag reason.
   - Account score is the count of distinct fired pattern types. Rankings also
     show per-pattern finding counts; repeated flags never inflate the distinct
     pattern score.

7. **Structured output**
   - Return an in-process `AnalysisResult` containing the reusable graph object.
   - Persist one consolidated JSON-safe structure containing the baseline,
     resolution metrics, rankings, all ten pattern keys including explicit empty
     lists, exclusions, same-owner suggestions, findings, and node-link graph.
   - Persist SQLite beside the JSON so every result is traceable to source rows.

## 4. Testing and hold-out discipline

- Build/tune only on case files `01` and `02`, using the expectations frozen in
  `docs/SYNTHETIC_BUILD_EXPECTATIONS.md`.
- Map supplied scenario folders to locked requirements as follows:
  round trip→4, transit/layering→5, accumulation→6, structuring→7,
  duplicates→1 and 2, money trail→10, aggregation→8 edge/generalization checks,
  circular flow→9. Balance consistency is checked across every build case.
- Burst and combined folders are cross-pattern generalization tests; they do not
  create new detector types.
- After all build expectations pass simultaneously, run every case `03` exactly
  once. If a general correction is required, make it using cases `01` and `02`
  only, then permit at most one final held-out rerun.
- Read ground truth only after the first held-out run. Extra case `04` files are
  supplementary one-shot validation and are never tuning inputs.
- Only after synthetic validation is frozen, execute the full pipeline once on
  the 162-file extraction run `run_20260627_152635`.

## 5. Production validation and deliverables

- Run the unittest suite and schema/anti-overfit static checks.
- Run build cases and capture expected-versus-actual evidence.
- Run held-out cases once and record results without concealment.
- Run the full primary extraction output once; print baseline, counterparty
  resolution rate, exclusion counts, graph counts, and top five ranked accounts.
- Re-read `ANALYSIS_INSTRUCTIONS.md`, produce `IMPLEMENTATION_CHECKLIST.md` with
  every requirement, implementation module, evidence, and remaining limitation.
- Confirm extraction source and artifacts are byte-for-byte untouched.

## 6. Known input-contract differences to report, not hide

- The current extraction names its duplicate audit `duplicates.csv` and gives a
  summary schema rather than the baseline transaction schema.
- The synthetic attachment is one flat CSV per case, not three CSVs per case.
- Four scenario folders contain a fourth case despite the specified three-case
  protocol.
- The supplied scenario taxonomy includes burst and aggregation folders but no
  dedicated balance-only folder. Those datasets are used for generalization and
  graph validation without adding out-of-scope fraud patterns.

These are handled at the ingestion/test harness boundary. Detector logic remains
independent of filenames, banks, holders, account numbers, and dataset identity.
