# Copied from: analysis phase cidecode hackathon/analysis phase cidecode hackathon/analysis_engine/balance.py
"""Balance-chain gatekeeper implementing the mandatory extraction-error rule."""

from __future__ import annotations

from datetime import date
import sqlite3
from typing import Any

import pandas as pd

from .config import AnalysisConfig
from .database import fetch_transactions
from .models import Finding, PATTERN_CATALOG
from .utils import lowest_confidence


def _between(value: str | None, start: str | None, end: str | None) -> bool:
    if not value or not start or not end:
        return False
    return start <= value <= end


def _duplicate_explanation(
    duplicates: pd.DataFrame,
    account_id: str,
    start_date: str | None,
    end_date: str | None,
    residual: float,
    tolerance: float,
) -> list[str]:
    if duplicates.empty:
        return []
    candidates = duplicates[
        (duplicates["account_id"] == account_id)
        & duplicates["date"].map(lambda value: _between(value, start_date, end_date))
        & duplicates["balance"].isna()
    ].copy()
    if candidates.empty:
        return []
    candidates["delta"] = candidates["credit_amount"] - candidates["debit_amount"]
    for row in candidates.itertuples():
        if abs(float(row.delta) - residual) <= tolerance:
            return [row.txn_id]
    if abs(float(candidates["delta"].sum()) - residual) <= tolerance:
        return candidates["txn_id"].astype(str).tolist()
    return []


def validate_balances(
    connection: sqlite3.Connection,
    config: AnalysisConfig,
) -> tuple[list[Finding], dict[str, Any]]:
    frame = fetch_transactions(connection)
    frame["date_sort"] = frame["date"].fillna("9999-12-31")
    frame["time_sort"] = frame["time"].fillna("")
    frame = frame.sort_values(
        ["account_id", "date_sort", "time_sort", "source_order", "row_id"],
        kind="mergesort",
    )
    duplicates = frame[frame["source_bucket"] == "duplicate"]
    findings: list[Finding] = []
    audit_rows: list[tuple[Any, ...]] = []
    status_counts: dict[str, int] = {}

    for account_id, group in frame.groupby("account_id", sort=False, dropna=False):
        previous: pd.Series | None = None
        for _, row in group.iterrows():
            status = "unavailable"
            expected = None
            actual = None if pd.isna(row["balance"]) else float(row["balance"])
            difference = None
            explanation = "No usable running balance is present for this row."

            if row["exclusion_reason"] == "extraction_error_balance_mismatch":
                status = "excluded_source_balance_mismatch"
                explanation = "Extraction already proved this row does not reconcile; the chain restarts after it."
                previous = None
            elif row["date"] is None:
                status = "invalid_date_chain_break"
                explanation = "A chronological balance check is impossible because the date is unavailable."
                previous = None
            elif actual is None:
                status = "balance_unavailable"
            elif previous is None or pd.isna(previous["balance"]):
                status = "anchor"
                explanation = "This is the first usable balance in the account segment and is the validation anchor."
                previous = row
            else:
                expected = (
                    float(previous["balance"])
                    + float(row["credit_amount"])
                    - float(row["debit_amount"])
                )
                difference = actual - expected
                if abs(difference) <= config.balance_tolerance:
                    status = "consistent"
                    explanation = "Previous balance plus credit minus debit equals the printed balance."
                    previous = row
                elif row["source_bucket"] == "duplicate":
                    status = "duplicate_not_in_balance_chain"
                    explanation = (
                        "The suspected duplicate does not form an independent ledger step and remains excluded."
                    )
                else:
                    residual = actual - expected
                    explaining_duplicates = _duplicate_explanation(
                        duplicates,
                        str(account_id),
                        previous["date"],
                        row["date"],
                        residual,
                        config.balance_tolerance,
                    )
                    if explaining_duplicates:
                        status = "consistent_with_excluded_duplicate"
                        explanation = (
                            "The balance gap is exactly explained by excluded duplicate-bucket movement(s): "
                            + ", ".join(explaining_duplicates)
                        )
                        previous = row
                    else:
                        status = "mismatch"
                        explanation = (
                            f"Expected balance {expected:.2f}, but the source row prints {actual:.2f}; "
                            "the row is excluded as an extraction error."
                        )
                        if int(row["eligible_for_detection"]) == 1:
                            connection.execute(
                                """
                                UPDATE transactions
                                SET eligible_for_detection = 0,
                                    exclusion_reason = 'post_load_balance_mismatch',
                                    balance_validation_status = 'mismatch'
                                WHERE row_id = ?
                                """,
                                (int(row["row_id"]),),
                            )
                            tiers = [row["confidence_tier"]]
                            txn_ids = [str(row["txn_id"])]
                            if previous is not None:
                                tiers.append(previous["confidence_tier"])
                                txn_ids.insert(0, str(previous["txn_id"]))
                            findings.append(
                                Finding(
                                    pattern_id=3,
                                    pattern_name=PATTERN_CATALOG[3],
                                    accounts=[str(account_id)],
                                    txn_ids=txn_ids,
                                    explanation=explanation,
                                    confidence_tier=lowest_confidence(tiers),
                                    details={
                                        "expected_balance": expected,
                                        "actual_balance": actual,
                                        "difference": difference,
                                        "source_document": row["doc_id"],
                                        "source_page": row["source_page"],
                                        "runtime_thresholds": {
                                            "balance_tolerance": config.balance_tolerance,
                                        },
                                        "source_documents": [
                                            {
                                                "txn_id": str(row["txn_id"]),
                                                "doc_id": str(row["doc_id"] or ""),
                                                "page": str(row["source_page"] or ""),
                                            }
                                        ],
                                        "explanation_source": "template",
                                    },
                                )
                            )
                        previous = row

            status_counts[status] = status_counts.get(status, 0) + 1
            audit_rows.append(
                (
                    int(row["row_id"]), str(row["txn_id"]), str(account_id), status,
                    str(previous["txn_id"]) if previous is not None and status not in {"anchor"} else None,
                    expected, actual, difference, explanation,
                )
            )
            connection.execute(
                "UPDATE transactions SET balance_validation_status = ? WHERE row_id = ?",
                (status, int(row["row_id"])),
            )

    connection.execute("DELETE FROM balance_validation")
    connection.executemany(
        """
        INSERT INTO balance_validation(
            row_id, txn_id, account_id, status, previous_txn_id,
            expected_balance, actual_balance, difference, explanation
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        audit_rows,
    )
    connection.commit()
    summary = {
        "rows_checked": int(len(frame)),
        "status_counts": status_counts,
        "newly_excluded_count": len(findings),
    }
    return findings, summary
