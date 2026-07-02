# Copied from: analysis phase cidecode hackathon/analysis phase cidecode hackathon/analysis_engine/detectors/suspicious_ranking.py
"""Pattern 21: top suspicious account ranking.

Meta-pattern that runs after all other detectors. Identifies the highest-risk
accounts based on the combined suspicious behaviours observed — accounts that
appear across the most distinct pattern types are ranked highest.

This is a summarising pattern: it does not discover new suspicious activity
but rather consolidates findings from all other patterns into a risk ranking.
"""

from __future__ import annotations

import sqlite3

from ..config import AnalysisConfig
from ..models import Finding
from ..scoring import score_accounts
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
    ranked_accounts = score_accounts(findings_by_pattern)
    if not ranked_accounts:
        return []

    # Only include accounts with at least 2 distinct patterns (truly multi-signal)
    findings = []
    for scored in ranked_accounts:
        if scored.distinct_pattern_count < 2:
            continue

        pattern_names = sorted(
            key.split("_", 1)[1] if "_" in key else key
            for key in scored.pattern_breakdown
        )

        findings.append(
            make_finding(
                connection,
                21,
                [scored.account_id],
                scored.source_txn_ids[:200],  # Cap transaction IDs for very large sets
                f"Account {scored.account_id} has an overall suspicious-account score "
                f"of {scored.total_score:.2f}, based on {scored.distinct_pattern_count} "
                f"distinct pattern type(s) and {scored.total_findings} total finding(s): "
                f"{', '.join(pattern_names)}.",
                {
                    "total_score": scored.total_score,
                    "score_breakdown": scored.score_breakdown,
                    "distinct_pattern_count": scored.distinct_pattern_count,
                    "pattern_ids": sorted(
                        int(key.split("_", 1)[0]) for key in scored.pattern_breakdown
                    ),
                    "pattern_names": pattern_names,
                    "pattern_breakdown": scored.pattern_breakdown,
                    "total_findings": scored.total_findings,
                    "total_transactions_involved": len(scored.source_txn_ids),
                    "source_finding_ids": scored.finding_ids,
                    "runtime_thresholds": scored.score_breakdown["runtime_thresholds"],
                },
            )
        )
        if len(findings) >= config.maximum_findings_per_pattern:
            break

    return findings
