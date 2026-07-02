"""Case relationship structure built from finalized graph and findings."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

import networkx as nx

from .llm_client import GroqKeyRotatingClient
from .models import Finding
from .scoring import finding_tier


def build_case_structure(
    graph: nx.MultiDiGraph,
    findings_by_pattern: dict[str, list[Finding]],
    suspicious_accounts: list[dict[str, Any]],
    llm_client: GroqKeyRotatingClient | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], str, dict[str, Any]]:
    account_scores = {str(item.get("account_id")): item for item in suspicious_accounts}
    account_findings = _account_findings(findings_by_pattern)
    components = _components(graph)
    node_cluster: dict[str, str] = {}
    clusters: list[dict[str, Any]] = []

    for idx, members in enumerate(components, start=1):
        cluster_id = f"cluster_{idx:03d}"
        member_list = sorted(str(member) for member in members)
        for member in member_list:
            node_cluster[member] = cluster_id
        edge_count = sum(1 for source, target in graph.edges() if str(source) in members and str(target) in members)
        total_score = sum(float(account_scores.get(member, {}).get("total_score", 0) or 0) for member in member_list)
        pattern_ids = sorted({
            int(finding.pattern_id)
            for member in member_list
            for finding in account_findings.get(member, [])
        })
        clusters.append(
            {
                "cluster_id": cluster_id,
                "member_accounts": member_list,
                "account_count": len(member_list),
                "edge_count": edge_count,
                "is_isolated": edge_count == 0,
                "total_score": round(total_score, 4),
                "pattern_ids": pattern_ids,
                "highest_priority_account": max(
                    member_list,
                    key=lambda member: (float(account_scores.get(member, {}).get("total_score", 0) or 0), member),
                ) if member_list else "",
            }
        )

    case_structure = {
        "cluster_count": len(clusters),
        "clusters": clusters,
        "account_to_cluster": node_cluster,
    }
    network_graph_for_display = _network_graph_for_display(graph, node_cluster, account_scores, account_findings)
    cluster_summaries = [
        _summarize_cluster(cluster, graph, account_findings, account_scores, llm_client)
        for cluster in clusters
    ]
    case_summary = _case_summary(clusters, suspicious_accounts)
    return case_structure, cluster_summaries, case_summary, network_graph_for_display


def _components(graph: nx.MultiDiGraph) -> list[set[str]]:
    if graph is None or graph.number_of_nodes() == 0:
        return []
    undirected = graph.to_undirected()
    components = [set(str(node) for node in component) for component in nx.connected_components(undirected)]
    return sorted(components, key=lambda members: (-len(members), sorted(members)))


def _account_findings(findings_by_pattern: dict[str, list[Finding]]) -> dict[str, list[Finding]]:
    mapping: dict[str, list[Finding]] = defaultdict(list)
    for findings in findings_by_pattern.values():
        for finding in findings:
            if finding.pattern_id in {6, 21, 22, 23}:
                continue
            for account in finding.accounts:
                mapping[str(account)].append(finding)
    return mapping


def _network_graph_for_display(
    graph: nx.MultiDiGraph,
    node_cluster: dict[str, str],
    account_scores: dict[str, dict[str, Any]],
    account_findings: dict[str, list[Finding]],
) -> dict[str, Any]:
    nodes = []
    for node, data in graph.nodes(data=True):
        account = str(node)
        score = account_scores.get(account, {})
        tiers = sorted({finding_tier(finding) for finding in account_findings.get(account, [])})
        nodes.append(
            {
                "id": account,
                "cluster_id": node_cluster.get(account, ""),
                "observed_account": bool(data.get("observed_account")),
                "total_score": float(score.get("total_score", 0) or 0),
                "suspicion_tiers": tiers,
                "pattern_count": len(account_findings.get(account, [])),
            }
        )
    edges = []
    for source, target, key, data in graph.edges(keys=True, data=True):
        edges.append(
            {
                "id": f"{source}->{target}::{key}",
                "source": str(source),
                "target": str(target),
                "amount": float(data.get("amount", 0) or 0),
                "date": str(data.get("date", "") or ""),
                "txn_ids": [str(value) for value in data.get("txn_ids", []) if str(value)],
                "confidence_score": float(data.get("confidence_score", 1.0) or 1.0),
            }
        )
    return {"nodes": nodes, "edges": edges}


def _summarize_cluster(
    cluster: dict[str, Any],
    graph: nx.MultiDiGraph,
    account_findings: dict[str, list[Finding]],
    account_scores: dict[str, dict[str, Any]],
    llm_client: GroqKeyRotatingClient | None,
) -> dict[str, Any]:
    members = [str(value) for value in cluster.get("member_accounts", [])]
    pattern_names = sorted({
        finding.pattern_name
        for member in members
        for finding in account_findings.get(member, [])
    })
    template = _template_cluster_summary(cluster, pattern_names)
    summary = template
    source = "template"
    errors: list[str] = []
    if len(members) > 1 and llm_client is not None and llm_client.available and llm_client.status_label() == "active":
        payload = {
            "cluster": cluster,
            "accounts": [
                {
                    "account_id": member,
                    "score": account_scores.get(member, {}),
                    "patterns": [
                        {
                            "pattern_id": finding.pattern_id,
                            "pattern_name": finding.pattern_name,
                            "tier": finding_tier(finding),
                            "txn_ids": finding.txn_ids[:20],
                        }
                        for finding in account_findings.get(member, [])
                    ],
                }
                for member in members
            ],
            "edges": [
                {
                    "source": str(source_node),
                    "target": str(target_node),
                    "amount": float(data.get("amount", 0) or 0),
                    "date": str(data.get("date", "") or ""),
                }
                for source_node, target_node, data in graph.edges(data=True)
                if str(source_node) in members and str(target_node) in members
            ][:80],
        }
        result = llm_client.chat_json(
            [
                {
                    "role": "system",
                    "content": (
                        "Given connected bank accounts, relationships, and finalized pattern evidence, "
                        "write a 4-6 sentence plain-English case summary. State only what the data shows. "
                        "Do not speculate about intent or guilt. Return JSON with key summary."
                    ),
                },
                {"role": "user", "content": json.dumps(payload, default=str, sort_keys=True)},
            ],
            call_context="cluster_summary",
        )
        if result.ok:
            try:
                candidate = str(json.loads(result.content).get("summary", "")).strip()
            except json.JSONDecodeError:
                candidate = ""
            if candidate:
                summary = candidate
                source = "groq"
        else:
            errors.append(result.error)
    row = {
        "cluster_id": cluster.get("cluster_id"),
        "summary": summary,
        "explanation_source": source,
    }
    if errors:
        row["llm_errors"] = errors
    return row


def _template_cluster_summary(cluster: dict[str, Any], pattern_names: list[str]) -> str:
    members = cluster.get("member_accounts", [])
    if cluster.get("is_isolated"):
        account = members[0] if members else "unknown"
        patterns = ", ".join(pattern_names) if pattern_names else "no scored pattern"
        return f"Account {account} was evaluated as an isolated account with no established transactional link to other accounts in this case. It was associated with {patterns}."
    patterns = ", ".join(pattern_names[:8]) if pattern_names else "no scored pattern"
    return (
        f"{cluster.get('cluster_id')} contains {cluster.get('account_count')} connected account(s) "
        f"and {cluster.get('edge_count')} transaction edge(s). The cluster evidence includes {patterns}."
    )


def _case_summary(clusters: list[dict[str, Any]], suspicious_accounts: list[dict[str, Any]]) -> str:
    if not clusters:
        return "No transactional account clusters were available in this run."
    top = max(clusters, key=lambda item: (float(item.get("total_score", 0) or 0), str(item.get("cluster_id", ""))))
    return (
        f"The case contains {len(clusters)} account cluster(s), with {len(suspicious_accounts)} account(s) ranked for suspicious activity. "
        f"The highest-priority cluster is {top.get('cluster_id')} with total score {float(top.get('total_score', 0) or 0):.2f}."
    )
