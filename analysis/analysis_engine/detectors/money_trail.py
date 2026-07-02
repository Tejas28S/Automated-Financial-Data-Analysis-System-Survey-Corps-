# Copied from: analysis phase cidecode hackathon/analysis phase cidecode hackathon/analysis_engine/detectors/money_trail.py
"""Pattern 8: FIFO tracing of significant or investigator-requested credits."""

from __future__ import annotations

import sqlite3

import pandas as pd

from ..config import AnalysisConfig
from ..utils import normalize_text
from .common import is_high_frequency_amount, make_finding, summarize_txn_ids


ORDINARY_SPENDING_TOKENS = {
    "ATM",
    "CASH",
    "POS",
    "MAB",
    "CHARGES",
    "GST",
    "INTEREST",
    "BILL",
    "RECHARGE",
    "FUEL",
}


def trace_credit(
    connection: sqlite3.Connection,
    txn_id: str,
    config: AnalysisConfig,
    *,
    trigger_reason: str = "manual",
    gated_reason: str = "manual_investigator_requested_credit",
    significant_credit_threshold: float | None = None,
    strong_account_credit_floor: float | None = None,
    suspicious_txn_ids: set[str] | None = None,
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
    suspicious_txn_ids = {str(value) for value in (suspicious_txn_ids or set()) if str(value)}
    source_credit_context = {
        "txn_id": txn_id,
        "date": str(credit_row["date"] or ""),
        "narration": str(credit_row["narration"] or ""),
        "counterparty_account": str(credit_row["counterparty_account"] or ""),
        "counterparty_name_raw": str(credit_row["counterparty_name_raw"] or ""),
        "counterparty_resolution_method": str(credit_row["counterparty_resolution_method"] or ""),
        "counterparty_resolution_confidence": float(credit_row["counterparty_resolution_confidence"] or 0.0),
    }

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
                "narration": str(row.narration or ""),
                "counterparty_account": str(row.counterparty_account or ""),
                "counterparty_name_raw": str(row.counterparty_name_raw or ""),
                "counterparty_resolution_method": str(row.counterparty_resolution_method or ""),
                "counterparty_resolution_confidence": float(row.counterparty_resolution_confidence or 0.0),
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
    allocation_txn_ids = [str(item["debit_txn_id"]) for item in allocations]
    suspicious_overlap = sorted(set(allocation_txn_ids).intersection(suspicious_txn_ids))
    ordinary_count = sum(
        1
        for item in allocations
        if any(token in normalize_text(item.get("narration", "")) for token in ORDINARY_SPENDING_TOKENS)
    )
    ordinary_ratio = ordinary_count / len(allocations) if allocations else 0.0
    long_chain = len(allocations) > config.money_trail_long_chain_debit_count
    chain_specificity = (
        "overlaps_other_patterns"
        if suspicious_overlap
        else "ordinary_spending_like"
        if ordinary_ratio >= 0.75
        else "long_low_specificity_chain"
        if long_chain
        else "specific_follow_on_debits"
    )
    evidence_strength = "strong"
    if chain_specificity in {"ordinary_spending_like", "long_low_specificity_chain"}:
        evidence_strength = "weak"
    chain_confidence_score = 0.65 if evidence_strength == "weak" else 1.0

    important = [txn_id] + suspicious_overlap
    reported_txn_ids, txn_summary = summarize_txn_ids(
        consumed_txn_ids,
        important_txn_ids=important,
        limit=config.money_trail_reported_txn_id_limit,
    )
    if len(allocations) > config.money_trail_reported_txn_id_limit:
        important_allocations = [
            item
            for item in allocations
            if str(item.get("debit_txn_id", "")) in set(suspicious_overlap)
        ]
        reported_allocations = list(
            {
                str(item.get("debit_txn_id", "")): item
                for item in (important_allocations + allocations[: config.money_trail_reported_txn_id_limit])
            }.values()
        )[: config.money_trail_reported_txn_id_limit]
    else:
        reported_allocations = allocations
    return make_finding(
        connection,
        8,
        [str(credit_row["account_id"])],
        reported_txn_ids,
        f"Credit {txn_id} of {credited_amount:.2f} was traced FIFO into {len(allocations)} subsequent debit(s); {traced:.2f} was allocated and {remaining:.2f} remains untraced.",
        {
            "source_credit_txn_id": txn_id,
            "trigger_reason": trigger_reason,
            "gated_reason": gated_reason,
            "credited_amount": credited_amount,
            "source_credit_context": source_credit_context,
            "pre_credit_balance": pre_credit_balance,
            "trace_status": status,
            "traced_amount": traced,
            "remaining_amount": remaining,
            "allocations": reported_allocations,
            "allocation_summary": {
                "allocation_count": len(allocations),
                "reported_allocation_count": len(reported_allocations),
                "allocations_truncated": len(reported_allocations) < len(allocations),
                "ordinary_spending_like_debit_count": ordinary_count,
                "ordinary_spending_like_ratio": ordinary_ratio,
                "suspicious_overlap_txn_ids": suspicious_overlap[: config.money_trail_reported_txn_id_limit],
                "chain_specificity": chain_specificity,
                **txn_summary,
            },
            "confidence_score": chain_confidence_score,
            "runtime_thresholds": {
                "significant_credit_threshold": significant_credit_threshold,
                "strong_account_credit_floor": strong_account_credit_floor,
                "maximum_trace_debits": config.maximum_trace_debits,
                "reported_txn_id_limit": config.money_trail_reported_txn_id_limit,
                "long_chain_debit_count": config.money_trail_long_chain_debit_count,
            },
        },
        evidence_strength=evidence_strength,
    )


def detect_requested_money_trails(
    connection: sqlite3.Connection,
    baseline: dict,
    config: AnalysisConfig,
    credit_txn_ids: list[str] | None = None,
) -> list:
    del baseline
    return [trace_credit(connection, txn_id, config) for txn_id in (credit_txn_ids or [])]


def detect_auto_money_trails(
    connection: sqlite3.Connection,
    baseline: dict,
    config: AnalysisConfig,
    strong_account_ids: set[str] | None = None,
    manual_credit_txn_ids: list[str] | None = None,
    suspicious_txn_ids: set[str] | None = None,
) -> list:
    strong_account_ids = {str(value) for value in (strong_account_ids or set()) if str(value)}
    manual_credit_txn_ids = list(dict.fromkeys(str(value) for value in (manual_credit_txn_ids or []) if str(value)))
    threshold = float(
        baseline.get("thresholds", {}).get("money_trail_extreme_credit_min")
        or baseline.get("transaction_amounts", {}).get("p95")
        or 0.0
    )
    account_credit_floors = {
        str(account_id): float(value or 0.0)
        for account_id, value in (
            baseline.get("account_credit_floor_strong", {})
            or baseline.get("account_credit_floor_p40", {})
            or {}
        ).items()
    }
    frame = pd.read_sql_query(
        """
        SELECT txn_id, account_id, credit_amount, narration
        FROM transactions
        WHERE eligible_for_detection = 1
          AND credit_amount > ?
        ORDER BY credit_amount DESC, source_order, row_id
        """,
        connection,
        params=(config.money_epsilon,),
    )
    selected: dict[str, tuple[str, str, float | None]] = {}
    suspicious_txn_ids = {str(value) for value in (suspicious_txn_ids or set()) if str(value)}
    for row in frame.itertuples(index=False):
        txn_id = str(row.txn_id)
        account_id = str(row.account_id)
        credit_amount = float(row.credit_amount)
        narration = normalize_text(getattr(row, "narration", ""))
        if any(token in narration for token in {"OPENING", "BALANCE FORWARD", "B/F", "BROUGHT FORWARD"}):
            continue
        if account_id in strong_account_ids:
            floor = account_credit_floors.get(account_id, 0.0)
            if credit_amount >= floor and txn_id in suspicious_txn_ids:
                selected[txn_id] = (
                    "auto_strong_account_credit",
                    "credit_txn_seen_in_prior_pattern_on_strong_account",
                    floor,
                )
        if credit_amount >= threshold and (
            txn_id in suspicious_txn_ids
            or not is_high_frequency_amount(credit_amount, baseline)
        ):
            selected.setdefault(
                txn_id,
                (
                    "auto_extreme_credit",
                    "extreme_credit_with_prior_pattern_overlap"
                    if txn_id in suspicious_txn_ids
                    else "extreme_credit_with_rare_amount",
                    None,
                ),
            )
        if len(selected) >= config.money_trail_auto_max_credits:
            break
    for txn_id in manual_credit_txn_ids:
        selected[txn_id] = ("manual", "manual_investigator_requested_credit", None)

    findings = []
    for txn_id, (reason, gated_reason, floor) in selected.items():
        try:
            findings.append(
                trace_credit(
                    connection,
                    txn_id,
                    config,
                    trigger_reason=reason,
                    gated_reason=gated_reason,
                    significant_credit_threshold=threshold,
                    strong_account_credit_floor=floor,
                    suspicious_txn_ids=suspicious_txn_ids,
                )
            )
        except ValueError:
            continue
    if not findings:
        findings.append(
            make_finding(
                connection,
                8,
                [],
                [],
                "No money-trail traces were triggered because no eligible credit met the runtime significance threshold and no manual credit was requested.",
                {
                    "trigger_status": "not_triggered",
                    "runtime_thresholds": {
                        "significant_credit_threshold": threshold,
                        "account_credit_floor_strong": account_credit_floors,
                        "strong_account_credit_quantile": config.money_trail_strong_account_credit_quantile,
                        "extreme_credit_quantile": config.money_trail_strict_credit_quantile,
                        "auto_max_credits": config.money_trail_auto_max_credits,
                        "auto_trigger_rule": "prior-detector suspicious strong-account credit floor or extreme runtime credit percentile",
                    },
                },
            )
        )
    return findings
