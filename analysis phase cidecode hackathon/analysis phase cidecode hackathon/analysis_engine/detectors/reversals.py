"""Pattern 2: debit-credit reversals detected without trusting source labels."""

from __future__ import annotations

from difflib import SequenceMatcher
import re
import sqlite3

import pandas as pd

from ..config import AnalysisConfig
from ..database import fetch_transactions
from ..utils import normalize_text
from .common import make_finding


REVERSAL_EVIDENCE_RE = re.compile(
    r"\b(?:REVERSAL|REVERSED|FAILED|FAILURE|RETURN|RETURNED|REFUND|BOUNCE|DISHONOUR)\b",
    re.IGNORECASE,
)
NUMBER_TOKEN_RE = re.compile(r"\d{6,}")


def detect_reversals(
    connection: sqlite3.Connection,
    baseline: dict,
    config: AnalysisConfig,
) -> list:
    frame = fetch_transactions(connection, "eligible_for_detection = 1 AND date IS NOT NULL")
    frame["parsed_date"] = pd.to_datetime(frame["date"])
    window_days = int(baseline["thresholds"]["reversal_window_days"])
    findings = []

    for account_id, group in frame.groupby("account_id", sort=False):
        ordered = group.sort_values(["parsed_date", "time", "source_order", "row_id"])
        debits = ordered[ordered["debit_amount"] > config.money_epsilon]
        credits = ordered[ordered["credit_amount"] > config.money_epsilon]
        for credit in credits.itertuples(index=False):
            candidates = debits[
                (debits["parsed_date"] <= credit.parsed_date)
                & ((credit.parsed_date - debits["parsed_date"]).dt.days <= window_days)
                & (debits["source_order"] < credit.source_order)
            ].copy()
            if candidates.empty:
                continue
            candidates["relative_difference"] = (
                (candidates["debit_amount"] - credit.credit_amount).abs()
                / candidates[["debit_amount"]].assign(credit=float(credit.credit_amount)).max(axis=1)
            )
            candidates = candidates[
                candidates["relative_difference"] <= config.reversal_amount_relative_tolerance
            ]
            if candidates.empty:
                continue

            chosen = None
            chosen_evidence = None
            for debit in candidates.sort_values(["parsed_date", "source_order"], ascending=False).itertuples(index=False):
                credit_text = normalize_text(credit.narration)
                debit_text = normalize_text(debit.narration)
                credit_numbers = set(NUMBER_TOKEN_RE.findall(credit_text))
                debit_numbers = set(NUMBER_TOKEN_RE.findall(debit_text))
                shared_number = bool(credit_numbers.intersection(debit_numbers))
                same_reference = bool(
                    credit.reference and debit.reference
                    and normalize_text(credit.reference) == normalize_text(debit.reference)
                )
                keyword = bool(REVERSAL_EVIDENCE_RE.search(credit_text))
                similarity = SequenceMatcher(None, credit_text, debit_text).ratio()
                same_counterparty = bool(
                    credit.counterparty_account and debit.counterparty_account
                    and credit.counterparty_account == debit.counterparty_account
                )
                if keyword and (shared_number or same_reference or same_counterparty or similarity >= config.reversal_narration_similarity):
                    chosen = debit
                    chosen_evidence = {
                        "keyword_evidence": keyword,
                        "shared_reference_number": shared_number,
                        "same_reference": same_reference,
                        "same_counterparty": same_counterparty,
                        "narration_similarity": similarity,
                    }
                    break
            if chosen is None:
                continue
            date_difference = (credit.parsed_date - chosen.parsed_date).days
            findings.append(
                make_finding(
                    connection,
                    2,
                    [str(account_id)],
                    [str(chosen.txn_id), str(credit.txn_id)],
                    f"Debit {chosen.txn_id} is followed {date_difference} day(s) later by near-equal credit {credit.txn_id} with explicit reversal/reference evidence.",
                    {
                        "debit_amount": float(chosen.debit_amount),
                        "credit_amount": float(credit.credit_amount),
                        "date_difference_days": date_difference,
                        "source_label_cross_check": bool(credit.is_reversed_source_label),
                        **(chosen_evidence or {}),
                    },
                )
            )
            if len(findings) >= config.maximum_findings_per_pattern:
                return findings
    return findings
