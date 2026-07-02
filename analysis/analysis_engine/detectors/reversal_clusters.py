# Copied from: analysis phase cidecode hackathon/analysis phase cidecode hackathon/analysis_engine/detectors/reversal_clusters.py
"""Pattern 14: reversal clusters.

Detects multiple debit/reversal pairs occurring repeatedly within the same
account or related accounts — a sign of systematic testing or intentional
transaction manipulation.
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict

import pandas as pd

from ..config import AnalysisConfig
from ..database import fetch_transactions
from ..utils import normalize_text
from .common import make_finding

REVERSAL_RE = re.compile(
    r"\b(?:REVERSAL|REVERSED|FAILED|FAILURE|RETURN|RETURNED|REFUND|BOUNCE|DISHONOUR)\b",
    re.IGNORECASE,
)


def detect_reversal_clusters(
    connection: sqlite3.Connection,
    baseline: dict,
    config: AnalysisConfig,
) -> list:
    del baseline
    frame = fetch_transactions(connection, "eligible_for_detection = 1 AND date IS NOT NULL")
    if frame.empty:
        return []

    frame["parsed_date"] = pd.to_datetime(frame["date"], errors="coerce")
    findings = []

    for account_id, group in frame.groupby("account_id", sort=False):
        ordered = group.sort_values(["parsed_date", "time", "source_order", "row_id"])

        debits = ordered[ordered["debit_amount"] > config.money_epsilon]
        credits = ordered[ordered["credit_amount"] > config.money_epsilon]

        if debits.empty or credits.empty:
            continue

        # Find reversal credits (credits with reversal keywords in narration)
        reversal_credits = credits[
            credits["narration"].fillna("").apply(
                lambda n: bool(REVERSAL_RE.search(normalize_text(n)))
            )
        ]
        if reversal_credits.empty:
            continue

        # Match reversal credits to preceding debits by amount
        matched_pairs = []
        used_debits: set[str] = set()

        for rev in reversal_credits.itertuples(index=False):
            rev_amount = float(rev.credit_amount)

            # Find matching debit (same account, similar amount, before the reversal)
            candidates = debits[
                (~debits["txn_id"].isin(used_debits))
                & (debits["parsed_date"] <= rev.parsed_date)
            ]
            if candidates.empty:
                continue

            for debit in candidates.sort_values("parsed_date", ascending=False).itertuples(index=False):
                max_amt = max(rev_amount, float(debit.debit_amount), config.money_epsilon)
                diff_ratio = abs(rev_amount - float(debit.debit_amount)) / max_amt
                if diff_ratio <= config.reversal_amount_relative_tolerance:
                    matched_pairs.append((str(debit.txn_id), str(rev.txn_id)))
                    used_debits.add(str(debit.txn_id))
                    break

        if len(matched_pairs) < config.reversal_cluster_min_pairs:
            continue

        # This account has a cluster of reversals
        txn_ids = []
        for debit_id, credit_id in matched_pairs:
            txn_ids.extend([debit_id, credit_id])
        txn_ids = list(dict.fromkeys(txn_ids))

        findings.append(
            make_finding(
                connection,
                14,
                [str(account_id)],
                txn_ids,
                f"Account {account_id} has {len(matched_pairs)} debit-reversal pairs, "
                f"indicating systematic transaction reversal activity.",
                {
                    "reversal_pair_count": len(matched_pairs),
                    "pairs": [
                        {"debit_txn_id": d, "reversal_txn_id": r}
                        for d, r in matched_pairs
                    ],
                    "runtime_threshold_min_pairs": config.reversal_cluster_min_pairs,
                },
            )
        )
        if len(findings) >= config.maximum_findings_per_pattern:
            break

    return findings
