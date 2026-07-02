# Copied from: analysis phase cidecode hackathon/analysis phase cidecode hackathon/analysis_engine/detectors/internal_flow_hub.py
"""Pattern 12: internal debit/credit pairs matched across accounts.

Finds matched debit→credit pairs between different observed accounts using
amount, date, reference number, and counterparty relationships — revealing
internal fund routing that may indicate layering.
"""

from __future__ import annotations

import sqlite3

import pandas as pd

from ..config import AnalysisConfig
from ..database import fetch_transactions
from ..utils import normalize_compact
from .common import make_finding


def detect_matched_internal_flow_hub(
    connection: sqlite3.Connection,
    baseline: dict,
    config: AnalysisConfig,
) -> list:
    del baseline  # Uses config tolerances directly.
    frame = fetch_transactions(connection, "eligible_for_detection = 1 AND date IS NOT NULL")

    if frame.empty:
        return []

    # Get set of observed (internal) account IDs
    accounts = pd.read_sql_query("SELECT account_id FROM accounts", connection)
    observed = set(accounts["account_id"].astype(str))

    # Split into debits and credits
    debits = frame[frame["debit_amount"] > config.money_epsilon].copy()
    credits = frame[frame["credit_amount"] > config.money_epsilon].copy()

    if debits.empty or credits.empty:
        return []

    findings = []
    seen_pairs: set[tuple[str, str]] = set()

    for debit in debits.itertuples(index=False):
        debit_acct = str(debit.account_id)
        if debit_acct not in observed:
            continue

        # Find matching credits: same date, different account, similar amount
        matching = credits[
            (credits["date"] == debit.date)
            & (credits["account_id"] != debit_acct)
            & (credits["account_id"].astype(str).isin(observed))
        ]
        if matching.empty:
            continue

        for credit in matching.itertuples(index=False):
            credit_acct = str(credit.account_id)
            pair_key = tuple(sorted((str(debit.txn_id), str(credit.txn_id))))
            if pair_key in seen_pairs:
                continue

            # Amount match check
            max_amt = max(float(debit.debit_amount), float(credit.credit_amount), config.money_epsilon)
            diff_ratio = abs(float(debit.debit_amount) - float(credit.credit_amount)) / max_amt
            if diff_ratio > config.duplicate_amount_relative_tolerance:
                continue

            # Corroborating evidence: reference match, counterparty match, or narration link
            ref_match = bool(
                debit.reference and credit.reference
                and normalize_compact(debit.reference) == normalize_compact(credit.reference)
            )
            counterparty_match = bool(
                (hasattr(debit, 'counterparty_account') and debit.counterparty_account == credit_acct)
                or (hasattr(credit, 'counterparty_account') and credit.counterparty_account == debit_acct)
            )

            if not (ref_match or counterparty_match):
                continue

            seen_pairs.add(pair_key)
            findings.append(
                make_finding(
                    connection,
                    16,
                    [debit_acct, credit_acct],
                    list(pair_key),
                    f"Debit of {debit.debit_amount:.2f} from {debit_acct} on {debit.date} "
                    f"matches credit of {credit.credit_amount:.2f} to {credit_acct} — "
                    f"internal transfer confirmed by {'reference' if ref_match else 'counterparty'} evidence.",
                    {
                        "debit_account": debit_acct,
                        "credit_account": credit_acct,
                        "debit_amount": float(debit.debit_amount),
                        "credit_amount": float(credit.credit_amount),
                        "amount_difference_ratio": diff_ratio,
                        "date": str(debit.date),
                        "reference_match": ref_match,
                        "counterparty_match": counterparty_match,
                    },
                )
            )
            if len(findings) >= config.maximum_findings_per_pattern:
                return findings

    return findings
