# Analysis Pattern Logic And Scoring

This document describes the current implemented analysis phase under
`analysis/analysis_engine/` after the final fix-plan update.

The final catalog has 19 scored detector patterns, Pattern 21 account ranking,
and Patterns 22-23 as lead-only outputs. Pattern 6 is structural graph evidence
and is persisted, but it is not counted as a suspicious-account score hit.

## Finding Shape

Every detector emits `Finding` records from `analysis/analysis_engine/models.py`:

- `finding_id`
- `pattern_id`
- `pattern_name`
- `accounts`
- `txn_ids`
- `explanation`
- `narration`
- `narration_validation`
- `confidence_tier`
- `evidence_strength`
- `detection_method`
- `details`

`explanation` is the deterministic detector explanation. `narration` is the
operator-facing wording. If Groq is unavailable, narration stays as the template.
If Groq writes unsupported dates, account-like tokens, or long numeric claims,
the system falls back to the deterministic template and records
`narration_validation`.

## Evidence Strength

Evidence strength is assigned by pattern in `models.py`:

| Pattern | Strength | Base weight |
|---:|---|---:|
| 1 duplicate verification | weak | 0.6 |
| 2 failed/reversed transaction | weak | 0.6 |
| 3 pass-through/routing account | weak | 0.9 |
| 4 fund pooling account | weak | 0.8 |
| 5 structuring/smurfing | strong | 1.8 |
| 6 money-flow graph construction | structural | 0.3 |
| 7 circular flow | strong | 1.9 |
| 8 money trail tracing | strong | 2.2 |
| 9 credit-to-cash-out chains | weak | 1.0 |
| 10 cross-statement links | strong | 1.6 |
| 11 balance parking account | weak | 0.7 |
| 12 hub ranking | weak | 0.6 |
| 13 low-value account testing | strong | 1.4 |
| 14 reversal clusters | weak | 0.8 |
| 15 round-value debit patterns | weak | 0.7 |
| 16 shared UPI identifiers | strong | 1.5 |
| 17 round-trip detection | strong | 2.0 |
| 18 dormant reactivation | strong | 1.7 |
| 19 first-contact large transfer | strong | 1.5 |
| 21 suspicious account ranking | composite | not scored |
| 22 LLM-investigated anomalies | lead | not scored |
| 23 ML ensemble anomaly lead | lead | not scored |

Strong graph-dependent findings are downgraded to weak if the weakest
counterparty edge confidence is below `strong_counterparty_confidence_min`
from `AnalysisConfig` (default `0.65`). Edge confidence policy:

- exact reference / UPI / account match: `1.0`
- amount/date mirror when explicitly corroborated by ledger pairing: `1.0`
- narration-derived deterministic match: `0.65`
- LLM-inferred counterparty match: lead/assist only unless corroborated

## Account Score

Pattern 21 ranking calls `score_accounts()` in
`analysis/analysis_engine/scoring.py`.

Excluded from account scoring:

- Pattern 6, because it is graph construction evidence.
- Pattern 21, because it is the ranking itself.
- Pattern 22, because it is LLM lead-only.
- Pattern 23, because it is ML lead-only.

Formula:

```text
total_score =
    breadth_component
  + weighted_severity
  + value_component
  + centrality_bonus
```

Breadth:

```text
breadth_component =
  strong_pattern_count * 150
  + weak_pattern_count * 40
```

Weighted severity:

```text
base_weight_for_pattern * confidence_multiplier
```

Confidence multiplier is `1.0` for `high` and `0.5` for `low`.

Value component extracts numeric values from finding details whose keys contain
`amount`, `volume`, `credit`, or `debit`, then applies logarithmic scaling so a
single large amount cannot dominate all other evidence.

Centrality bonus uses `max(betweenness_centrality, degree_centrality) * 10`
when a finding provides graph centrality details.

Account ranking output appears in `suspicious_accounts[]` with:

- `account_id`
- `distinct_pattern_count`
- `strong_pattern_count`
- `weak_pattern_count`
- `pattern_breakdown`
- `finding_ids`
- `source_txn_ids`
- `total_findings`
- `total_score`
- `score_breakdown`

## Balance Handling

Balance mismatch is no longer a detector pattern. Extraction rows with
`flag_reason` or `exclusion_reason` containing `balance_mismatch` are summarized
under `balance_validation` as data-quality exclusions only. They do not create
suspicion findings.

## Pattern Logic

### Pattern 1: Duplicate Verification

Source: `analysis/analysis_engine/detectors/duplicates.py`

Independently verifies duplicate-like pairs using a 2-of-3 signal rule:

- same reference
- narration similarity above runtime/config threshold
- same counterparty on same day

Amount and date windows are also enforced. Recurring payment series are excluded
using repeat-count and gap-tolerance settings before a duplicate finding is
created.

### Pattern 2: Failed/Reversed Transaction Detection

Source: `detectors/reversals.py`

Finds a debit followed by a near-equal credit reversal within the configured
window and amount tolerance. Existing extraction reversal labels can support the
finding but do not replace the runtime check.

### Pattern 3: Pass-Through / Routing Account

Sources: `detectors/transit.py`, `detectors/high_throughput.py`

Merges old transit and high-throughput behavior. Flags accounts with fast
inflow-to-outflow movement, high throughput ratio, or unusually high transaction
velocity compared with runtime baseline statistics.

### Pattern 4: Fund Pooling Account

Source: `detectors/accumulation.py`

Finds accounts receiving funds from multiple sources and retaining or pooling
incoming value above runtime volume and counterparty thresholds.

### Pattern 5: Structuring / Smurfing

Source: `detectors/structuring.py`

Detects repeated smaller transactions clustered around runtime-derived amount
thresholds, especially when daily counts or repeated values suggest deliberate
splitting.

### Pattern 6: Money-Flow Graph Construction

Sources: `graph.py`, `database.py`

Builds the directed money-flow graph from eligible transactions and resolved
counterparties. It records node count, edge count, edge sources, transaction IDs,
and counterparty confidence. This is structural evidence, not a suspicious score
hit by itself.

### Pattern 7: Circular Flow

Source: `detectors/circular.py`

Searches the directed graph for multi-hop cycles up to the configured maximum
cycle length. The finding includes edge confidence details and is downgraded if
graph links are below the strong-confidence threshold.

### Pattern 8: Money Trail Tracing

Source: `detectors/money_trail.py`

Traces significant credits into later debits using FIFO allocation. It now
auto-triggers from runtime significant-credit thresholds and from accounts
already hit by strong deterministic patterns. Manual credit transaction IDs are
still supported.

### Pattern 9: Credit-To-Cash-Out Chains

Source: `detectors/credit_to_cash.py`

Finds large credits followed by ATM/cash-like withdrawals within the configured
window and runtime amount thresholds.

### Pattern 10: Cross-Statement Links

Source: `detectors/cross_statement.py`

Finds debit/credit pairs across or within statements when amount/date matching
is corroborated by reference match or resolved counterparty link. Details record
whether the match is in the same document.

### Pattern 11: Balance Parking Account

Source: `detectors/holding_accounts.py`

Finds accounts where large value remains parked for a runtime-derived duration
or above a runtime holding threshold.

### Pattern 12: Hub Ranking

Source: `detectors/hubs.py`

Ranks graph hubs using degree, counterparties, and volume/centrality signals.
This is weak evidence because being a hub can be legitimate without additional
patterns.

### Pattern 13: Low-Value Account Testing

Source: `detectors/low_value_testing.py`

Finds small test transfers, often reciprocal or followed by larger movement.
Although amounts are low, it is strong evidence because it indicates account-link
probing behavior.

### Pattern 14: Reversal Clusters

Source: `detectors/reversal_clusters.py`

Looks for repeated reversal-like behavior clustered by account, date, and amount
patterns.

### Pattern 15: Round-Value Debit Patterns

Source: `detectors/round_value_debits.py`

Finds repeated debit values divisible by the configured round-value divisor,
with frequency checked against runtime account behavior.

### Pattern 16: Shared UPI Identifiers

Source: `detectors/shared_upi.py`

Finds UPI/VPA identifiers shared across accounts or reused in suspicious ways.
This is strong because a shared payment identifier can connect otherwise separate
accounts.

### Pattern 17: Round-Trip Detection

Source: `detectors/round_trip.py`

Finds funds moving from an account through one or more hops and returning to the
origin within the configured hop/date/amount constraints. Edge confidence is
recorded and can downgrade strong evidence.

### Pattern 18: Dormant Reactivation

Source: `detectors/dormant_reactivation.py`

Detects accounts with a runtime-derived dormant gap followed by a large credit
and rapid outbound movement. Thresholds come from transaction amount quantiles
and typical account gap statistics.

### Pattern 19: First-Contact Large Transfer

Source: `detectors/first_contact.py`

Flags the first observed transfer between an account pair when the amount is
large relative to runtime transaction thresholds. Edge confidence is recorded and
can downgrade strong evidence.

### Pattern 21: Suspicious Account Ranking

Source: `detectors/ranking.py`, `scoring.py`

Creates account-level composite findings from the scoring output. It records
which patterns contributed and the score breakdown.

### Pattern 22: LLM-Investigated Anomalies

Source: `anomaly_investigator.py`

Lead-only. Runs only when strong deterministic findings or manual account IDs
justify investigation. It does not add to account scores.

### Pattern 23: ML Ensemble Anomaly Lead

Source: `detectors/ml_ensemble.py`

Lead-only. Uses account and graph features with Isolation Forest,
Local Outlier Factor, and a robust histogram/MAD-style score. Accounts already
covered by strong deterministic findings are skipped. A lead is emitted only
when at least two independent models fire.

