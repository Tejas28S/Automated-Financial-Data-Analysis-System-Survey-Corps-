"""Pattern 13: cross-statement money flow links.

Links transactions across different bank statements (doc_ids) to reconstruct
complete fund movement chains that span multiple documents.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict

import pandas as pd

from ..config import AnalysisConfig
from ..database import fetch_transactions
from ..utils import normalize_compact
from .common import make_finding


def detect_cross_statement_flows(
    connection: sqlite3.Connection,
    baseline: dict,
    config: AnalysisConfig,
) -> list:
    del baseline
    frame = fetch_transactions(
        connection,
        "eligible_for_detection = 1 AND date IS NOT NULL AND COALESCE(doc_id, '') != ''",
    )
    if frame.empty:
        return []

    # We need at least 2 different doc_ids to have cross-statement flows
    unique_docs = frame["doc_id"].nunique()
    if unique_docs < 2:
        return []

    # Group by date and look for amount-matched debit-credit pairs across documents
    frame["amount"] = frame[["debit_amount", "credit_amount"]].max(axis=1)
    frame["direction"] = frame.apply(
        lambda r: "debit" if float(r["debit_amount"]) > config.money_epsilon else "credit",
        axis=1,
    )

    findings = []
    seen_pairs: set[tuple[str, str]] = set()

    for transaction_date, date_group in frame.groupby("date", sort=True):
        debits = date_group[date_group["direction"] == "debit"]
        credits = date_group[date_group["direction"] == "credit"]

        for debit in debits.itertuples(index=False):
            for credit in credits.itertuples(index=False):
                # Must be from different documents
                if str(debit.doc_id) == str(credit.doc_id):
                    continue
                # Must be different accounts
                if str(debit.account_id) == str(credit.account_id):
                    continue

                pair_key = tuple(sorted((str(debit.txn_id), str(credit.txn_id))))
                if pair_key in seen_pairs:
                    continue

                # Amount match
                max_amt = max(float(debit.debit_amount), float(credit.credit_amount), config.money_epsilon)
                diff_ratio = abs(float(debit.debit_amount) - float(credit.credit_amount)) / max_amt
                if diff_ratio > config.duplicate_amount_relative_tolerance:
                    continue

                # Corroborating evidence: reference match or counterparty link
                ref_match = bool(
                    debit.reference and credit.reference
                    and normalize_compact(debit.reference) == normalize_compact(credit.reference)
                )
                counterparty_link = bool(
                    (hasattr(debit, 'counterparty_account')
                     and str(getattr(debit, 'counterparty_account', '') or '') == str(credit.account_id))
                    or (hasattr(credit, 'counterparty_account')
                        and str(getattr(credit, 'counterparty_account', '') or '') == str(debit.account_id))
                )

                if not (ref_match or counterparty_link):
                    continue

                seen_pairs.add(pair_key)
                findings.append(
                    make_finding(
                        connection,
                        13,
                        [str(debit.account_id), str(credit.account_id)],
                        list(pair_key),
                        f"Cross-statement link: debit of {debit.debit_amount:.2f} in document "
                        f"'{debit.doc_id}' matches credit of {credit.credit_amount:.2f} in document "
                        f"'{credit.doc_id}' on {transaction_date}, linking fund flow across statements.",
                        {
                            "source_document": str(debit.doc_id),
                            "target_document": str(credit.doc_id),
                            "debit_account": str(debit.account_id),
                            "credit_account": str(credit.account_id),
                            "debit_amount": float(debit.debit_amount),
                            "credit_amount": float(credit.credit_amount),
                            "date": str(transaction_date),
                            "reference_match": ref_match,
                            "counterparty_link": counterparty_link,
                        },
                    )
                )
                if len(findings) >= config.maximum_findings_per_pattern:
                    return findings

    return findings
