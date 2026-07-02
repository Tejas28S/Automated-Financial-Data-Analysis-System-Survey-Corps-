"""Pattern 14: large credit followed by ATM/cash withdrawal chains.

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


def detect_credit_to_cash_chains(
    connection: sqlite3.Connection,
    baseline: dict,
    config: AnalysisConfig,
) -> list:
    thresholds = baseline["thresholds"]
    credit_min = thresholds["credit_to_cash_amount_min"]
    window_days = config.credit_to_cash_window_days

    frame = fetch_transactions(connection, "eligible_for_detection = 1 AND date IS NOT NULL")
    if frame.empty:
        return []

    frame["parsed_date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["amount"] = frame[["debit_amount", "credit_amount"]].max(axis=1)

    findings = []

    for account_id, group in frame.groupby("account_id", sort=False):
        ordered = group.sort_values(["parsed_date", "time", "source_order", "row_id"])

        # Find large credits
        large_credits = ordered[ordered["credit_amount"] >= credit_min]
        if large_credits.empty:
            continue

        # Find cash withdrawals by narration
        cash_mask = ordered["narration"].fillna("").apply(
            lambda n: bool(CASH_WITHDRAWAL_RE.search(normalize_text(n)))
        )
        cash_debits = ordered[cash_mask & (ordered["debit_amount"] > config.money_epsilon)]
        if cash_debits.empty:
            continue

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

            txn_ids = [str(credit_row.txn_id)] + following_cash["txn_id"].astype(str).tolist()
            duration = int((following_cash["parsed_date"].max() - credit_date).days)

            findings.append(
                make_finding(
                    connection,
                    14,
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
                            "window_days": window_days,
                        },
                    },
                )
            )
            if len(findings) >= config.maximum_findings_per_pattern:
                return findings

    return findings
