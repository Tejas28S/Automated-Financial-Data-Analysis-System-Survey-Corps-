# Copied from: analysis phase cidecode hackathon/analysis phase cidecode hackathon/analysis_engine/detectors/structuring.py
"""Pattern 5: baseline-relative structuring/smurfing clusters."""

from __future__ import annotations

import sqlite3

import numpy as np

from ..config import AnalysisConfig
from ..database import fetch_transactions
from .common import make_finding


def detect_structuring(
    connection: sqlite3.Connection,
    baseline: dict,
    config: AnalysisConfig,
) -> list:
    thresholds = baseline["thresholds"]
    frame = fetch_transactions(connection, "eligible_for_detection = 1 AND date IS NOT NULL")
    frame["amount"] = frame[["debit_amount", "credit_amount"]].max(axis=1)
    frame["direction"] = np.where(
        frame["debit_amount"] > config.money_epsilon, "debit", "credit"
    )
    candidates = frame[
        (frame["amount"] > config.money_epsilon)
        & (frame["amount"] <= thresholds["structuring_individual_amount_max"])
    ]
    findings = []
    for (account_id, transaction_date, direction), group in candidates.groupby(
        ["account_id", "date", "direction"], sort=True
    ):
        count = len(group)
        total = float(group["amount"].sum())
        if (
            count < thresholds["structuring_min_count"]
            or total < thresholds["structuring_collective_amount_min"]
        ):
            continue
        txn_ids = group.sort_values(["time", "source_order", "row_id"])["txn_id"].astype(str).tolist()
        findings.append(
            make_finding(
                connection,
                5,
                [str(account_id)],
                txn_ids,
                f"Account {account_id} made {count} {direction} transactions on {transaction_date}; each was below the runtime individual cutoff, while together they totalled {total:.2f}.",
                {
                    "direction": direction,
                    "date": transaction_date,
                    "transaction_count": count,
                    "aggregate_amount": total,
                    "individual_amounts": group["amount"].astype(float).tolist(),
                    "runtime_thresholds": {
                        "individual_amount_max": thresholds["structuring_individual_amount_max"],
                        "minimum_count": thresholds["structuring_min_count"],
                        "collective_amount_min": thresholds["structuring_collective_amount_min"],
                    },
                },
            )
        )
        if len(findings) >= config.maximum_findings_per_pattern:
            break
    return findings
