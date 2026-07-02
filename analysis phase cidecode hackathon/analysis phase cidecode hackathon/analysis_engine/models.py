"""Typed, JSON-safe analysis results with an in-process reusable graph."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
import hashlib
import json
from typing import Any

import networkx as nx


PATTERN_CATALOG: dict[int, str] = {
    1: "duplicate_detection_cross_check",
    2: "failed_reversed_transaction_detection",
    3: "balance_consistency_validation",
    4: "round_trip_detection",
    5: "transit_pass_through_detection",
    6: "accumulation_account_detection",
    7: "structuring_smurfing_detection",
    8: "money_flow_graph_construction",
    9: "circular_flow_multi_hop_cycle_detection",
    10: "money_trail_tracing",
    11: "high_throughput_pass_through_detection",
    12: "matched_internal_flow_hub_detection",
    13: "cross_statement_money_flow_links",
    14: "large_credit_followed_by_cash_withdrawal",
    15: "accumulation_holding_account_detection",
    16: "repeated_round_value_debit_pattern",
    17: "shared_upi_identifier_detection",
    18: "reversal_cluster_detection",
    19: "low_value_reciprocal_account_testing",
    20: "high_risk_internal_flow_hub_ranking",
    21: "top_suspicious_account_ranking",
}


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
    details: dict[str, Any] = field(default_factory=dict)
    finding_id: str = ""

    def __post_init__(self) -> None:
        self.accounts = sorted({str(value) for value in self.accounts if str(value)})
        self.txn_ids = list(dict.fromkeys(str(value) for value in self.txn_ids if str(value)))
        if not self.finding_id:
            identity = json.dumps(
                [self.pattern_id, self.accounts, self.txn_ids, self.explanation],
                sort_keys=True,
                default=_json_default,
            )
            self.finding_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AnalysisResult:
    run_metadata: dict[str, Any]
    baseline_summary: dict[str, Any]
    counterparty_resolution: dict[str, Any]
    suspicious_accounts: list[dict[str, Any]]
    findings_by_pattern: dict[str, list[Finding]]
    excluded_rows: dict[str, list[dict[str, Any]]]
    possible_same_owner: list[dict[str, Any]]
    graph: nx.MultiDiGraph
    balance_validation: dict[str, Any] = field(default_factory=dict)

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
            "baseline_summary": self.baseline_summary,
            "counterparty_resolution": self.counterparty_resolution,
            "suspicious_accounts": self.suspicious_accounts,
            "findings_by_pattern": findings,
            "all_findings": [item for values in findings.values() for item in values],
            "excluded_rows": self.excluded_rows,
            "possible_same_owner": self.possible_same_owner,
            "balance_validation": self.balance_validation,
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
