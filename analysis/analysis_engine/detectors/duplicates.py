# Copied from: analysis phase cidecode hackathon/analysis phase cidecode hackathon/analysis_engine/detectors/duplicates.py
"""Pattern 1: verification of extraction's duplicates.csv bucket.

This is a data-integrity reconciliation check, not standalone fraud evidence.
It confirms extraction duplicates, finds missed duplicate-like pairs, and flags
possible extraction false positives.
"""

from __future__ import annotations

from difflib import SequenceMatcher
import sqlite3

import pandas as pd

from ..config import AnalysisConfig
from ..database import fetch_transactions
from ..utils import normalize_text
from .common import make_finding


def _similar(left: str, right: str) -> float:
    return SequenceMatcher(None, normalize_text(left), normalize_text(right)).ratio()


def _is_recurring_payment(
    account_history: pd.DataFrame,
    current: object,
    previous: object,
    config: AnalysisConfig,
) -> bool:
    counterparty = str(getattr(current, "counterparty_account", "") or getattr(previous, "counterparty_account", "") or "")
    if not counterparty:
        return False
    amount = float(getattr(current, "amount", 0.0) or 0.0)
    candidates = account_history[
        (account_history["counterparty_account"].fillna("").astype(str) == counterparty)
        & ((account_history["amount"] - amount).abs() <= max(config.money_epsilon, amount * config.duplicate_amount_relative_tolerance))
    ].copy()
    if len(candidates) < config.duplicate_recurring_min_occurrences:
        return False
    candidates = candidates.sort_values("parsed_date")
    gaps = candidates["parsed_date"].diff().dt.days.dropna()
    regular_gaps = gaps[(gaps >= 25) & (gaps <= 35)]
    return len(regular_gaps) >= max(1, config.duplicate_recurring_min_occurrences - 1)


def detect_duplicate_cross_check(
    connection: sqlite3.Connection,
    baseline: dict,
    config: AnalysisConfig,
) -> list:
    del baseline  # Pattern 1 uses configurable universal identity tolerances.
    frame = fetch_transactions(connection)
    frame = frame[frame["date"].notna()].copy()
    frame["parsed_date"] = pd.to_datetime(frame["date"])
    frame["amount"] = frame[["debit_amount", "credit_amount"]].max(axis=1)
    frame["direction"] = frame.apply(
        lambda row: "debit" if row["debit_amount"] > config.money_epsilon else "credit",
        axis=1,
    )
    frame["amount_bucket"] = (frame["amount"] * 100).round().astype(int)

    extraction_flagged = set(
        frame.loc[
            (frame["source_bucket"] == "duplicate")
            | frame["duplicate_of_txn_id"].fillna("").ne(""),
            "txn_id",
        ].astype(str)
    )
    confirmed_flags: set[str] = set()
    findings = []
    seen_pairs: set[tuple[str, str]] = set()

    for (_, _, _), group in frame.groupby(
        ["account_id", "direction", "amount_bucket"], sort=False, dropna=False
    ):
        ordered = group.sort_values(["parsed_date", "time", "source_order", "row_id"])
        rows = list(ordered.itertuples(index=False))
        for current_index, current in enumerate(rows):
            for previous in reversed(rows[:current_index]):
                date_difference = (current.parsed_date - previous.parsed_date).days
                if date_difference > config.duplicate_date_window_days:
                    break
                if date_difference < 0:
                    continue
                maximum = max(float(current.amount), float(previous.amount), config.money_epsilon)
                amount_difference_ratio = abs(float(current.amount) - float(previous.amount)) / maximum
                if amount_difference_ratio > config.duplicate_amount_relative_tolerance:
                    continue
                narration_similarity = _similar(current.narration, previous.narration)
                same_reference = bool(
                    current.reference
                    and previous.reference
                    and normalize_text(current.reference) == normalize_text(previous.reference)
                )
                same_counterparty = bool(
                    current.counterparty_account
                    and previous.counterparty_account
                    and current.counterparty_account == previous.counterparty_account
                )
                signal_count = sum([
                    bool(same_reference),
                    bool(narration_similarity >= config.duplicate_narration_similarity),
                    bool(same_counterparty and date_difference == 0),
                ])
                if signal_count < 2:
                    continue
                if _is_recurring_payment(ordered, current, previous, config):
                    continue
                pair = tuple(sorted((str(previous.txn_id), str(current.txn_id))))
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                flagged_in_pair = extraction_flagged.intersection(pair)
                category = "confirmed_extraction_duplicate" if flagged_in_pair else "missed_by_extraction"
                confirmed_flags.update(flagged_in_pair)
                explanation = (
                    f"Transactions {pair[0]} and {pair[1]} share an account, direction, "
                    f"near-equal amount, nearby date, and corroborating narration/reference evidence. "
                    + (
                        "This independently confirms the extraction duplicate flag."
                        if flagged_in_pair
                        else "Extraction did not place either row in the duplicate bucket."
                    )
                )
                findings.append(
                    make_finding(
                        connection,
                        1,
                        [str(current.account_id)],
                        list(pair),
                        explanation,
                        {
                            "reconciliation_category": category,
                            "date_difference_days": date_difference,
                            "amount_difference_ratio": amount_difference_ratio,
                            "narration_similarity": narration_similarity,
                            "same_reference": same_reference,
                            "same_counterparty": same_counterparty,
                            "signal_count": signal_count,
                            "recurring_payment_exclusion_checked": True,
                            "runtime_thresholds": {
                                "required_signal_count": 2,
                                "date_window_days": config.duplicate_date_window_days,
                                "amount_relative_tolerance": config.duplicate_amount_relative_tolerance,
                                "narration_similarity_min": config.duplicate_narration_similarity,
                                "recurring_min_occurrences": config.duplicate_recurring_min_occurrences,
                            },
                        },
                    )
                )
                if len(findings) >= config.maximum_findings_per_pattern:
                    return findings

    for txn_id in sorted(extraction_flagged - confirmed_flags):
        row = frame[frame["txn_id"] == txn_id]
        if row.empty:
            continue
        item = row.iloc[0]
        findings.append(
            make_finding(
                connection,
                1,
                [str(item["account_id"])],
                [txn_id],
                "Extraction flagged this row as a duplicate, but the independent account/amount/date/narration cross-check found no corroborating match.",
                {"reconciliation_category": "possible_extraction_false_positive"},
            )
        )
    if not findings:
        findings.append(
            make_finding(
                connection,
                1,
                [],
                [],
                "No duplicates detected; extraction's duplicate bucket is empty and independently confirmed clean."
                if not extraction_flagged
                else "Extraction's duplicate bucket had entries, but the tightened independent check corroborated none of them.",
                {
                    "reconciliation_category": "no_duplicates_confirmed"
                    if not extraction_flagged else "possible_extraction_false_positive_summary",
                    "extraction_duplicate_count": len(extraction_flagged),
                    "independent_duplicate_pair_count": 0,
                    "runtime_thresholds": {
                        "required_signal_count": 2,
                        "date_window_days": config.duplicate_date_window_days,
                        "amount_relative_tolerance": config.duplicate_amount_relative_tolerance,
                        "narration_similarity_min": config.duplicate_narration_similarity,
                    },
                },
            )
        )
    return findings
