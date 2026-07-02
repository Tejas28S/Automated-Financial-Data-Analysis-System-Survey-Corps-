# Copied from: analysis phase cidecode hackathon/analysis phase cidecode hackathon/analysis_engine/baseline.py
"""Runtime statistics and detector thresholds derived from the loaded dataset."""

from __future__ import annotations

from math import ceil
import sqlite3
from typing import Any

import numpy as np
import pandas as pd

from .config import AnalysisConfig
from .database import fetch_transactions, persist_baseline
from .utils import clamp, safe_ratio


def _quantile(values: pd.Series, probability: float) -> float:
    clean = pd.to_numeric(values, errors="coerce").dropna()
    return float(clean.quantile(probability)) if not clean.empty else 0.0


def _account_statistics(frame: pd.DataFrame) -> pd.DataFrame:
    eligible = frame[frame["eligible_for_detection"] == 1].copy()
    eligible["amount"] = eligible[["debit_amount", "credit_amount"]].max(axis=1)
    grouped = eligible.groupby("account_id", dropna=False)
    stats = grouped.agg(
        account_holder=("account_holder", lambda values: next((v for v in values if v), "")),
        bank_name=("bank_name", lambda values: next((v for v in values if v), "")),
        ifsc_code=("ifsc_code", lambda values: next((v for v in values if v), "")),
        transaction_count=("row_id", "size"),
        total_credit=("credit_amount", "sum"),
        total_debit=("debit_amount", "sum"),
        first_date=("date", "min"),
        last_date=("date", "max"),
    ).reset_index()
    stats["total_volume"] = stats["total_credit"] + stats["total_debit"]
    larger = stats[["total_credit", "total_debit"]].max(axis=1)
    smaller = stats[["total_credit", "total_debit"]].min(axis=1)
    stats["throughput_ratio"] = [safe_ratio(a, b) for a, b in zip(smaller, larger)]
    stats["unique_counterparty_count"] = 0
    return stats


def _typical_gap_days(frame: pd.DataFrame) -> float:
    gaps: list[float] = []
    eligible = frame[(frame["eligible_for_detection"] == 1) & frame["date"].notna()].copy()
    eligible["parsed_date"] = pd.to_datetime(eligible["date"], errors="coerce")
    for _, group in eligible.groupby("account_id"):
        dates = group["parsed_date"].dropna().drop_duplicates().sort_values()
        differences = dates.diff().dt.days.dropna()
        gaps.extend(float(value) for value in differences if value > 0)
    return float(np.median(gaps)) if gaps else 1.0


def compute_baseline(
    connection: sqlite3.Connection,
    config: AnalysisConfig,
    persist: bool = True,
) -> dict[str, Any]:
    frame = fetch_transactions(connection)
    eligible = frame[frame["eligible_for_detection"] == 1].copy()
    eligible["amount"] = eligible[["debit_amount", "credit_amount"]].max(axis=1)
    positive_amounts = eligible.loc[eligible["amount"] > config.money_epsilon, "amount"]
    rounded_amounts = positive_amounts.round(2)
    amount_counts = rounded_amounts.value_counts().sort_index()
    frequency_quantile_cutoff = (
        float(amount_counts.quantile(config.amount_common_frequency_quantile))
        if not amount_counts.empty
        else 0.0
    )
    frequency_share_cutoff = (
        float(ceil(len(rounded_amounts) * config.amount_common_frequency_share))
        if not rounded_amounts.empty
        else 0.0
    )
    frequency_cutoff = max(3.0, min(frequency_quantile_cutoff, frequency_share_cutoff))
    high_frequency_amounts = sorted(
        float(amount)
        for amount, count in amount_counts.items()
        if float(count) >= frequency_cutoff and int(count) > 1
    )
    account_stats = _account_statistics(frame)

    median_amount = _quantile(positive_amounts, 0.5)
    amount_mad = (
        float(np.median(np.abs(positive_amounts - median_amount)))
        if not positive_amounts.empty
        else 0.0
    )
    relative_scale = safe_ratio(amount_mad, median_amount)
    typical_gap = _typical_gap_days(frame)

    dated = eligible[eligible["date"].notna()].copy()
    dated["direction"] = np.where(dated["debit_amount"] > config.money_epsilon, "debit", "credit")
    daily_counts = (
        dated.groupby(["account_id", "date", "direction"]).size().astype(float)
        if not dated.empty
        else pd.Series(dtype=float)
    )

    upper_amount = _quantile(positive_amounts, config.upper_amount_quantile)
    high_amount = _quantile(positive_amounts, config.high_volume_quantile)
    daily_count_cutoff = max(
        config.minimum_cluster_size,
        int(np.floor(_quantile(daily_counts, config.dense_activity_quantile))) + 1,
    )
    structured_total_cutoff = _quantile(positive_amounts, 0.90) * daily_count_cutoff

    throughput_values = account_stats["throughput_ratio"] if not account_stats.empty else pd.Series(dtype=float)
    volume_values = account_stats["total_volume"] if not account_stats.empty else pd.Series(dtype=float)
    credit_values = account_stats["total_credit"] if not account_stats.empty else pd.Series(dtype=float)
    outflow_ratios = account_stats.apply(
        lambda row: safe_ratio(row["total_debit"], row["total_credit"]), axis=1
    ) if not account_stats.empty else pd.Series(dtype=float)

    date_values = pd.to_datetime(eligible["date"], errors="coerce").dropna()
    source_counts = frame["source_bucket"].value_counts().to_dict()
    exclusion_counts = (
        frame.loc[frame["eligible_for_detection"] == 0, "exclusion_reason"]
        .replace("", "other")
        .value_counts()
        .to_dict()
    )

    thresholds = {
        "typical_transaction_gap_days": typical_gap,
        "reversal_window_days": max(1, int(ceil(typical_gap * config.reversal_window_multiplier))),
        "round_trip_window_days": min(
            config.default_round_trip_window_days,
            max(1, int(ceil(typical_gap * config.settlement_window_multiplier))),
        ),
        "round_trip_min_retention_ratio": 1.0 - clamp(relative_scale * 2.0, 0.05, 0.40),
        "transit_throughput_ratio_min": _quantile(throughput_values, config.high_ratio_quantile),
        "transit_volume_min": _quantile(volume_values, config.high_volume_quantile),
        "transit_transaction_count_min": _quantile(account_stats.get("transaction_count", pd.Series(dtype=float)), 0.5),
        "accumulation_credit_min": _quantile(credit_values, config.high_volume_quantile),
        "accumulation_outflow_to_inflow_max": _quantile(outflow_ratios, config.low_ratio_quantile),
        "accumulation_unique_counterparty_min": 0.0,
        "structuring_individual_amount_max": upper_amount,
        "structuring_min_count": daily_count_cutoff,
        "structuring_collective_amount_min": structured_total_cutoff,
        "high_value_amount": high_amount,
        # ── New pattern thresholds (11–21), data-driven ──
        "high_throughput_volume_min": _quantile(volume_values, config.high_volume_quantile),
        "credit_to_cash_amount_min": _quantile(positive_amounts, config.high_volume_quantile),
        "money_trail_extreme_credit_min": _quantile(positive_amounts, config.money_trail_strict_credit_quantile),
        "round_value_collective_min": _quantile(positive_amounts, 0.50) * config.round_value_min_repeats,
        "holding_credit_min": _quantile(credit_values, config.high_volume_quantile),
        "holding_retention_min": clamp(
            1.0 - _quantile(outflow_ratios, config.high_ratio_quantile), 0.30, 0.90
        ),
        "hub_flow_volume_min": _quantile(volume_values, 0.50),
    }

    top_volume = (
        account_stats.sort_values(["total_volume", "account_id"], ascending=[False, True])
        .head(10)[["account_id", "transaction_count", "total_volume", "throughput_ratio"]]
        .to_dict(orient="records")
        if not account_stats.empty
        else []
    )
    credit_floor_p40 = {
        str(account_id): _quantile(group["credit_amount"], 0.40)
        for account_id, group in eligible[eligible["credit_amount"] > config.money_epsilon].groupby("account_id")
    }
    strong_account_credit_floor = {
        str(account_id): _quantile(group["credit_amount"], config.money_trail_strong_account_credit_quantile)
        for account_id, group in eligible[eligible["credit_amount"] > config.money_epsilon].groupby("account_id")
    }
    summary: dict[str, Any] = {
        "row_counts": {
            "total": int(len(frame)),
            "eligible": int((frame["eligible_for_detection"] == 1).sum()),
            "excluded": int((frame["eligible_for_detection"] == 0).sum()),
            "by_source_bucket": {str(k): int(v) for k, v in source_counts.items()},
            "by_exclusion_reason": {str(k): int(v) for k, v in exclusion_counts.items()},
        },
        "account_count": int(eligible["account_id"].nunique()),
        "date_range": {
            "start": date_values.min().date().isoformat() if not date_values.empty else None,
            "end": date_values.max().date().isoformat() if not date_values.empty else None,
        },
        "transaction_amounts": {
            "median": median_amount,
            "p75": _quantile(positive_amounts, 0.75),
            "p90": _quantile(positive_amounts, 0.90),
            "p95": _quantile(positive_amounts, 0.95),
            "p99": _quantile(positive_amounts, 0.99),
            "maximum": float(positive_amounts.max()) if not positive_amounts.empty else 0.0,
            "median_absolute_deviation": amount_mad,
        },
        "median_transactions_per_account": _quantile(
            account_stats.get("transaction_count", pd.Series(dtype=float)), 0.5
        ),
        "thresholds": thresholds,
        "amount_commonality": {
            "rounding_decimals": 2,
            "frequency_quantile": config.amount_common_frequency_quantile,
            "frequency_share": config.amount_common_frequency_share,
            "frequency_quantile_cutoff_count": frequency_quantile_cutoff,
            "frequency_share_cutoff_count": frequency_share_cutoff,
            "frequency_cutoff_count": frequency_cutoff,
            "high_frequency_amounts": high_frequency_amounts,
            "amount_frequency_counts": {
                f"{float(amount):.2f}": int(count)
                for amount, count in amount_counts.items()
            },
        },
        "account_credit_floor_p40": credit_floor_p40,
        "account_credit_floor_strong": strong_account_credit_floor,
        "top_accounts_by_volume": top_volume,
        "top_accounts_by_connectivity": [],
    }

    connection.execute("DELETE FROM accounts")
    if not account_stats.empty:
        connection.executemany(
            """
            INSERT INTO accounts(
                account_id, account_holder, bank_name, ifsc_code, transaction_count,
                total_credit, total_debit, total_volume, throughput_ratio,
                unique_counterparty_count, first_date, last_date
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row.account_id, row.account_holder, row.bank_name, row.ifsc_code,
                    int(row.transaction_count), float(row.total_credit), float(row.total_debit),
                    float(row.total_volume), float(row.throughput_ratio),
                    int(row.unique_counterparty_count), row.first_date, row.last_date,
                )
                for row in account_stats.itertuples(index=False)
            ],
        )
    connection.commit()
    if persist:
        persist_baseline(connection, summary)
    return summary


def finalize_counterparty_statistics(
    connection: sqlite3.Connection,
    baseline: dict[str, Any],
    config: AnalysisConfig,
) -> dict[str, Any]:
    counts = pd.read_sql_query(
        """
        SELECT account_id, COUNT(DISTINCT counterparty_account) AS unique_counterparty_count
        FROM transactions
        WHERE eligible_for_detection = 1
          AND COALESCE(counterparty_account, '') != ''
        GROUP BY account_id
        """,
        connection,
    )
    connection.execute("UPDATE accounts SET unique_counterparty_count = 0")
    connection.executemany(
        "UPDATE accounts SET unique_counterparty_count = ? WHERE account_id = ?",
        [(int(row.unique_counterparty_count), row.account_id) for row in counts.itertuples(index=False)],
    )
    account_frame = pd.read_sql_query("SELECT * FROM accounts", connection)
    unique_values = account_frame["unique_counterparty_count"] if not account_frame.empty else pd.Series(dtype=float)
    baseline["thresholds"]["accumulation_unique_counterparty_min"] = max(
        1.0, _quantile(unique_values, config.high_ratio_quantile)
    )
    baseline["top_accounts_by_connectivity"] = (
        account_frame.sort_values(
            ["unique_counterparty_count", "total_volume", "account_id"],
            ascending=[False, False, True],
        )
        .head(10)[["account_id", "unique_counterparty_count", "total_volume"]]
        .to_dict(orient="records")
        if not account_frame.empty
        else []
    )
    connection.commit()
    persist_baseline(connection, baseline)
    return baseline
