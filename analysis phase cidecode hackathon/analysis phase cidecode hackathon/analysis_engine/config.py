"""Configuration values that are universal safeguards, never dataset tuning."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AnalysisConfig:
    """Configurable universal limits; patterns 4–7 thresholds come from baseline."""

    balance_tolerance: float = 1.0
    money_epsilon: float = 0.01

    duplicate_date_window_days: int = 1
    duplicate_amount_relative_tolerance: float = 0.005
    duplicate_narration_similarity: float = 0.80

    reversal_amount_relative_tolerance: float = 0.02
    reversal_narration_similarity: float = 0.45
    reversal_window_multiplier: float = 3.0

    default_round_trip_window_days: int = 30
    settlement_window_multiplier: float = 10.0
    max_round_trip_hops: int = 5
    max_cycle_length: int = 6

    upper_amount_quantile: float = 0.99
    high_volume_quantile: float = 0.75
    high_ratio_quantile: float = 0.75
    low_ratio_quantile: float = 0.25
    dense_activity_quantile: float = 0.95
    minimum_cluster_size: int = 3

    maximum_findings_per_pattern: int = 5_000
    maximum_trace_debits: int = 10_000

    llm_model: str = "llama-3.1-8b-instant"
    enable_llm_fallback: bool = True

    sqlite_busy_timeout_ms: int = 30_000

    # ── New pattern thresholds (11–21) ──
    high_throughput_min_counterparties: int = 3
    high_throughput_retention_max: float = 0.10

    credit_to_cash_window_days: int = 3

    round_value_min_repeats: int = 3
    round_value_divisor: float = 1000.0

    low_value_test_max_amount: float = 100.0
    low_value_test_min_pairs: int = 2

    reversal_cluster_min_pairs: int = 3

    hub_ranking_min_degree: int = 3

