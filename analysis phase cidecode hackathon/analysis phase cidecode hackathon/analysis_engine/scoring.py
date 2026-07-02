"""Suspicion scoring – rank accounts by distinct-pattern breadth.

Step 6 of the implementation plan.  The core insight is that an account
flagged by *many different* pattern types is far more suspicious than one
flagged many times by a *single* pattern.  Five structuring findings still
count as one pattern type; one round-trip plus one accumulation finding
counts as two.

Public API
----------
score_accounts(findings_by_pattern)
    → list[ScoredAccount]   (sorted descending by distinct_pattern_count)
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from .models import Finding, pattern_key, PATTERN_CATALOG

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class ScoredAccount:
    """A single account's suspicion summary.

    Attributes
    ----------
    account_id : str
        The account identifier.
    distinct_pattern_count : int
        Number of *distinct* pattern types (from ``PATTERN_CATALOG``) that
        produced at least one finding mentioning this account.  This is the
        primary score used for ranking.
    pattern_breakdown : dict[str, int]
        Mapping of ``pattern_key`` → number of findings for that pattern
        that mention this account.
    finding_ids : list[str]
        De-duplicated list of ``finding_id`` values across every finding
        that mentions this account.
    total_findings : int
        Raw total number of findings (across all patterns) that mention
        this account.  Always ``sum(pattern_breakdown.values())``.
    """

    account_id: str
    distinct_pattern_count: int
    pattern_breakdown: dict[str, int]
    finding_ids: list[str]
    total_findings: int

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict (JSON-safe)."""
        return asdict(self)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_account_index(
    findings_by_pattern: dict[str, list[Finding]],
) -> dict[str, dict[str, list[Finding]]]:
    """Build a nested index: account_id → pattern_key → [findings].

    Only pattern keys that actually contain findings are included.
    """
    index: dict[str, dict[str, list[Finding]]] = {}

    for p_key, findings in findings_by_pattern.items():
        for finding in findings:
            for account_id in finding.accounts:
                account_patterns = index.setdefault(account_id, {})
                account_patterns.setdefault(p_key, []).append(finding)

    return index


def _collect_finding_ids(
    pattern_findings: dict[str, list[Finding]],
) -> list[str]:
    """Return a de-duplicated, deterministically ordered list of finding IDs."""
    seen: set[str] = set()
    ordered: list[str] = []
    for findings in pattern_findings.values():
        for finding in findings:
            if finding.finding_id and finding.finding_id not in seen:
                seen.add(finding.finding_id)
                ordered.append(finding.finding_id)
    return ordered


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def score_accounts(
    findings_by_pattern: dict[str, list[Finding]],
) -> list[ScoredAccount]:
    """Score every account that appears in at least one finding.

    Parameters
    ----------
    findings_by_pattern : dict[str, list[Finding]]
        Keys are ``pattern_key`` strings (e.g.
        ``'7_structuring_smurfing_detection'``); values are lists of
        :class:`Finding` objects produced by that detector.

    Returns
    -------
    list[ScoredAccount]
        Accounts sorted **descending** by ``distinct_pattern_count``, then
        descending by ``total_findings``, then ascending by ``account_id``
        (for deterministic tie-breaking).
    """
    if not findings_by_pattern:
        logger.info("score_accounts: no findings supplied – returning empty list.")
        return []

    # 1. Build per-account index  ──────────────────────────────────────────
    account_index = _build_account_index(findings_by_pattern)

    if not account_index:
        logger.info("score_accounts: findings present but no accounts referenced.")
        return []

    # 2. Assemble scored rows  ─────────────────────────────────────────────
    scored: list[ScoredAccount] = []

    for account_id, pattern_findings in account_index.items():
        pattern_breakdown: dict[str, int] = {
            p_key: len(flist) for p_key, flist in pattern_findings.items()
        }
        finding_ids = _collect_finding_ids(pattern_findings)
        total_findings = sum(pattern_breakdown.values())

        scored.append(
            ScoredAccount(
                account_id=account_id,
                distinct_pattern_count=len(pattern_breakdown),
                pattern_breakdown=pattern_breakdown,
                finding_ids=finding_ids,
                total_findings=total_findings,
            )
        )

    # 3. Sort  ─────────────────────────────────────────────────────────────
    scored.sort(
        key=lambda sa: (-sa.distinct_pattern_count, -sa.total_findings, sa.account_id),
    )

    logger.info(
        "score_accounts: scored %d accounts; top score = %d distinct patterns.",
        len(scored),
        scored[0].distinct_pattern_count if scored else 0,
    )

    return scored
