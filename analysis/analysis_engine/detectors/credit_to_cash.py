# Copied from: analysis phase cidecode hackathon/analysis phase cidecode hackathon/analysis_engine/detectors/credit_to_cash.py
"""Pattern 9: large credit followed by ATM/cash withdrawal chains.

Detects accounts receiving large incoming credits followed by rapid ATM
withdrawals or cash-outs within a short window.
"""

from __future__ import annotations

import re
import sqlite3

import pandas as pd

from ..config import AnalysisConfig
from ..database import fetch_transactions
from ..utils import normalize_text
from .common import make_finding

# Narration keywords that indicate ATM or cash withdrawal
CASH_WITHDRAWAL_RE = re.compile(
    r"\b(?:ATM|CASH\s*W(?:ITHDRAWAL|DL)|CASH\s*(?:DR|DEBIT)|SELF\s*WITHDRAWAL|"
    r"ATM\s*WDL|CASH\s*AT\s*ATM|CASHBACK|SELF\s*(?:DR|DEBIT))\b",
    re.IGNORECASE,
)


def _account_credit_threshold(ordered: pd.DataFrame, global_floor: float, config: AnalysisConfig) -> float:
    credit_rows = ordered[
        ~ordered["narration"].fillna("").apply(
            lambda value: any(
                token in normalize_text(value)
                for token in {"OPENING", "BALANCE FORWARD", "B/F", "BROUGHT FORWARD"}
            )
        )
    ]
    credits = pd.to_numeric(credit_rows["credit_amount"], errors="coerce")
    credits = credits[credits > config.money_epsilon]
    account_floor = float(credits.quantile(config.credit_to_cash_account_credit_quantile)) if not credits.empty else 0.0
    return max(float(global_floor or 0.0), account_floor)


def _is_regular_cash_habit(cash_debits: pd.DataFrame, config: AnalysisConfig) -> bool:
    if len(cash_debits) < config.credit_to_cash_recurring_min_occurrences:
        return False
    ordered = cash_debits.dropna(subset=["parsed_date"]).sort_values("parsed_date")
    if len(ordered) < config.credit_to_cash_recurring_min_occurrences:
        return False
    gaps = ordered["parsed_date"].diff().dt.days.dropna()
    gaps = gaps[gaps > 0]
    if len(gaps) < config.credit_to_cash_recurring_min_occurrences - 1:
        return False
    median_gap = float(gaps.median())
    if median_gap <= 0:
        return False
    tolerance = max(config.duplicate_recurring_gap_tolerance_days, median_gap * 0.25)
    regular = gaps[(gaps - median_gap).abs() <= tolerance]
    return len(regular) >= max(1, config.credit_to_cash_recurring_min_occurrences - 1)


def detect_credit_to_cash_chains(
    connection: sqlite3.Connection,
    baseline: dict,
    config: AnalysisConfig,
) -> list:
    thresholds = baseline["thresholds"]
    global_credit_min = thresholds["credit_to_cash_amount_min"]
    window_days = config.credit_to_cash_window_days

    frame = fetch_transactions(connection, "eligible_for_detection = 1 AND date IS NOT NULL")
    if frame.empty:
        return []

    frame["parsed_date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["amount"] = frame[["debit_amount", "credit_amount"]].max(axis=1)

    findings = []

    for account_id, group in frame.groupby("account_id", sort=False):
        ordered = group.sort_values(["parsed_date", "time", "source_order", "row_id"])

        credit_min = _account_credit_threshold(ordered, global_credit_min, config)

        # Find large credits
        non_opening_credit = ~ordered["narration"].fillna("").apply(
            lambda value: any(
                token in normalize_text(value)
                for token in {"OPENING", "BALANCE FORWARD", "B/F", "BROUGHT FORWARD"}
            )
        )
        large_credits = ordered[non_opening_credit & (ordered["credit_amount"] >= credit_min)]
        if large_credits.empty:
            continue

        # Find cash withdrawals by narration
        cash_mask = ordered["narration"].fillna("").apply(
            lambda n: bool(CASH_WITHDRAWAL_RE.search(normalize_text(n)))
        )
        cash_debits = ordered[cash_mask & (ordered["debit_amount"] > config.money_epsilon)]
        if cash_debits.empty:
            continue
        recurring_cash_habit = _is_regular_cash_habit(cash_debits, config)

        for credit_row in large_credits.itertuples(index=False):
            credit_date = credit_row.parsed_date
            if pd.isna(credit_date):
                continue

            # Find cash withdrawals within the window after this credit
            following_cash = cash_debits[
                (cash_debits["parsed_date"] >= credit_date)
                & ((cash_debits["parsed_date"] - credit_date).dt.days <= window_days)
                & (cash_debits["source_order"] > credit_row.source_order)
            ]
            if following_cash.empty:
                continue

            total_withdrawn = float(following_cash["debit_amount"].sum())
            withdrawal_ratio = total_withdrawn / max(float(credit_row.credit_amount), config.money_epsilon)

            # Only flag if a meaningful portion was withdrawn as cash
            if withdrawal_ratio < 0.30:
                continue
            recurring_exclusion_applied = (
                recurring_cash_habit
                and withdrawal_ratio < config.credit_to_cash_high_ratio_override
            )
            if recurring_exclusion_applied:
                continue

            txn_ids = [str(credit_row.txn_id)] + following_cash["txn_id"].astype(str).tolist()
            duration = int((following_cash["parsed_date"].max() - credit_date).days)

            findings.append(
                make_finding(
                    connection,
                    9,
                    [str(account_id)],
                    txn_ids,
                    f"Account {account_id} received a credit of {credit_row.credit_amount:.2f} "
                    f"followed by {len(following_cash)} ATM/cash withdrawal(s) totalling "
                    f"{total_withdrawn:.2f} ({withdrawal_ratio:.0%} of credit) within {duration} day(s).",
                    {
                        "credit_amount": float(credit_row.credit_amount),
                        "total_cash_withdrawn": total_withdrawn,
                        "withdrawal_ratio": withdrawal_ratio,
                        "withdrawal_count": len(following_cash),
                        "duration_days": duration,
                        "withdrawal_amounts": following_cash["debit_amount"].astype(float).tolist(),
                        "runtime_thresholds": {
                            "credit_min": credit_min,
                            "global_credit_min": global_credit_min,
                            "account_credit_quantile": config.credit_to_cash_account_credit_quantile,
                            "window_days": window_days,
                            "recurring_cash_habit_detected": recurring_cash_habit,
                            "recurring_cash_habit_excluded": recurring_exclusion_applied,
                            "high_ratio_override": config.credit_to_cash_high_ratio_override,
                        },
                    },
                )
            )
            if len(findings) >= config.maximum_findings_per_pattern:
                return findings

    return findings
