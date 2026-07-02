# Analysis Phase — Final Implementation Plan

Status: READY FOR IMPLEMENTATION (after one final read-through approval)
Scope: Build the real analysis phase in a NEW root-level folder called
`analysis/`. Do not modify anything inside the existing base model folder
(`analysis phase cidecode hackathon/analysis phase cidecode hackathon/`).
This phase must also be wired into the existing `app.py` interface so a
single upload-and-run flow covers extraction AND analysis (Section 9). This
phase also defines two bounded, capped LLM-assist roles (Section 9d) and a
multi-key rotation layer so any Groq/LLM usage never becomes a single point
of failure (Section 9e).

---

## 0. Non-Negotiable Ground Rules

These apply to every section below without exception:

1. **Do not touch the base model folder.** It stays exactly as-is, untouched,
   as a reference implementation that already works on synthetic data. If
   logic needs to be reused from it, **copy** the relevant module into
   `analysis/`, adapt it there, and cite which original file it came from.
   Never edit files inside `analysis phase cidecode hackathon/analysis phase cidecode hackathon/`.

2. **All new work happens in `analysis/`** at the repo root, alongside
   wherever the extraction phase's `outputs/extractions/` folder lives.

3. **No hardcoding.** No bank names, account numbers, fixed amounts, or
   fixed dates anywhere in detector logic. Every threshold is computed at
   runtime from the loaded dataset's own statistics, and the actual computed
   value is recorded in the output (`runtime_thresholds`) so it's auditable.

4. **Build blind.** No ground-truth, hypothesis, or "expected accounts" file
   is given to or used by the implementation. The only inputs are: real
   extraction output, the pattern definitions in this document, and the base
   model folder as a read-only reference. Verification against known
   real-data evidence happens separately, after this phase produces output —
   not during implementation. (This rule governs what *Codex/the build*
   gets to see while implementing — it does not restrict what Section 9d
   Role B's LLM call sees at runtime, which is scoped separately and
   explicitly in Section 9d. Role B reading one account's real transaction
   narrations during a live run is not the same as feeding ground-truth
   hypothesis files into the build process.)

5. **All patterns run, every time, with zero-result patterns shown
   explicitly.** No pattern is silently skipped or omitted from output
   because it found nothing. (Pattern 22 is a partial exception by design —
   see Section 9d Role B: its *key* is always present in output per Ground
   Rule above on output keys, and its trigger evaluation always runs, but
   the LLM investigation step itself only fires when the trigger condition
   is met. "Not triggered" must still be explicit in the output, never a
   silently missing key.)

6. **Synthetic test cases are the pass/fail oracle**, not real data. A
   detector is correct only if it passes its synthetic build cases. Real
   data is used to confirm the pipeline runs end-to-end, not to tune
   thresholds.

7. **Priority patterns get extra build and test rigor.** Money trail
   tracing (10), round-trip detection (4), and round-value debit patterns
   (19) are the most important detectors in this build — they are the
   patterns most directly tied to the problem statement's core ask
   (tracing fund movement and round-tripping). They must each get: a
   dedicated extra synthetic edge-case pass beyond the standard build/
   held-out cases (Section 6), the most detailed `explanation` text of any
   pattern, and first review priority if anything in verification looks
   off. They are not exempt from any rule above — still no hardcoding,
   still build blind — but they get more scrutiny than the other 18.

8. **This plan is a strong starting point, not gospel — Codex should
   improve it where it is actually wrong or weak.** This document was
   written without running the real code, so some specifics here (exact
   file names, exact schema fields, exact build order, exact algorithm
   choice for a given detector) may not survive contact with the real
   codebase. If, while implementing, Codex finds a specific point in this
   plan that is incorrect, suboptimal, or in conflict with what the actual
   code/data shows, it should change the approach — not implement something
   it knows is wrong just to follow this document literally. The conditions
   for doing this:
   - State clearly what was changed and why, with the specific evidence
     from the real codebase or data that justified the change (a file, a
     schema mismatch, a logic error, a better algorithm) — not a vague
     "this seemed better."
   - Do not silently deviate. Every deviation must be visible in Codex's
     response, the same way every kept decision should be.
   - The deviation must still satisfy every rule in 1–7 above — copy-not-
     edit the base model folder, no hardcoding, build blind, all 21 scored
     patterns present with zero-results shown, synthetic tests as the
     pass/fail oracle for patterns 1–21, and extra rigor on the three
     ★ PRIORITY patterns. Those constraints are not up for revision; the
     specific implementation choices underneath them are. The same applies
     to Pattern 22 and the two LLM-assist roles in Section 9d: the *caps*,
     the *separation from scoring*, and the *detection_method tagging* are
     fixed constraints — the specific anomaly-scoring method, pre-filter
     technique, or exact cap numbers are open to improvement with stated
     evidence.
   - If the needed change is large enough that it would significantly
     restructure this plan (e.g. a different file layout, a different
     library, a different detector algorithm family), flag it explicitly
     as "this is a structural deviation from the plan, here is why" rather
     than blending it in quietly, so it gets noticed and reviewed before
     the rest of the build continues on top of it.

---

## 1. Input Contract — What This Phase Receives

The analysis phase reads the output folder of one completed extraction run:

```
outputs/extractions/<run_id>/
├── clean_transactions.csv        <- primary input, eligible transactions
├── flagged_transactions.csv      <- supplementary, conditional eligibility
├── duplicates.csv                <- supplementary, excluded by default
├── metadata.json                 <- run metadata, must match CSV counts
├── extraction_ledger.json        <- per-source-file extraction receipt
└── extraction_summary_report.json
```

### Step 1a — Verify before doing anything else

Before any ingestion code is written, run a count-reconciliation check
against the actual current run folder:

- Row count in `clean_transactions.csv`
- Row count in `metadata.json`
- Row count in `extraction_summary_report.json`

All three must agree. If they do not agree on the real current run, stop
and report the exact mismatch — this is an extraction-phase bug to fix
separately, not something to silently carry forward into the analysis
phase as a warning field.

### Step 1b — Schema normalization

The three CSVs do not share consistent column naming. Ingestion must
normalize all of them into one canonical schema before anything else
happens. Known inconsistencies to handle defensively (verify against the
real files, this list may not be exhaustive):

| Canonical field | clean_transactions.csv | flagged_transactions.csv | duplicates.csv |
|---|---|---|---|
| account_id | `account_number` | `Account_ID` | `account_number` |
| ifsc_code | `ifsc_code` | `IFSC_Code` | (absent) |
| date | `Date` | `Date` | `date` |
| debit_amount | `Debit` | `Debit` | `debit` |
| credit_amount | `Credit` | `Credit` | `credit` |
| balance | `Balance` | `Balance` | (absent) |
| narration | `Narration` | `Narration` | `narration` |
| txn_id | `txn_id` (present) | (absent — generate) | (absent — generate from `duplicate_row_number`) |

`Reference_Number` must be read as string type regardless of how pandas
infers it (it can appear as float in real data due to all-numeric values).

### Step 1c — Eligibility rules

- All rows in `clean_transactions.csv`: eligible, high confidence.
- Rows in `flagged_transactions.csv` with `flag_reason == balance_mismatch`:
  excluded from detection, kept in `excluded_rows` for audit.
- Rows in `flagged_transactions.csv` with any other `flag_reason`
  (e.g. `narration_contains_multiple_transactions`): eligible, but tagged
  `confidence_tier: low` on any finding that touches them.
- All rows in `duplicates.csv`: excluded from detection by default, kept
  in `excluded_rows` for audit and available to the duplicate detector for
  cross-checking.

---

## 2. Output Contract — What This Phase Produces

Output directory: `analysis/outputs/<run_id>/`

```
analysis/outputs/<run_id>/
├── analysis.db              <- SQLite, queryable, full normalized data + findings
├── analysis_results.json    <- primary structured output, full findings
└── analysis_summary.txt     <- human-readable smoke summary (non-authoritative)
```

No PDF, Excel, or rich-text report is produced by this phase. That is a
separate later phase that consumes `analysis_results.json`.

### `analysis.db` is the working data store, not just an output artifact

This needs to be explicit, because it changes how the pipeline should be
built: `analysis.db` is not something assembled at the end purely to hand
back to the user. It is the single source of truth the pipeline itself
reads and writes throughout the run, from right after ingestion onward.

Concretely:

1. The three input CSVs (Section 1) are read and schema-normalized exactly
   once, in `ingest.py`. Immediately after normalization — not at the end
   of the run — the normalized rows are loaded into `analysis.db`.
2. From that point on, **no detector reads the original CSVs again, and no
   detector holds its own separately-filtered copy of the data in memory
   across the run.** Every detector (Section 3) queries `analysis.db`
   directly for whatever subset of transactions/accounts it needs.
3. The money-flow graph (Pattern 8) is also persisted into `analysis.db`
   as it's built — nodes (accounts) and edges (matched flows, tagged
   `edge_source: deterministic | llm_inferred` per Section 9d) — not held
   only in an in-memory graph object that disappears if the run is
   interrupted.
4. Each detector writes its findings into `analysis.db` as it produces
   them, not just into an in-memory list collected at the very end.
   `analysis_results.json` is then generated *from* the database once all
   22 patterns have run — the JSON is a structured export of what's in
   `analysis.db`, not a parallel structure built independently alongside it.

The reasoning: most of the 21 detectors repeatedly need the same kinds of
lookups — "all transactions for account X ordered by date," "does an edge
exist from B back to A," "which accounts appear in both pattern 4 and
pattern 10's findings." These are simple indexed SQL queries against
`analysis.db`, versus re-filtering a CSV/dataframe independently inside
each of 21 separate detector modules, which is both slower and risks one
detector's filter logic quietly diverging from another's. Building
`analysis.db` once, early, and treating it as the pipeline's actual working
memory avoids that — and means the "output" database isn't extra work
bolted on at the end, it's the same database the whole run already used.

### `analysis_results.json` shape

```json
{
  "run_metadata": {
    "run_id": "",
    "analysis_timestamp": "",
    "input_extraction_run_id": "",
    "input_contract_warnings": []
  },
  "input_contract": {
    "clean_row_count": 0,
    "flagged_row_count": 0,
    "duplicate_row_count": 0,
    "count_reconciliation_status": "ok | mismatch",
    "count_reconciliation_detail": ""
  },
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
    "10_money_trail_tracing": [],
    "11_credit_to_cash_out_chains": [],
    "12_cross_statement_links": [],
    "13_high_throughput_accounts": [],
    "14_holding_accounts": [],
    "15_hub_ranking": [],
    "16_internal_flow_hub": [],
    "17_low_value_account_testing": [],
    "18_reversal_clusters": [],
    "19_round_value_debit_patterns": [],
    "20_shared_upi_identifiers": [],
    "21_suspicious_account_ranking": [],
    "22_llm_investigated_anomalies": []
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

All 22 keys are always present. Empty array means the pattern ran and found
nothing — this must be distinguishable in code from "did not run." Pattern
22 has a different "did not run" meaning than 1–21: it is **available but
not triggered** when no account meets the trigger condition in Section 9d,
versus **ran with zero findings** when it was triggered but the LLM found
nothing beyond what the 21 deterministic patterns already caught. Both
states must be distinguishable in the output — see `details.trigger_status`
in Section 9d.

### Common required fields on every finding

| Field | Required | Meaning |
|---|---|---|
| `finding_id` | yes | Stable generated ID |
| `pattern_id` | yes | 1–22 |
| `pattern_name` | yes | From the pattern catalog |
| `accounts` | yes | Observed accounts involved |
| `txn_ids` | yes | Source or generated transaction IDs involved |
| `explanation` | yes | Plain-English deterministic explanation, no jargon |
| `confidence_tier` | yes | `high` or `low` |
| `detection_method` | yes | `deterministic` \| `llm_assisted_resolution` \| `llm_investigated_anomaly` — see Section 9d |
| `details.runtime_thresholds` | where applicable | Actual computed threshold values used |
| `details.source_documents` | yes | Transaction-to-source-file/page provenance |

---

## 3. Pattern Catalog — All 21, What Each One Actually Measures

This is the full set. None are removed, none are added beyond this list.

| # | Pattern | What it measures | Threshold derived from (runtime) |
|---|---|---|---|
| 1 | Duplicate cross-check | Same account/date/amount/narration appearing more than once, beyond what extraction already caught | Date-window, amount-tolerance, narration-similarity — identity tolerances, recorded not hardcoded |
| 2 | Failed/reversed transactions | A debit followed by a matching reversal credit | Reversal window from typical inter-transaction gap; amount/narration tolerance |
| 3 | Balance consistency validation | Row-to-row balance arithmetic doesn't chain correctly | Tolerance = max(rounding quantum inferred from data, tiny fraction of median transaction) |
| **4 ★ PRIORITY** | **Round-trip detection** | Money leaves an account and returns via a short path | Time window from median inter-transaction gap; retention ratio from amount-dispersion stats |
| 5 | Transit/pass-through | Account has near-equal high-volume in/out flow | Throughput ratio, volume, txn-count — all from account-population quantiles |
| 6 | Accumulation accounts | Many sources feed in, little flows out | Inbound-counterparty count, outflow/inflow ratio — from peer quantiles |
| 7 | Structuring/smurfing | Repeated sub-threshold amounts clustered in time | Individual amount cutoff, daily count, aggregate — from amount/activity distribution |
| 8 | Money flow graph construction | Builds the cross-account directed graph all graph detectors depend on | No fraud threshold; edge-confidence tiers from match-quality (exact ref > amount/date mirror > narration-only > llm_inferred, see Section 9d Role A) |
| 9 | Circular flow / multi-hop cycle | 3+ account cycles in the flow graph | Run SCC-scoping first, then temporal-respecting cycle enumeration inside SCCs; max_cycle_length as computational safety bound |
| **10 ★ PRIORITY** | **Money trail tracing** | FIFO trace of where a specific credit's funds went | Not a suspicion threshold — deterministic allocation until exhausted/statement ends |
| 11 | Credit-to-cash-out chains | Large credit followed by clustered ATM/cash withdrawals | Large-credit cutoff from account's own credit distribution percentile; window from median gap |
| 12 | Cross-statement links | Same reference/amount appears as debit in one account's statement and credit in another's | Reference-match confidence threshold from match-score separation in the dataset |
| 13 | High-throughput accounts | Very high transaction count/volume relative to peers | Volume/count percentile of account population |
| 14 | Holding accounts | Net retains substantial value without transit-like churn | Net-retention threshold from peer distribution |
| 15 | Hub ranking | Accounts central to the matched-flow graph (high degree/betweenness) | Graph centrality percentile |
| 16 | Internal flow hub | Accounts with many verified matched flows to/from other accounts in-dataset | Matched-flow count percentile |
| 17 | Low-value account-testing links | Small reciprocal transfers suggesting account verification/testing rather than material movement | Amount ceiling from low-percentile of transaction amounts |
| 18 | Reversal clusters | Multiple reversal-pattern transactions concentrated on one account | Reversal-count percentile per account |
| **19 ★ PRIORITY** | **Round-value debit patterns** | Repeated exact/round amounts suggesting structured payouts | Round-number detection + repetition-count percentile |
| 20 | Shared UPI/VPA identifiers | Same UPI handle appears across multiple distinct accounts | Cross-account appearance count ≥ 2, no percentile needed — structural, not statistical |
| 21 | Suspicious account ranking | Composite ranking combining findings across patterns 1–20 | See scoring model, Section 5 |
| 22 | LLM-investigated anomalies | Free-form investigative read of accounts that are statistically anomalous but matched zero of patterns 1–20 — catches pattern shapes not covered by any named detector | Not threshold-based in the usual sense; trigger condition and hard cap defined in Section 9d Role B. Never contributes to the Pattern 21 score. |

★ PRIORITY patterns (4, 10, 19) get the extra rigor described in Ground
Rule 7: dedicated extra synthetic edge cases, the most detailed
explanations, and first review priority during verification.

Patterns 11–21 are not optional extras — they are part of this build and
must run with the same rigor (runtime thresholds, synthetic test coverage,
explicit zero-result reporting) as patterns 1–10.

Pattern 22 is categorically different from 1–21 and must never be presented
or scored as if it were a 22nd equally-weighted detector — see Section 9d
for why and how it stays separated.

---

## 4. Build Order

Dependency-driven, not numeric:

1. **Ingestion + schema normalization** (Section 1) — nothing else can start
   without this.
2. **Pattern 3 — Balance consistency.** Gates everything downstream; rows
   that fail this are excluded from all other detectors.
3. **Pattern 1 — Duplicate cross-check.** Prevents double-counted flows
   from corrupting later volume-based detectors.
4. **Pattern 2 — Failed/reversed.** Removes non-final movements before flow
   interpretation.
5. **Pattern 8 — Money flow graph construction.** Hard dependency for every
   detector below.
6. **Patterns 5, 6, 13, 14 — single/peer-account statistical detectors**
   (transit, accumulation, high-throughput, holding). These only need the
   graph for counterparty counts, not cycle logic.
7. **Pattern 7, 19 — structuring and round-value patterns.** Per-account,
   amount/time clustering.
8. **Pattern 4, 9 — round-trip and circular flow.** Need the full graph and
   temporal-respecting cycle logic.
9. **Pattern 11, 12, 16 — cash-out chains, cross-statement links, internal
   flow hub.** Need resolved graph edges with confidence tiers.
10. **Pattern 15, 17, 18, 20 — hub ranking, low-value testing, reversal
    clusters, shared UPI.** Derived from graph + per-account stats already
    computed above.
11. **Pattern 10 — money trail tracing.** Runs on-demand against specific
    credit transaction IDs (typically the highest-ranked suspicious credits
    from earlier patterns), not a blanket sweep.
12. **Pattern 21 — suspicious account ranking.** Composite, runs last among
    the scored patterns, depends on all findings from 1–20.
13. **Pattern 22 — LLM-investigated anomalies.** Runs after Pattern 21, using
    Pattern 21's own anomaly/score data to compute the trigger set (Section
    9d Role B). Capped, optional-per-run, never feeds back into Pattern 21's
    score.
14. **Output assembly** — merge everything into `analysis_results.json` and
    `analysis.db`.

---

## 5. Scoring Model (Pattern 21)

Account-level suspicion score combines, from first principles — not tuned
toward any expected account:

- **Pattern breadth**: number of distinct patterns (1–20) the account
  appears in. An account flagged by 4 different pattern types is more
  suspicious than one flagged 4 times by the same pattern.
- **Confidence-weighted severity**: each finding contributes
  `base_weight(pattern) × confidence_multiplier(high=1.0, low=0.5)`.
- **Value-weighted**: findings involving larger transaction volumes
  contribute more than findings on negligible amounts — scaled by the
  account's own transaction-value distribution, not an absolute rupee
  figure, so this generalizes across datasets of different scale.
- **Graph centrality bonus**: accounts central to the matched-flow graph
  (high degree or betweenness, from Pattern 15) get a modest additive
  bonus, since centrality is independent corroboration beyond any single
  pattern.

All weights (`base_weight` per pattern, the confidence multiplier, the
centrality bonus factor) are named constants recorded in
`details.runtime_thresholds` of the Pattern 21 finding — auditable, not
silently baked into a black-box formula. They may be fixed engineering
constants (not data-derived) since they represent a scoring *policy*, but
the policy values themselves must be visible in the output.

The final ranked list in `suspicious_accounts` shows: account_id, total
score, score breakdown by pattern, and distinct pattern count.

Pattern 22 (LLM-investigated anomalies) is explicitly excluded from this
score. It is computed from the score, not an input to it — see Section 9d
Role B. Mixing it into the composite would let an unverified, free-form LLM
read silently inflate an account's ranking, which breaks the
court-defensibility of Pattern 21.

---

## 6. Verification Sequence

0. **Available test data — use what already exists, no need to generate
   fresh data for testing.** Two real sources are already on disk and
   usable for this phase's own testing, without waiting on a live
   extraction run or building new fixtures:
   - **Prior extraction session outputs.** Earlier sessions already
     produced complete `outputs/extractions/<run_id>/` folders (the ones
     referenced throughout this document, e.g. the runs whose
     `clean_transactions.csv` / `metadata.json` were inspected while
     writing this plan). These are real, already-on-disk extraction
     outputs and can be used directly as input for analysis-phase pipeline
     and shape/audit testing — no need to re-run extraction first just to
     get a test input folder.
   - **Synthetic CSVs already in the base model folder.** The base model
     folder (Ground Rule 1 — read-only, never modified) already contains
     its own synthetic test datasets used to validate the original 10/21
     detectors there. These synthetic CSVs should be used as-is for this
     phase's own testing too — copy them into `analysis/synthetic_cases/`
     per the file plan in Section 7, do not regenerate synthetic data from
     scratch unless a specific pattern genuinely has no existing coverage
     there. This avoids duplicating effort that's already done and gives
     the new build a known-answer source from day one.
   Both of these are testing inputs only — neither is the "ground truth"
   referenced in Ground Rule 4 (build blind). Using existing extraction
   output or existing synthetic CSVs to confirm the pipeline runs and
   produces sane shapes is not the same as using the hypothesis/ground-
   truth files, which remain off-limits to the implementation per Ground
   Rule 4.
1. Fix any non-portable file paths in the copied test files (e.g.
   hardcoded OS-specific paths) before relying on the test suite.
2. Run synthetic build-case tests for every pattern that has them. A
   detector is not considered done until it passes its synthetic known-
   answer cases.
3. For the three ★ PRIORITY patterns (4 round-trip, 10 money trail, 19
   round-value debits), go beyond the standard build cases: construct at
   least one additional adversarial synthetic case per pattern that
   specifically tries to break it — e.g. for round-trip, a cycle that
   almost-but-not-quite closes (should NOT fire) versus one that closes
   with a long delay (should still fire if within the runtime window);
   for money trail, a credit that is partially consumed and partially
   carries to a new statement page; for round-value debits, round amounts
   that are coincidental versus genuinely repeated. These three patterns
   should not move to the real-data run until these extra cases pass.
4. Run held-out synthetic cases once, after build cases pass, as the final
   synthetic check.
5. Run the full pipeline against the real current extraction output folder.
   This run is a shape/audit check only:
   - `analysis_results.json` exists and is valid JSON.
   - `analysis.db` exists and is queryable.
   - `findings_by_pattern` has exactly 22 keys, all present (21 scored
     patterns + Pattern 22, which is present even when `not_triggered`).
   - `input_contract.count_reconciliation_status` is `ok`.
   - Counterparty resolution rate is visible in output.
   - No detector throws an unhandled exception.
   - The three ★ PRIORITY patterns get manually reviewed first, before the
     other 18 — read every finding they produced and confirm the
     `explanation` text actually makes sense against the source rows cited.
6. Do not adjust any threshold based on what the real run finds. If real-run
   results look wrong, the fix is in detector logic or ingestion, never in
   nudging a threshold to produce a different-looking output.
7. Verify the LLM-touching paths separately, since they have failure modes
   the deterministic detectors don't:
   - Force a narration fallback test (9c): simulate Groq being unavailable
     and confirm `explanation` still populates via template, and
     `explanation_source: "template"` is recorded correctly.
   - Confirm Role A (9d) respects its cap: with a small synthetic batch of
     unresolved narrations larger than the cap, verify only the capped
     number of LLM calls are made and the skipped count is recorded.
   - Confirm Role B (9d) trigger logic on a synthetic case built
     specifically to have one account with a high anomaly score and zero
     pattern matches, and one account with a high anomaly score that *does*
     match a pattern — only the first should enter the Pattern 22 trigger
     set.
   - Confirm key rotation (9e): simulate one key returning a rate-limit
     error and verify the wrapper switches to the next key and the call
     still succeeds, with the rotation event logged. Simulate all keys
     failing and verify the pipeline degrades gracefully per each module's
     defined fallback rather than crashing.

---

## 7. Final File-By-File Plan (inside `analysis/`, new folder)

```
analysis/
├── analysis_engine/
│   ├── __init__.py
│   ├── ingest.py          <- Section 1: load + normalize extraction output
│   ├── database.py        <- SQLite schema + persistence
│   ├── baseline.py        <- runtime dataset statistics for thresholds
│   ├── balance.py         <- Pattern 3
│   ├── counterparties.py  <- narration parsing, reference extraction, resolution
│   ├── graph.py           <- Pattern 8, the shared directed graph
│   ├── models.py          <- Finding, AnalysisResult, full 22-pattern catalog (21 scored + Pattern 22)
│   ├── pipeline.py        <- orchestration, build order from Section 4
│   ├── scoring.py         <- Pattern 21
│   ├── llm_client.py       <- Section 9e: multi-key rotation wrapper, used by narration.py, llm_resolution.py, anomaly_investigator.py
│   ├── llm_resolution.py   <- Section 9d Role A: candidate edge inference for counterparties.py / graph.py
│   ├── anomaly_investigator.py <- Section 9d Role B: Pattern 22, capped trigger + investigation
│   ├── narration.py       <- Section 9c: Groq calls + template fallback for explanations
│   ├── output.py          <- writes analysis_results.json, guarantees all 22 keys
│   ├── cli.py             <- entrypoint, callable standalone or from app.py
│   └── detectors/
│       ├── duplicates.py
│       ├── reversals.py
│       ├── round_trip.py          <- ★ PRIORITY
│       ├── transit.py
│       ├── accumulation.py
│       ├── structuring.py
│       ├── circular.py
│       ├── money_trail.py         <- ★ PRIORITY
│       ├── credit_to_cash.py
│       ├── cross_statement.py
│       ├── high_throughput.py
│       ├── holding_accounts.py
│       ├── hub_ranking.py
│       ├── internal_flow_hub.py
│       ├── low_value_testing.py
│       ├── reversal_clusters.py
│       ├── round_value_debits.py  <- ★ PRIORITY
│       └── shared_upi.py
├── tests/
│   ├── test_all_build_cases.py
│   ├── test_heldout_cases.py
│   ├── test_pipeline.py
│   ├── test_priority_pattern_edge_cases.py   <- Section 6.3, adversarial cases
│   └── test_llm_assist_and_rotation.py        <- Section 6.7, Role A/B caps + key rotation
├── synthetic_cases/        <- copied from base model folder, adapted as needed
└── outputs/
    └── <run_id>/           <- analysis_results.json, analysis.db, analysis_summary.txt

app.py                       <- MODIFIED: Section 9, calls analysis pipeline after extraction
```

Every module above that has an equivalent in the base model folder should
be **copied in** (not symlinked, not imported cross-folder) and adapted to
the real schema from Section 1. Cite the source file from the base model
folder in a comment at the top of each copied file for traceability. The
base model folder itself is never modified.

---

## 9. Interface Integration — One Upload Flow For Extraction + Analysis

The existing root-level interface (`app.py`) currently only runs the
extraction phase. It must be updated so a single file upload triggers
extraction, then automatically runs this analysis phase on the extraction
output, in one flow — the user should not have to manually invoke a
separate script.

### 9a. Flow change in `app.py`

- Keep the existing upload → extraction call exactly as it works today. Do
  not change extraction behavior.
- Immediately after extraction completes successfully and writes its output
  folder, call the new `analysis/analysis_engine/pipeline.py` entrypoint
  against that same run's output folder, using the run_id extraction just
  produced — no manual path entry by the user.
- If extraction fails or produces a count-reconciliation mismatch
  (Section 1a), do not proceed to analysis automatically — surface the
  extraction problem to the user first.
- Add a `analysis/` import path / module reference into `app.py` the same
  way the extraction module is already referenced. Do not duplicate
  analysis logic inside `app.py` itself — `app.py` only orchestrates the
  call, all real logic stays inside `analysis/analysis_engine/`.

### 9b. What the interface displays after analysis completes

Keep this strictly to real findings — no filler text, no generic
boilerplate, no "this system uses advanced AI" type language anywhere in
the UI output. The interface should show, per run:

- A short run summary: total transactions analyzed, accounts involved,
  date range, counterparty resolution rate.
- For each of the 21 scored patterns: how many findings (including an
  explicit "0 findings" state, never hidden), and the findings themselves —
  account(s) involved, transaction IDs, and the explanation text.
- The ★ PRIORITY patterns (round-trip, money trail, round-value debits)
  should be visually surfaced first/most prominently in the interface,
  ahead of the other 18, since they are the most important findings for
  the investigator reading this.
- The ranked suspicious accounts list from Pattern 21, with score
  breakdown.
- Pattern 22 (LLM-investigated anomalies) shown in a visually distinct
  section labeled clearly as "Additional leads — AI-assisted, not part of
  the scored ranking" — never mixed into the 21-pattern list or the
  suspicious-accounts ranking. If it did not trigger this run, show that
  explicitly (`trigger_status: not_triggered`), not as an absence.
- Nothing should be shown that isn't directly backed by a finding record
  already in `analysis_results.json`. The interface is a renderer of real
  output, not a place that generates additional text on its own.

### 9c. Groq LLM — Role C: narrating findings (one of three scoped roles)

Groq is the single LLM provider used across this entire pipeline, in three
separate, clearly-bounded roles: this one (narration), and the two
described in Section 9d (Role A — counterparty resolution assist, Role B —
the "nitro boost" anomaly investigation). This section covers only the
narration role. Read this alongside 9d, not as the sole use of Groq in the
system — the "hard boundaries" below apply specifically to *this* role's
job (writing explanations), not to what Groq is allowed to do elsewhere in
the pipeline.

Use Groq here for exactly one purpose: turning a finding's structured
fields (`accounts`, `txn_ids`, `details`, the runtime thresholds used) into
a clear, plain-English paragraph for the `explanation` field and/or a short
narrative summary shown in the interface. This is a writing/explanation
step, not a detection step.

Hard boundaries on this role specifically:

- Groq never decides whether something is suspicious *in this role*. All
  detection, scoring, and thresholding for patterns 1–21 happens in
  deterministic Python code per Sections 3–5. In this role, Groq only
  receives an already-finalized finding and writes a clear sentence or
  short paragraph describing what was found, in plain English a
  non-technical investigator can read.
- Groq's prompt in this role must be constrained to: "Given these
  structured fields, write a factual, plain-English explanation of what
  this finding shows. Do not add speculation, do not add severity language
  not already implied by the data, do not invent any account, amount, or
  date not present in the input fields." Every narration prompt should
  include only the fields of that one finding — never the whole dataset,
  never other accounts' data, to keep narration scoped and prevent
  hallucinated cross-references.
- If Groq is unavailable, slow, or returns something that doesn't parse, the
  pipeline must fall back to a deterministic template-based explanation
  (already required as the baseline `explanation` field per Section 2) —
  the analysis output is never blocked on Groq being reachable. Build the
  deterministic explanation first as the real baseline; Groq only upgrades
  the wording on top of it.
- Log which explanations were Groq-generated versus template-generated, so
  this is auditable — this matters for a forensic tool being explained to
  judges. Store this as `details.explanation_source: "groq" | "template"`
  on each finding.
- This narration role is scoped to writing explanations only — it does not
  read raw transaction narrations across an account or propose graph
  edges. Those are Role A and Role B's jobs, defined in 9d, with their own
  separate boundaries and caps. Do not blend narration-role calls with
  Role A/B calls in the same prompt or function.

### 9d. Two more Groq roles, beyond narration — Role A and Role B

Role C (9c, narration) is the only *unconditional* Groq usage in this
pipeline — it runs on every finding. Role A and Role B below are different
in kind: both are **capped, conditionally-triggered, and never decide
suspicion on their own**. All three roles use the same Groq provider
through the same `llm_client.py` wrapper (Section 9e) — they are not
different LLMs, just different, separately-bounded jobs for it. They exist
because some problems in this dataset are structurally hard for
deterministic code (free-text entity matching, recognizing a pattern shape
nobody coded a detector for) — not because LLM judgment is preferred over
deterministic logic anywhere it can do the job.

All three roles must tag every output they produce with `detection_method`
(Section 2 common fields) so a reader of `analysis_results.json` can always
tell deterministic findings apart from LLM-touched ones at a glance.

#### Role A — LLM-assisted counterparty resolution (`llm_resolution.py`)

**Problem this solves:** Patterns 4, 8, 9, 11, 12, 15, 16 all depend on the
matched-flow graph (Pattern 8), which depends on resolving counterparties
from narration text. Deterministic regex/reference matching
(`counterparties.py`) will not catch every real-world narration format
across 17+ banks. Rows it cannot resolve leave the graph artificially
sparse, which silently weakens every graph-dependent detector even though
their threshold logic is correct.

**Scope — strictly bounded, never runs on the full dataset:**

1. Run deterministic resolution in `counterparties.py` first, exactly as
   already specified. This is the default and primary path for all rows.
2. Only rows where deterministic resolution finds **nothing** (no
   reference number, no account-number pattern, no UPI handle) become
   candidates for Role A.
3. Before any LLM call, apply a cheap deterministic pre-filter to that
   unresolved subset: fuzzy name-fragment overlap, partial account-number
   overlap, shared narration substrings. This produces a short candidate
   list of *plausible* pairs/clusters — not every unresolved row gets an
   LLM call, only ones with some pre-existing signal worth disambiguating.
4. For each candidate pair/cluster, call the LLM with **only that one
   pair's evidence** — e.g. one UPI handle plus its narration context,
   compared against one account holder name plus its narration context.
   Never pass the whole dataset or other accounts' data in this prompt.
   Ask it to judge entity-match likelihood and state its reasoning.
5. Any edge the LLM proposes goes into the graph tagged
   `edge_source: "llm_inferred"`, separate from `edge_source:
   "deterministic"`, with the LLM's stated reasoning attached as
   `details.llm_reasoning` on the resulting graph edge. Graph-dependent
   detectors must be able to report findings built on deterministic-only
   edges (high confidence) separately from findings that required an
   `llm_inferred` edge (`confidence_tier: low`, regardless of what the
   detector's own threshold logic would otherwise assign).
6. Hard cap: no more than a fixed number of LLM calls per run for this role
   (recommend starting at 500–1000 candidate pairs; tune only via testing,
   never by watching real-data output). If the candidate list exceeds the
   cap, take the highest-pre-filter-similarity candidates first and record
   how many candidates were skipped due to the cap in
   `counterparty_resolution.llm_assist_summary`.

This role never creates a finding by itself — it only creates graph edges
that the existing 21 deterministic detectors then evaluate with their own
unchanged threshold logic.

#### Role B — LLM-investigated anomalies (`anomaly_investigator.py`, Pattern 22)

**Problem this solves:** No deterministic detector can catch a fraud
pattern nobody coded a rule for. This is structurally unavoidable for any
rule-based system. Role B is the one place in this pipeline an LLM reads
raw transaction narrations and reasons freely — justified only because, by
construction, no deterministic detector has an answer for these specific
accounts.

**Trigger condition — must be computable, not a vague "looks suspicious":**

1. Compute an account-level anomaly score using stats already produced by
   `baseline.py` (throughput ratio, transaction velocity, counterparty
   diversity, average amount relative to peers, balance volatility),
   combined via a simple method (e.g. isolation forest or summed
   normalized z-scores — pick one, document the choice, do not hand-tune
   it on real data).
2. Take the top N accounts by this score, where N is itself a runtime
   percentile (e.g. top 5%), not a fixed count.
3. Intersect with **zero findings**: keep only accounts from step 2 where
   none of patterns 1–20 produced a finding. This is the actual trigger
   set — `anomaly_score > P95 AND matched_patterns_1_to_20 == 0`.
4. **Hard cap, non-negotiable:** sort the trigger set by anomaly score
   descending and take at most 15–20 accounts, regardless of how many
   qualify. This cap must be a named constant, never silently raised.
5. A manual trigger must also exist: any account_id can be explicitly
   requested for Role B investigation via the CLI/interface regardless of
   its score, for investigator-directed use. This does not count against
   the automatic cap, but should still have its own sane upper bound per
   run (e.g. 10 manual requests per run) to prevent runaway cost from
   repeated manual clicks.

**What happens when triggered:**

For each account in the trigger set, send the LLM that account's full
transaction history (narrations, amounts, dates, already-resolved
counterparties), the fact that it scored as a statistical anomaly, and the
fact that no named pattern matched. Prompt it to read the account the way
a financial investigator would and either (a) describe a specific,
evidence-backed pattern it notices, citing exact transaction IDs, or (b)
state plainly that it finds nothing notable beyond the statistical anomaly
itself. Do not feed it any other account's data in the same call.

**Output handling:**

- Every result — whether it found something or explicitly found nothing —
  is recorded under `findings_by_pattern.22_llm_investigated_anomalies`
  with `detection_method: "llm_investigated_anomaly"` and
  `confidence_tier: "low"` always (never `high` — this category is a lead
  for human review, never court-ready evidence on its own).
- Each result also carries `details.trigger_reason` (which condition
  fired: automatic score-based, or manual) and `details.trigger_status`:
  `triggered_with_finding`, `triggered_no_finding`, or — for the run-level
  summary, not per-account — `not_triggered` if no account met the
  condition at all this run. This must be visible in the run summary so a
  reader can tell "Role B ran and found nothing" apart from "Role B never
  ran this time."
- Pattern 22 findings are excluded from Pattern 21's score (Section 5) and
  shown in their own clearly-separated section in the interface (Section
  9b) — they are leads for an investigator to manually pursue, structurally
  different in kind from the 21 deterministic/scored patterns, and must
  never be presented with the same visual weight or implied certainty.

### 9e. Multi-key rotation for LLM calls (`llm_client.py`)

All three LLM-touching modules (`narration.py` for Role C/9c,
`llm_resolution.py` for Role A/9d, `anomaly_investigator.py` for Role B/9d)
call a single shared wrapper in `llm_client.py` — none of them talk to the
Groq API directly. This is the only place API keys and rotation logic
live.

**Requirements:**

- Load a list of API keys from environment/config (e.g.
  `GROQ_API_KEYS="key1,key2,key3"` or a small JSON list) — never a single
  hardcoded key, never committed to the repo.
- Maintain a simple rotation state: current key index, and a per-key status
  (`active`, `rate_limited`, `exhausted`, `invalid`) with a timestamp of
  when that status was last set.
- On every call, use the current active key. If the call fails with a
  rate-limit or quota-exhausted response (detect via status code / error
  body, not by guessing from a timeout), mark that key `rate_limited` or
  `exhausted` as appropriate, advance to the next key with status `active`,
  and **retry the same request once** on the new key before giving up on
  that call entirely.
- If a key is marked `rate_limited`, allow it to be retried after a cooldown
  window (e.g. retry-after header if provided, else a fixed backoff) rather
  than treating it as permanently dead — `exhausted`/`invalid` keys (auth
  failure, revoked) should not be retried automatically within the same
  run.
- If **all** keys are exhausted/rate-limited/invalid in the same call
  attempt, the wrapper must fail gracefully, not crash the pipeline: return
  a clear "llm_unavailable" signal up to the caller. Each of the three
  calling modules already has a defined fallback for this —
  `narration.py` falls back to template explanations (9c), and Role A / Role
  B (9d) simply do not produce their optional output for that run
  (`llm_assist_summary` / `trigger_status` reflect this honestly, the
  pipeline does not stop).
- Log every key rotation event (which key, why, what it switched to) to the
  run's audit trail — not the keys themselves, just the rotation event and
  reason — so a slow or exhausted key during a real run is diagnosable
  after the fact.
- This module has no detection logic of its own — it is purely a transport/
  resilience layer. Keep it small and testable independent of any specific
  prompt content from 9c/9d.

---

## 10. What Happens After This Phase Runs

The output of this phase (`analysis_results.json`) is handed to a separate,
later report-generation phase — not built in this pass. This phase's job
ends at producing complete, structured, auditable findings, rendered
through the interface update in Section 9.

Separately, outside of this implementation work, the real-data findings get
compared against independently-held investigative reference material as a
verification step — that comparison does not happen inside this codebase
and does not influence any threshold or detector logic here.

---

## 11. Final Note — This Plan Can Be Wrong, and That's Fine

This restates Ground Rule 8 one more time, deliberately, as the last word
in this document: if, while actually building this, Codex finds that any
part of this plan — the SQLite-as-working-store design, the build order,
a specific detector's algorithm, the LLM trigger logic, the file layout,
anything — is weak, overcomplicated, or simply wrong once it's working
against the real codebase and real data, **it has full permission to use
its own better approach instead.** This is a deliberate grant of
flexibility, not a fallback to be used reluctantly.

The only conditions, unchanged from Ground Rule 8: say plainly what was
changed and why, with real evidence from the actual code or data driving
the change — not silently swap an approach without flagging it — and keep
the non-negotiables intact regardless of what else changes (don't touch
the base model folder, no hardcoding, build blind, all 21 scored patterns
present with zero-results shown, synthetic tests as the pass/fail oracle,
extra rigor on the three ★ PRIORITY patterns, and the caps/separation/
tagging rules around Pattern 22 and the three Groq roles). Everything else
— including everything in this final section's own suggestions — is a
starting point Codex is expected to improve on wherever it genuinely can.
