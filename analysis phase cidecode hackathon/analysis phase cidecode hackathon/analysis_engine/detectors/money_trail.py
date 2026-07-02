"""Pattern 10: investigator-requested FIFO tracing of a specific credit."""

from __future__ import annotations

import sqlite3

import pandas as pd

from ..config import AnalysisConfig
from .common import make_finding


def trace_credit(
    connection: sqlite3.Connection,
    txn_id: str,
    config: AnalysisConfig,
):
    credit_row = connection.execute(
        "SELECT * FROM transactions WHERE txn_id = ? AND eligible_for_detection = 1",
        (txn_id,),
    ).fetchone()
    if credit_row is None:
        raise ValueError(f"Eligible credit transaction not found: {txn_id}")
    credited_amount = float(credit_row["credit_amount"])
    if credited_amount <= config.money_epsilon:
        raise ValueError(f"Transaction is not a credit: {txn_id}")

    frame = pd.read_sql_query(
        """
        SELECT * FROM transactions
        WHERE account_id = ? AND eligible_for_detection = 1 AND date IS NOT NULL
        ORDER BY date, time, source_order, row_id
        """,
        connection,
        params=(credit_row["account_id"],),
    )
    positions = frame.index[frame["txn_id"] == txn_id].tolist()
    if not positions:
        raise ValueError(f"Credit transaction is unavailable in chronological account history: {txn_id}")
    start_position = positions[0]
    remaining = credited_amount
    pre_credit_balance = (
        float(credit_row["balance"]) - credited_amount
        if credit_row["balance"] is not None
        else None
    )
    allocations = []
    consumed_txn_ids = [txn_id]

    for row in frame.iloc[start_position + 1 :].itertuples(index=False):
        debit = float(row.debit_amount)
        if debit <= config.money_epsilon:
            continue
        allocation = min(remaining, debit)
        if allocation <= config.money_epsilon:
            break
        allocations.append(
            {
                "debit_txn_id": str(row.txn_id),
                "date": str(row.date),
                "debit_amount": debit,
                "allocated_from_credit": allocation,
                "remaining_after_allocation": remaining - allocation,
                "balance_after_debit": None if pd.isna(row.balance) else float(row.balance),
            }
        )
        consumed_txn_ids.append(str(row.txn_id))
        remaining -= allocation
        if remaining <= config.money_epsilon:
            remaining = 0.0
            break
        if (
            pre_credit_balance is not None
            and not pd.isna(row.balance)
            and float(row.balance) <= pre_credit_balance + config.balance_tolerance
        ):
            break
        if len(allocations) >= config.maximum_trace_debits:
            break

    traced = credited_amount - remaining
    status = "exhausted" if remaining <= config.money_epsilon else "partially_traced"
    return make_finding(
        connection,
        10,
        [str(credit_row["account_id"])],
        consumed_txn_ids,
        f"Credit {txn_id} of {credited_amount:.2f} was traced FIFO into {len(allocations)} subsequent debit(s); {traced:.2f} was allocated and {remaining:.2f} remains untraced.",
        {
            "source_credit_txn_id": txn_id,
            "credited_amount": credited_amount,
            "pre_credit_balance": pre_credit_balance,
            "trace_status": status,
            "traced_amount": traced,
            "remaining_amount": remaining,
            "allocations": allocations,
        },
    )


def detect_requested_money_trails(
    connection: sqlite3.Connection,
    baseline: dict,
    config: AnalysisConfig,
    credit_txn_ids: list[str] | None = None,
) -> list:
    del baseline
    return [trace_credit(connection, txn_id, config) for txn_id in (credit_txn_ids or [])]
