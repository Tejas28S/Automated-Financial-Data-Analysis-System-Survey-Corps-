"""Pattern 16: repeated round-value debit pattern.

Flags accounts making numerous transfers using identical or rounded amounts
(e.g. ₹10,000, ₹20,000, ₹50,000 repeatedly), which can indicate structuring
or automated layering.
"""

from __future__ import annotations

import sqlite3
from collections import Counter

import pandas as pd

from ..config import AnalysisConfig
from ..database import fetch_transactions
from .common import make_finding


def _is_round_value(amount: float, divisor: float) -> bool:
    """Check if an amount is a round multiple of the divisor."""
    if amount <= 0 or divisor <= 0:
        return False
    return abs(amount % divisor) < 0.01


def detect_round_value_debits(
    connection: sqlite3.Connection,
    baseline: dict,
    config: AnalysisConfig,
) -> list:
    thresholds = baseline["thresholds"]
    frame = fetch_transactions(connection, "eligible_for_detection = 1 AND date IS NOT NULL")
    if frame.empty:
        return []

    # Focus on debits only
    debits = frame[frame["debit_amount"] > config.money_epsilon].copy()
    if debits.empty:
        return []

    findings = []

    for account_id, group in debits.groupby("account_id", sort=False):
        # Find round-value amounts
        round_mask = group["debit_amount"].apply(
            lambda a: _is_round_value(float(a), config.round_value_divisor)
        )
        round_debits = group[round_mask]
        if len(round_debits) < config.round_value_min_repeats:
            continue

        # Count repeated amounts
        amount_counts = Counter(float(a) for a in round_debits["debit_amount"])
        repeated_amounts = {
            amt: count for amt, count in amount_counts.items()
            if count >= config.round_value_min_repeats
        }

        if not repeated_amounts:
            # Even without exact repeats, many round values together is suspicious
            total = float(round_debits["debit_amount"].sum())
            if total < thresholds["round_value_collective_min"]:
                continue
            if len(round_debits) < config.round_value_min_repeats * 2:
                continue

        # Collect txn_ids for all round-value debits
        ordered = round_debits.sort_values(["date", "time", "source_order", "row_id"])
        txn_ids = ordered["txn_id"].astype(str).tolist()
        amounts = ordered["debit_amount"].astype(float).tolist()
        total_round = float(ordered["debit_amount"].sum())

        explanation_parts = []
        if repeated_amounts:
            for amt, count in sorted(repeated_amounts.items(), key=lambda x: -x[1]):
                explanation_parts.append(f"₹{amt:,.0f} x{count}")
            explanation_detail = ", ".join(explanation_parts[:5])
            explanation = (
                f"Account {account_id} made {len(round_debits)} round-value debits "
                f"totalling {total_round:.2f}, with repeated amounts: {explanation_detail}."
            )
        else:
            explanation = (
                f"Account {account_id} made {len(round_debits)} round-value debits "
                f"(multiples of {config.round_value_divisor:.0f}) totalling {total_round:.2f}."
            )

        findings.append(
            make_finding(
                connection,
                16,
                [str(account_id)],
                txn_ids,
                explanation,
                {
                    "round_debit_count": len(round_debits),
                    "total_round_debits": total_round,
                    "repeated_amounts": {str(k): v for k, v in repeated_amounts.items()},
                    "individual_amounts": amounts,
                    "divisor": config.round_value_divisor,
                    "runtime_thresholds": {
                        "min_repeats": config.round_value_min_repeats,
                        "collective_min": thresholds["round_value_collective_min"],
                    },
                },
            )
        )
        if len(findings) >= config.maximum_findings_per_pattern:
            break

    return findings
