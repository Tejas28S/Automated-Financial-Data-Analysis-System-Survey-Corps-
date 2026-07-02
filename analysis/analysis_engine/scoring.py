"""Tiered suspicion scoring with overlap and confidence adjustment."""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, field
from typing import Any

from .models import Finding, PATTERN_BASE_WEIGHT

logger = logging.getLogger(__name__)


SCORING_POLICY: dict[str, Any] = {
    "tier_weights": {1: 100.0, 2: 35.0, 3: 8.0, 4: 2.0},
    "overlap_jaccard_threshold": 0.60,
    "overlap_reduction_multiplier": 0.20,
    "value_weight": 3.0,
    "centrality_bonus_weight": 5.0,
    "tier3_alone_is_weak_signal_only": True,
    "ranking_requires_tier1_or_tier2": True,
    "runtime_cutoff_method": "eligible_score_minimum_after_tier_gate",
    "tier_1_pattern_ids": [7, 8, 10, 17],
    "tier_2_pattern_ids": [3, 4, 5, 9, 11, 13, 18],
    "tier_3_pattern_ids": [12, 15, 16, 19],
    "below_tier3_pattern_ids": [22, 23],
    "legacy_base_weight_by_pattern": {**PATTERN_BASE_WEIGHT},
}

TIER_1_PATTERN_IDS = set(SCORING_POLICY["tier_1_pattern_ids"])
TIER_2_PATTERN_IDS = set(SCORING_POLICY["tier_2_pattern_ids"])
TIER_3_PATTERN_IDS = set(SCORING_POLICY["tier_3_pattern_ids"])
BELOW_TIER3_PATTERN_IDS = set(SCORING_POLICY["below_tier3_pattern_ids"])


@dataclass
class ScoredAccount:
    account_id: str
    distinct_pattern_count: int
    strong_pattern_count: int
    weak_pattern_count: int
    pattern_breakdown: dict[str, int]
    finding_ids: list[str]
    source_txn_ids: list[str]
    total_findings: int
    total_score: float
    score_breakdown: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _pattern_id_from_key(pattern_key: str) -> int:
    try:
        return int(str(pattern_key).split("_", 1)[0])
    except (TypeError, ValueError):
        return 0


def _build_account_index(
    findings_by_pattern: dict[str, list[Finding]],
) -> dict[str, dict[str, list[Finding]]]:
    index: dict[str, dict[str, list[Finding]]] = {}
    for p_key, findings in findings_by_pattern.items():
        pattern_id = _pattern_id_from_key(p_key)
        if pattern_id in {6, 21, 22, 23}:
            continue
        for finding in findings:
            for account_id in finding.accounts:
                account_patterns = index.setdefault(account_id, {})
                account_patterns.setdefault(p_key, []).append(finding)
    return index


def _collect_finding_ids(pattern_findings: dict[str, list[Finding]]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for findings in pattern_findings.values():
        for finding in findings:
            if finding.finding_id and finding.finding_id not in seen:
                seen.add(finding.finding_id)
                ordered.append(finding.finding_id)
    return ordered


def _collect_txn_ids(pattern_findings: dict[str, list[Finding]]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for findings in pattern_findings.values():
        for finding in findings:
            for txn_id in finding.txn_ids:
                if txn_id and txn_id not in seen:
                    seen.add(txn_id)
                    ordered.append(txn_id)
    return ordered


def _iter_numeric_values(value: Any, parent_key: str = "") -> list[float]:
    if isinstance(value, dict):
        numbers: list[float] = []
        for key, child in value.items():
            numbers.extend(_iter_numeric_values(child, str(key).lower()))
        return numbers
    if isinstance(value, list):
        numbers = []
        for child in value:
            numbers.extend(_iter_numeric_values(child, parent_key))
        return numbers
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if any(token in parent_key for token in ("amount", "volume", "credit", "debit")):
            number = float(value)
            return [abs(number)] if math.isfinite(number) else []
    return []


def _finding_value_basis(finding: Finding) -> float:
    values = _iter_numeric_values(finding.details)
    return max(values) if values else 0.0


def _finding_centrality_basis(finding: Finding) -> float:
    details = finding.details or {}
    return max(
        float(details.get("betweenness_centrality", 0) or 0),
        float(details.get("degree_centrality", 0) or 0),
    )


def finding_tier(finding: Finding) -> int:
    pattern_id = int(finding.pattern_id)
    details = finding.details or {}
    if pattern_id == 8:
        return 1 if details.get("trace_status") == "exhausted" else 2
    if pattern_id == 10:
        same_document = bool(details.get("same_document"))
        corroboration = str(details.get("corroboration_path", ""))
        return 1 if (not same_document and corroboration == "reference_match") else 3
    if pattern_id in TIER_1_PATTERN_IDS:
        return 1
    if pattern_id in TIER_2_PATTERN_IDS:
        return 2
    if pattern_id in TIER_3_PATTERN_IDS:
        return 3
    if pattern_id in BELOW_TIER3_PATTERN_IDS:
        return 4
    return 3


def _confidence_multiplier(finding: Finding) -> float:
    try:
        score = float(getattr(finding, "confidence_score", 1.0))
    except (TypeError, ValueError):
        score = 1.0
    return max(0.0, min(1.0, score))


def _txn_overlap(left: Finding, right: Finding) -> float:
    left_ids = set(left.txn_ids or [])
    right_ids = set(right.txn_ids or [])
    if not left_ids or not right_ids:
        return 0.0
    return len(left_ids & right_ids) / max(len(left_ids | right_ids), 1)


def _score_pattern_findings(pattern_findings: dict[str, list[Finding]]) -> dict[str, Any]:
    all_findings = [finding for findings in pattern_findings.values() for finding in findings]
    value_scale = max((_finding_value_basis(finding) for finding in all_findings), default=0.0)
    tier_counts = {1: 0, 2: 0, 3: 0, 4: 0}
    weighted_severity = 0.0
    value_component = 0.0
    centrality_bonus = 0.0
    pattern_components: dict[str, dict[str, Any]] = {}
    prior_scored: list[Finding] = []
    has_independent_flagging_evidence = False

    for p_key, findings in pattern_findings.items():
        component = 0.0
        applied_tiers: list[int] = []
        reduced_count = 0
        for finding in findings:
            tier = finding_tier(finding)
            tier_counts[tier] += 1
            applied_tiers.append(tier)
            if tier in {1, 2}:
                has_independent_flagging_evidence = True
            severity = float(SCORING_POLICY["tier_weights"].get(tier, 0.0)) * _confidence_multiplier(finding)
            if any(_txn_overlap(finding, previous) >= float(SCORING_POLICY["overlap_jaccard_threshold"]) for previous in prior_scored):
                severity *= float(SCORING_POLICY["overlap_reduction_multiplier"])
                reduced_count += 1
            component += severity
            weighted_severity += severity
            if value_scale > 0:
                value_component += (
                    math.log1p(_finding_value_basis(finding))
                    / math.log1p(value_scale)
                    * float(SCORING_POLICY["value_weight"])
                )
            centrality_bonus += _finding_centrality_basis(finding) * float(SCORING_POLICY["centrality_bonus_weight"])
            prior_scored.append(finding)

        pattern_components[p_key] = {
            "finding_count": float(len(findings)),
            "tiers": sorted(set(applied_tiers)),
            "weighted_severity": round(component, 4),
            "redundant_overlap_reduced_count": reduced_count,
            "max_confidence_score": round(max((_confidence_multiplier(f) for f in findings), default=1.0), 4),
        }

    total_score = weighted_severity + value_component + centrality_bonus
    score = {
        "total_score": round(total_score, 4),
        "breadth_component": 0.0,
        "weighted_severity": round(weighted_severity, 4),
        "value_component": round(value_component, 4),
        "centrality_bonus": round(centrality_bonus, 4),
        "strong_pattern_count": tier_counts[1],
        "weak_pattern_count": tier_counts[3] + tier_counts[4],
        "tier_counts": tier_counts,
        "has_independent_flagging_evidence": has_independent_flagging_evidence,
        "flagging_status": "eligible" if has_independent_flagging_evidence else "weak_signal_only",
        "pattern_components": pattern_components,
        "runtime_thresholds": SCORING_POLICY,
    }
    return score


def _assemble_scored_accounts(
    findings_by_pattern: dict[str, list[Finding]],
) -> list[ScoredAccount]:
    scored: list[ScoredAccount] = []
    for account_id, pattern_findings in _build_account_index(findings_by_pattern).items():
        pattern_breakdown = {p_key: len(flist) for p_key, flist in pattern_findings.items()}
        score_breakdown = _score_pattern_findings(pattern_findings)
        scored.append(
            ScoredAccount(
                account_id=account_id,
                distinct_pattern_count=len(pattern_breakdown),
                strong_pattern_count=score_breakdown["strong_pattern_count"],
                weak_pattern_count=score_breakdown["weak_pattern_count"],
                pattern_breakdown=pattern_breakdown,
                finding_ids=_collect_finding_ids(pattern_findings),
                source_txn_ids=_collect_txn_ids(pattern_findings),
                total_findings=sum(pattern_breakdown.values()),
                total_score=score_breakdown["total_score"],
                score_breakdown=score_breakdown,
            )
        )
    scored.sort(key=lambda sa: (-sa.total_score, -sa.distinct_pattern_count, -sa.total_findings, sa.account_id))
    return scored


def score_accounts(findings_by_pattern: dict[str, list[Finding]]) -> list[ScoredAccount]:
    if not findings_by_pattern:
        logger.info("score_accounts: no findings supplied; returning empty list.")
        return []
    scored = [
        item for item in _assemble_scored_accounts(findings_by_pattern)
        if item.score_breakdown.get("has_independent_flagging_evidence")
    ]
    cutoff = min((item.total_score for item in scored), default=0.0)
    for item in scored:
        item.score_breakdown.setdefault("runtime_thresholds", {})["flagged_score_cutoff"] = cutoff
    logger.info("score_accounts: scored %d eligible accounts; cutoff %.2f.", len(scored), cutoff)
    return scored


def weak_signal_accounts(findings_by_pattern: dict[str, list[Finding]]) -> list[dict[str, Any]]:
    weak = [
        item.to_dict()
        for item in _assemble_scored_accounts(findings_by_pattern)
        if not item.score_breakdown.get("has_independent_flagging_evidence")
    ]
    return weak
