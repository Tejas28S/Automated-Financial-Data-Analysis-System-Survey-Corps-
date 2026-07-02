# Copied from: analysis phase cidecode hackathon/analysis phase cidecode hackathon/analysis_engine/detectors/hub_ranking.py
"""Pattern 12: high-risk internal flow hub ranking.

Ranks accounts that act as major routing hubs because many suspicious flows
converge through them. Uses graph centrality (degree + betweenness) combined
with the number of distinct patterns that reference the account.
"""

from __future__ import annotations

import sqlite3

import networkx as nx
import pandas as pd

from ..config import AnalysisConfig
from ..graph import observed_account_subgraph
from .common import make_finding


def detect_high_risk_hub_ranking(
    connection: sqlite3.Connection,
    baseline: dict,
    graph: nx.MultiDiGraph,
    config: AnalysisConfig,
) -> list:
    thresholds = baseline["thresholds"]
    observed = observed_account_subgraph(graph)

    if observed.number_of_nodes() < 3:
        return []

    # Compute graph centrality metrics
    simple = nx.DiGraph()
    simple.add_nodes_from(observed.nodes())
    simple.add_edges_from((s, t) for s, t in observed.edges())

    in_degree = dict(simple.in_degree())
    out_degree = dict(simple.out_degree())
    total_degree = {node: in_degree.get(node, 0) + out_degree.get(node, 0) for node in simple.nodes()}

    # Betweenness centrality
    try:
        betweenness = nx.betweenness_centrality(simple)
    except Exception:
        betweenness = {node: 0.0 for node in simple.nodes()}

    # Compute volume flowing through each node
    node_volumes: dict[str, float] = {}
    for node in observed.nodes():
        in_vol = sum(float(d.get("amount", 0)) for _, _, d in graph.in_edges(node, data=True))
        out_vol = sum(float(d.get("amount", 0)) for _, _, d in graph.out_edges(node, data=True))
        node_volumes[node] = in_vol + out_vol

    findings = []

    # Rank by total degree (hub-ness)
    hub_candidates = sorted(
        total_degree.items(),
        key=lambda x: (-x[1], -node_volumes.get(x[0], 0), x[0]),
    )

    for node, degree in hub_candidates:
        if degree < config.hub_ranking_min_degree:
            continue
        if node_volumes.get(node, 0) < thresholds["hub_flow_volume_min"]:
            continue

        # Gather all transaction IDs flowing through this node
        incident = list(graph.in_edges(node, data=True)) + list(graph.out_edges(node, data=True))
        txn_ids = []
        for edge_tuple in incident:
            edge_data = edge_tuple[2] if len(edge_tuple) > 2 else {}
            txn_ids.extend(edge_data.get("txn_ids", [edge_data.get("txn_id", "")]))
        txn_ids = list(dict.fromkeys(tid for tid in txn_ids if tid))

        in_deg = in_degree.get(node, 0)
        out_deg = out_degree.get(node, 0)
        bc = betweenness.get(node, 0)
        volume = node_volumes.get(node, 0)

        findings.append(
            make_finding(
                connection,
                12,
                [node],
                txn_ids,
                f"Account {node} acts as a routing hub with {in_deg} inbound and {out_deg} outbound "
                f"connections, betweenness centrality {bc:.4f}, and total flow volume {volume:.2f}.",
                {
                    "in_degree": in_deg,
                    "out_degree": out_deg,
                    "total_degree": degree,
                    "betweenness_centrality": round(bc, 6),
                    "total_flow_volume": round(volume, 2),
                    "runtime_thresholds": {
                        "min_degree": config.hub_ranking_min_degree,
                        "flow_volume_min": thresholds["hub_flow_volume_min"],
                    },
                },
            )
        )
        if len(findings) >= config.maximum_findings_per_pattern:
            break

    return findings
