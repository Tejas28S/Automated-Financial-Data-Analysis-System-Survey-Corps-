# Copied from: analysis phase cidecode hackathon/analysis phase cidecode hackathon/analysis_engine/detectors/cross_statement.py
"""Pattern 10: matched money flow links across or within statements.

Links debit/credit pairs between different source accounts. Pairs may be in
different documents or in the same document; `same_document` is recorded.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict

import pandas as pd

from ..config import AnalysisConfig
from ..database import fetch_transactions
from .common import (
    corroboration_path_for_match,
    exact_reference_or_vpa_match,
    make_finding,
    narration_similarity_details,
)


def detect_cross_statement_flows(
    connection: sqlite3.Connection,
    baseline: dict,
    config: AnalysisConfig,
) -> list:
    frame = fetch_transactions(
        connection,
        "eligible_for_detection = 1 AND date IS NOT NULL AND COALESCE(doc_id, '') != ''",
    )
    if frame.empty:
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

                debit_row = debit._asdict()
                credit_row = credit._asdict()
                ref_match, reference_details = exact_reference_or_vpa_match(debit_row, credit_row)
                narration_details = narration_similarity_details(debit.narration, credit.narration)
                narration_match = (
                    narration_details["narration_similarity_score"] >= config.narration_corrob_min_similarity
                    and bool(narration_details["shared_narration_tokens"])
                )
                debit_method = str(getattr(debit, "counterparty_resolution_method", "") or "")
                credit_method = str(getattr(credit, "counterparty_resolution_method", "") or "")
                counterparty_link = bool(
                    (
                        debit_method != "amount_date_mirror_match"
                        and str(getattr(debit, "counterparty_account", "") or "") == str(credit.account_id)
                    )
                    or (
                        credit_method != "amount_date_mirror_match"
                        and str(getattr(credit, "counterparty_account", "") or "") == str(debit.account_id)
                    )
                )

                confidence = min(
                    float(getattr(debit, "counterparty_resolution_confidence", 0.85) or 0.85),
                    float(getattr(credit, "counterparty_resolution_confidence", 0.85) or 0.85),
                )
                methods = sorted({
                    debit_method,
                    credit_method,
                })

                if not (ref_match or narration_match or counterparty_link):
                    continue

                allowed, corroboration_path, guard_details = corroboration_path_for_match(
                    amount=float(debit.debit_amount),
                    methods=methods,
                    confidence=confidence,
                    reference_match=ref_match,
                    narration_match=narration_match,
                    strict_counterparty_link=counterparty_link,
                    baseline=baseline,
                    config=config,
                )
                if not allowed:
                    continue

                seen_pairs.add(pair_key)
                same_document = str(debit.doc_id) == str(credit.doc_id)
                link_scope = "intra_statement_link" if same_document else "cross_document_link"
                evidence_strength = "strong" if (not same_document and corroboration_path == "reference_match") else "weak"
                findings.append(
                    make_finding(
                        connection,
                        10,
                        [str(debit.account_id), str(credit.account_id)],
                        list(pair_key),
                        f"Matched flow link: debit of {debit.debit_amount:.2f} from {debit.account_id} "
                        f"matches credit of {credit.credit_amount:.2f} to {credit.account_id} "
                        f"on {transaction_date}.",
                        {
                            "source_document": str(debit.doc_id),
                            "target_document": str(credit.doc_id),
                            "same_document": same_document,
                            "link_scope": link_scope,
                            "debit_account": str(debit.account_id),
                            "credit_account": str(credit.account_id),
                            "debit_amount": float(debit.debit_amount),
                            "credit_amount": float(credit.credit_amount),
                            "date": str(transaction_date),
                            "reference_match": ref_match,
                            "reference_match_details": reference_details,
                            "narration_match": narration_match,
                            "narration_match_details": narration_details,
                            "counterparty_link": counterparty_link,
                            "counterparty_resolution_confidence": confidence,
                            "counterparty_resolution_methods": methods,
                            "corroboration_path": corroboration_path,
                            "amount_commonality_guard": guard_details,
                        },
                        evidence_strength=evidence_strength,
                        config=config,
                    )
                )
                if len(findings) >= config.maximum_findings_per_pattern:
                    return findings

    return findings
