# Copied from: analysis phase cidecode hackathon/analysis phase cidecode hackathon/analysis_engine/detectors/low_value_testing.py
"""Pattern 13: low-value reciprocal account testing.

Detects very small-value transfers (₹1, ₹2, etc.) exchanged between accounts
to test whether accounts are active before larger transfers commence.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict

import networkx as nx
import pandas as pd

from ..config import AnalysisConfig
from ..database import fetch_transactions
from ..utils import canonical_account_id
from .common import make_finding


def detect_low_value_testing(
    connection: sqlite3.Connection,
    baseline: dict,
    graph: nx.MultiDiGraph,
    config: AnalysisConfig,
) -> list:
    del baseline
    frame = fetch_transactions(connection, "eligible_for_detection = 1 AND date IS NOT NULL")
    if frame.empty:
        return []

    frame["amount"] = frame[["debit_amount", "credit_amount"]].max(axis=1)
    frame["parsed_date"] = pd.to_datetime(frame["date"], errors="coerce")

    # Filter to very small amounts
    small_txns = frame[
        (frame["amount"] > 0)
        & (frame["amount"] <= config.low_value_test_max_amount)
    ].copy()

    if small_txns.empty:
        return []

    # Look for reciprocal small-value transfers between account pairs
    # A reciprocal pair: A debits to B, then B debits to A (or vice versa via counterparty)
    pair_txns: dict[tuple[str, str], list[dict]] = defaultdict(list)

    for row in small_txns.itertuples(index=False):
        counterparty = canonical_account_id(getattr(row, 'counterparty_account', ''))
        account = canonical_account_id(row.account_id)
        if not counterparty or counterparty == account:
            continue

        pair_key = tuple(sorted((account, counterparty)))
        direction = "debit" if float(row.debit_amount) > config.money_epsilon else "credit"
        pair_txns[pair_key].append({
            "txn_id": str(row.txn_id),
            "account": account,
            "counterparty": counterparty,
            "amount": float(row.amount),
            "direction": direction,
            "date": str(row.date),
        })

    findings = []

    for (acct_a, acct_b), txns in sorted(pair_txns.items()):
        if len(txns) < config.low_value_test_min_pairs:
            continue

        # Check for reciprocal flow (both directions present)
        directions_from_a = {t["direction"] for t in txns if t["account"] == acct_a}
        directions_from_b = {t["direction"] for t in txns if t["account"] == acct_b}

        has_reciprocal = bool(directions_from_a) and bool(directions_from_b)
        if not has_reciprocal:
            # Also check if there are debits and credits for at least one account
            all_directions = {t["direction"] for t in txns}
            if len(all_directions) < 2:
                continue

        # Check if larger transactions follow between these accounts
        larger_exists = False
        if graph.has_node(acct_a) and graph.has_node(acct_b):
            for _, _, data in graph.edges(acct_a, data=True):
                if data.get("amount", 0) > config.low_value_test_max_amount * 10:
                    larger_exists = True
                    break
            if not larger_exists:
                for _, _, data in graph.edges(acct_b, data=True):
                    if data.get("amount", 0) > config.low_value_test_max_amount * 10:
                        larger_exists = True
                        break

        txn_ids = [t["txn_id"] for t in txns]
        amounts = [t["amount"] for t in txns]

        findings.append(
            make_finding(
                connection,
                13,
                [acct_a, acct_b],
                txn_ids,
                f"Accounts {acct_a} and {acct_b} exchanged {len(txns)} low-value "
                f"transaction(s) (amounts: {', '.join(f'{a:.2f}' for a in amounts[:5])}"
                + (f" ...and {len(amounts)-5} more" if len(amounts) > 5 else "")
                + f"), consistent with account-testing behaviour"
                + (" — larger transfers were subsequently observed." if larger_exists
                   else "."),
                {
                    "account_pair": [acct_a, acct_b],
                    "test_transaction_count": len(txns),
                    "amounts": amounts,
                    "has_reciprocal_flow": has_reciprocal,
                    "larger_transfers_followed": larger_exists,
                    "runtime_thresholds": {
                        "max_amount": config.low_value_test_max_amount,
                        "min_pairs": config.low_value_test_min_pairs,
                    },
                },
            )
        )
        if len(findings) >= config.maximum_findings_per_pattern:
            break

    return findings
