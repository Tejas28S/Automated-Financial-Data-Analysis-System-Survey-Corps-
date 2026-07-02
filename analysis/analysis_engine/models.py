# Copied from: analysis phase cidecode hackathon/analysis phase cidecode hackathon/analysis_engine/models.py
"""Typed, JSON-safe analysis results with an in-process reusable graph."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
import hashlib
import json
from typing import Any

import networkx as nx


PATTERN_CATALOG: dict[int, str] = {
    1: "duplicate_verification",
    2: "failed_reversed_transaction_detection",
    3: "pass_through_routing_account",
    4: "fund_pooling_account",
    5: "structuring_smurfing_detection",
    6: "money_flow_graph_construction",
    7: "circular_flow_multi_hop_cycle_detection",
    8: "money_trail_tracing",
    9: "credit_to_cash_out_chains",
    10: "cross_statement_links",
    11: "balance_parking_account",
    12: "hub_ranking",
    13: "low_value_account_testing",
    14: "reversal_clusters",
    15: "round_value_debit_patterns",
    16: "shared_upi_identifiers",
    17: "round_trip_detection",
    18: "dormant_reactivation",
    19: "first_contact_large_transfer",
    21: "suspicious_account_ranking",
    22: "llm_investigated_anomalies",
    23: "ml_ensemble_anomaly_lead",
}


PATTERN_EVIDENCE_STRENGTH: dict[int, str] = {
    1: "weak",
    2: "weak",
    3: "weak",
    4: "weak",
    5: "strong",
    6: "structural",
    7: "strong",
    8: "strong",
    9: "weak",
    10: "strong",
    11: "weak",
    12: "weak",
    13: "strong",
    14: "weak",
    15: "weak",
    16: "strong",
    17: "strong",
    18: "strong",
    19: "strong",
    21: "composite",
    22: "lead",
    23: "lead",
}


PATTERN_BASE_WEIGHT: dict[int, float] = {
    1: 0.6,
    2: 0.6,
    3: 0.9,
    4: 0.8,
    5: 1.8,
    6: 0.3,
    7: 1.9,
    8: 2.2,
    9: 1.0,
    10: 1.6,
    11: 0.7,
    12: 0.6,
    13: 1.4,
    14: 0.8,
    15: 0.7,
    16: 1.5,
    17: 2.0,
    18: 1.7,
    19: 1.5,
}


CONFIDENCE_TIER_BY_EVIDENCE_STRENGTH: dict[str, str] = {
    "strong": "high",
    "structural": "high",
    "composite": "high",
    "weak": "medium",
    "lead": "low",
}


def confidence_tier_for_evidence_strength(evidence_strength: str) -> str:
    return CONFIDENCE_TIER_BY_EVIDENCE_STRENGTH.get(str(evidence_strength or "").lower(), "medium")


def assert_confidence_tier_consistency(findings_by_pattern: dict[str, list["Finding"]]) -> None:
    mismatches = []
    for key, findings in findings_by_pattern.items():
        for finding in findings or []:
            if finding.confidence_score >= 0.85:
                expected = "high"
            elif finding.confidence_score >= 0.50:
                expected = "medium"
            else:
                expected = "low"
            actual = str(finding.confidence_tier or "").lower()
            if actual != expected:
                mismatches.append(
                    {
                        "pattern": key,
                        "finding_id": finding.finding_id,
                        "evidence_strength": finding.evidence_strength,
                        "confidence_tier": finding.confidence_tier,
                        "expected": expected,
                    }
                )
    if mismatches:
        raise AssertionError(f"Finding confidence_tier/evidence_strength mismatch: {mismatches[:10]}")


def pattern_key(pattern_id: int) -> str:
    return f"{pattern_id}_{PATTERN_CATALOG[pattern_id]}"


def _json_default(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


@dataclass
class Finding:
    pattern_id: int
    pattern_name: str
    accounts: list[str]
    txn_ids: list[str]
    explanation: str
    confidence_tier: str
    detection_method: str = "deterministic"
    details: dict[str, Any] = field(default_factory=dict)
    evidence_strength: str = ""
    narration: str = ""
    narration_validation: str = ""
    finding_id: str = ""
    confidence_score: float = 1.0

    def __post_init__(self) -> None:
        self.accounts = sorted({str(value) for value in self.accounts if str(value)})
        self.txn_ids = list(dict.fromkeys(str(value) for value in self.txn_ids if str(value)))
        if not self.evidence_strength:
            self.evidence_strength = PATTERN_EVIDENCE_STRENGTH.get(self.pattern_id, "weak")
        self.evidence_strength = str(self.evidence_strength).lower()
        self.confidence_score = self._derive_confidence_score()
        self.confidence_tier = self._derive_confidence_tier()
        runtime = self.details.setdefault("runtime_thresholds", {})
        if self.pattern_id in PATTERN_BASE_WEIGHT:
            runtime.setdefault("base_weight", PATTERN_BASE_WEIGHT[self.pattern_id])
        runtime.setdefault("evidence_strength", self.evidence_strength)
        self.details.setdefault("confidence_score", self.confidence_score)
        if not self.narration:
            self.narration = self.explanation
        if not self.narration_validation:
            self.narration_validation = "verified"
        if not self.finding_id:
            identity = json.dumps(
                [self.pattern_id, self.accounts, self.txn_ids, self.explanation],
                sort_keys=True,
                default=_json_default,
            )
            self.finding_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]

    def _derive_confidence_score(self) -> float:
        candidates: list[float] = []
        for key in ("confidence_score", "counterparty_resolution_confidence"):
            try:
                value = self.details.get(key)
                if value is not None and float(value) > 0:
                    candidates.append(float(value))
            except (TypeError, ValueError):
                pass
        for key in ("edge_corroboration", "allocations"):
            values = self.details.get(key)
            if isinstance(values, list):
                for item in values:
                    if isinstance(item, dict):
                        for nested_key in ("confidence_score", "counterparty_resolution_confidence"):
                            try:
                                value = item.get(nested_key)
                                if value is not None and float(value) > 0:
                                    candidates.append(float(value))
                            except (TypeError, ValueError):
                                pass
        if self.detection_method in {"llm_investigated_anomaly", "ml_ensemble_anomaly_lead"}:
            candidates.append(0.50)
        if not candidates:
            return 1.0
        score = min(candidates)
        return max(0.0, min(1.0, score))

    def _derive_confidence_tier(self) -> str:
        if self.confidence_score >= 0.85:
            return "high"
        if self.confidence_score >= 0.50:
            return "medium"
        return "low"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AnalysisResult:
    run_metadata: dict[str, Any]
    input_contract: dict[str, Any]
    baseline_summary: dict[str, Any]
    counterparty_resolution: dict[str, Any]
    suspicious_accounts: list[dict[str, Any]]
    findings_by_pattern: dict[str, list[Finding]]
    excluded_rows: dict[str, list[dict[str, Any]]]
    possible_same_owner: list[dict[str, Any]]
    graph: nx.MultiDiGraph
    balance_validation: dict[str, Any] = field(default_factory=dict)
    weak_signal_accounts: list[dict[str, Any]] = field(default_factory=list)
    case_structure: dict[str, Any] = field(default_factory=dict)
    cluster_summaries: list[dict[str, Any]] = field(default_factory=list)
    case_summary: str = ""
    network_graph_for_display: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, include_graph: bool = True) -> dict[str, Any]:
        findings = {
            pattern_key(pattern_id): [
                finding.to_dict()
                for finding in self.findings_by_pattern.get(pattern_key(pattern_id), [])
            ]
            for pattern_id in PATTERN_CATALOG
        }
        result: dict[str, Any] = {
            "run_metadata": self.run_metadata,
            "input_contract": self.input_contract,
            "baseline_summary": self.baseline_summary,
            "counterparty_resolution": self.counterparty_resolution,
            "suspicious_accounts": self.suspicious_accounts,
            "findings_by_pattern": findings,
            "all_findings": [item for values in findings.values() for item in values],
            "excluded_rows": self.excluded_rows,
            "possible_same_owner": self.possible_same_owner,
            "balance_validation": self.balance_validation,
            "weak_signal_accounts": self.weak_signal_accounts,
            "case_structure": self.case_structure,
            "cluster_summaries": self.cluster_summaries,
            "case_summary": self.case_summary,
            "network_graph_for_display": self.network_graph_for_display,
            "graph_summary": {
                "node_count": self.graph.number_of_nodes(),
                "edge_count": self.graph.number_of_edges(),
            },
        }
        if include_graph:
            result["graph"] = nx.node_link_data(self.graph, edges="edges")
        return result


def json_default(value: Any) -> Any:
    return _json_default(value)
