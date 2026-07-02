# Copied from: analysis phase cidecode hackathon/analysis phase cidecode hackathon/analysis_engine/detectors/holding_accounts.py
"""Pattern 11: balance parking account.

Unlike Pattern 6 (accumulation from many sources with little outflow), this
pattern focuses on accounts that receive significant inflows and retain funds
over time — the balance grows steadily rather than being immediately transferred.
"""

from __future__ import annotations

import sqlite3

import pandas as pd

from ..config import AnalysisConfig
from ..database import fetch_transactions
from ..utils import safe_ratio
from .common import make_finding


def detect_holding_accounts(
    connection: sqlite3.Connection,
    baseline: dict,
    config: AnalysisConfig,
) -> list:
    thresholds = baseline["thresholds"]
    accounts = pd.read_sql_query("SELECT * FROM accounts", connection)
    findings = []

    for row in accounts.itertuples(index=False):
        total_credit = float(row.total_credit)
        total_debit = float(row.total_debit)

        if total_credit < thresholds["holding_credit_min"]:
            continue

        # Retention ratio: how much was kept vs received
        retention = safe_ratio(total_credit - total_debit, total_credit) if total_credit > config.money_epsilon else 0.0
        if retention < thresholds["holding_retention_min"]:
            continue

        # Check balance growth over time
        account_id = str(row.account_id)
        txns = fetch_transactions(
            connection,
            "eligible_for_detection = 1 AND account_id = ? AND date IS NOT NULL AND balance IS NOT NULL",
            (account_id,),
        )
        if len(txns) < 3:
            continue

        txns = txns.sort_values(["date", "time", "source_order", "row_id"])
        balances = txns["balance"].astype(float).tolist()

        # Check if balance generally trends upward (last balance > first)
        first_balance = balances[0]
        last_balance = balances[-1]
        if last_balance <= first_balance:
            continue

        balance_growth = last_balance - first_balance
        largest_credits = (
            txns[txns["credit_amount"] > config.money_epsilon]
            .sort_values(["credit_amount", "date", "source_order"], ascending=[False, True, True])
            .head(5)
        )
        largest_debits = (
            txns[txns["debit_amount"] > config.money_epsilon]
            .sort_values(["debit_amount", "date", "source_order"], ascending=[False, True, True])
            .head(5)
        )
        representative = pd.concat([largest_credits, largest_debits], ignore_index=True)
        txn_ids = representative["txn_id"].astype(str).drop_duplicates().tolist()

        findings.append(
            make_finding(
                connection,
                11,
                [account_id],
                txn_ids,
                f"Account {account_id} received {total_credit:.2f} in total credits "
                f"but only disbursed {total_debit:.2f}, retaining {retention:.0%} of inflows. "
                f"Balance grew from {first_balance:.2f} to {last_balance:.2f} (+{balance_growth:.2f}), "
                f"consistent with a holding/accumulation pattern.",
                {
                    "total_credit": total_credit,
                    "total_debit": total_debit,
                    "retention_ratio": retention,
                    "first_balance": first_balance,
                    "last_balance": last_balance,
                    "balance_growth": balance_growth,
                    "transaction_count": len(txns),
                    "reported_transaction_count": len(txn_ids),
                    "evidence_summarized": True,
                    "representative_transactions": [
                        {
                            "txn_id": str(item.txn_id),
                            "date": str(item.date),
                            "narration": str(item.narration or ""),
                            "debit_amount": float(item.debit_amount),
                            "credit_amount": float(item.credit_amount),
                            "balance": float(item.balance),
                        }
                        for item in representative.itertuples(index=False)
                    ],
                    "runtime_thresholds": {
                        "credit_min": thresholds["holding_credit_min"],
                        "retention_min": thresholds["holding_retention_min"],
                        "representative_credit_count": 5,
                        "representative_debit_count": 5,
                    },
                },
            )
        )
        if len(findings) >= config.maximum_findings_per_pattern:
            break

    return findings
