"""Pattern 18: dormant reactivation followed by rapid outflow."""

from __future__ import annotations

import sqlite3

import pandas as pd

from ..config import AnalysisConfig
from ..database import fetch_transactions
from .common import make_finding


def detect_dormant_reactivation(
    connection: sqlite3.Connection,
    baseline: dict,
    config: AnalysisConfig,
) -> list:
    threshold = float(baseline.get("transaction_amounts", {}).get("p90") or 0.0)
    typical_gap = float(baseline.get("thresholds", {}).get("typical_transaction_gap_days") or 1.0)
    dormant_gap = max(30, int(round(typical_gap * 10)))
    window_days = config.dormant_reactivation_outflow_window_days

    frame = fetch_transactions(connection, "eligible_for_detection = 1 AND date IS NOT NULL")
    if frame.empty:
        return []
    frame["parsed_date"] = pd.to_datetime(frame["date"], errors="coerce")
    findings = []
    for account_id, group in frame.groupby("account_id", sort=False):
        ordered = group.sort_values(["parsed_date", "time", "source_order", "row_id"]).reset_index(drop=True)
        previous_date = None
        for row in ordered.itertuples(index=False):
            row_date = row.parsed_date
            if pd.isna(row_date):
                continue
            gap_days = None if previous_date is None else int((row_date - previous_date).days)
            previous_date = row_date
            if gap_days is None or gap_days < dormant_gap or float(row.credit_amount) < threshold:
                continue
            following = ordered[
                (ordered["source_order"] > row.source_order)
                & (ordered["parsed_date"] >= row_date)
                & ((ordered["parsed_date"] - row_date).dt.days <= window_days)
                & (ordered["debit_amount"] > config.money_epsilon)
            ]
            outflow = float(following["debit_amount"].sum())
            ratio = outflow / max(float(row.credit_amount), config.money_epsilon)
            if ratio < config.dormant_reactivation_outflow_ratio_min:
                continue
            txn_ids = [str(row.txn_id)] + following["txn_id"].astype(str).tolist()
            findings.append(
                make_finding(
                    connection,
                    18,
                    [str(account_id)],
                    txn_ids,
                    f"Account {account_id} was inactive for {gap_days} day(s), then received {row.credit_amount:.2f} and sent out {outflow:.2f} within {window_days} day(s).",
                    {
                        "dormant_gap_days": gap_days,
                        "reactivation_credit_amount": float(row.credit_amount),
                        "outflow_amount": outflow,
                        "outflow_ratio": ratio,
                        "outflow_window_days": window_days,
                        "runtime_thresholds": {
                            "dormant_gap_days": dormant_gap,
                            "large_credit_min": threshold,
                            "outflow_ratio_min": config.dormant_reactivation_outflow_ratio_min,
                        },
                    },
                    config=config,
                )
            )
            if len(findings) >= config.maximum_findings_per_pattern:
                return findings
    return findings
