"""Pattern 21: top suspicious account ranking.

Meta-pattern that runs after all other detectors. Identifies the highest-risk
accounts based on the combined suspicious behaviours observed — accounts that
appear across the most distinct pattern types are ranked highest.

This is a summarising pattern: it does not discover new suspicious activity
but rather consolidates findings from all other patterns into a risk ranking.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Any

from ..config import AnalysisConfig
from ..models import Finding, PATTERN_CATALOG, pattern_key
from .common import make_finding


def detect_top_suspicious_ranking(
    connection: sqlite3.Connection,
    findings_by_pattern: dict[str, list[Finding]],
    config: AnalysisConfig,
) -> list:
    """Rank accounts by the number of distinct pattern types that flagged them.

    Parameters
    ----------
    connection : sqlite3.Connection
        Database connection for ``make_finding`` provenance.
    findings_by_pattern : dict
        All findings from patterns 1–20 (keyed by pattern_key).
    config : AnalysisConfig
        Pipeline configuration.

    Returns
    -------
    list[Finding]
        One finding per top-ranked account, explaining which patterns flagged it.
    """
    # Build per-account index: account_id → set of pattern IDs
    account_patterns: dict[str, set[int]] = defaultdict(set)
    account_txn_ids: dict[str, list[str]] = defaultdict(list)
    account_finding_count: dict[str, int] = defaultdict(int)

    for p_key, flist in findings_by_pattern.items():
        for f in flist:
            pid = f.pattern_id
            # Skip self (pattern 21) to avoid circularity
            if pid == 21:
                continue
            for acct in f.accounts:
                account_patterns[acct].add(pid)
                account_txn_ids[acct].extend(f.txn_ids)
                account_finding_count[acct] += 1

    if not account_patterns:
        return []

    # Rank by distinct pattern count, then total findings, then account ID
    ranked = sorted(
        account_patterns.items(),
        key=lambda item: (-len(item[1]), -account_finding_count[item[0]], item[0]),
    )

    # Only include accounts with at least 2 distinct patterns (truly multi-signal)
    findings = []
    for account_id, pattern_ids in ranked:
        if len(pattern_ids) < 2:
            continue

        pattern_names = sorted(
            PATTERN_CATALOG.get(pid, f"pattern_{pid}") for pid in pattern_ids
        )
        txn_ids = list(dict.fromkeys(account_txn_ids[account_id]))
        total_findings = account_finding_count[account_id]

        findings.append(
            make_finding(
                connection,
                21,
                [account_id],
                txn_ids[:200],  # Cap txn_ids for very large sets
                f"Account {account_id} is flagged by {len(pattern_ids)} distinct pattern types "
                f"with {total_findings} total finding(s): {', '.join(pattern_names)}.",
                {
                    "distinct_pattern_count": len(pattern_ids),
                    "pattern_ids": sorted(pattern_ids),
                    "pattern_names": pattern_names,
                    "total_findings": total_findings,
                    "total_transactions_involved": len(txn_ids),
                },
            )
        )
        if len(findings) >= config.maximum_findings_per_pattern:
            break

    return findings
