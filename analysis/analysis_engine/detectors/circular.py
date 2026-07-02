# Copied from: analysis phase cidecode hackathon/analysis phase cidecode hackathon/analysis_engine/detectors/circular.py
"""Pattern 9: directed cycles of length three or greater."""

from __future__ import annotations

import sqlite3

import networkx as nx

from ..config import AnalysisConfig
from ..graph import observed_account_subgraph
from .common import edge_confidence_details, make_finding


def _canonical_cycle(cycle: list[str]) -> tuple[str, ...]:
    rotations = [tuple(cycle[index:] + cycle[:index]) for index in range(len(cycle))]
    return min(rotations)


def detect_circular_flows(
    connection: sqlite3.Connection,
    baseline: dict,
    graph: nx.MultiDiGraph,
    config: AnalysisConfig,
) -> list:
    del baseline
    observed = observed_account_subgraph(graph)
    simple = nx.DiGraph()
    simple.add_nodes_from(observed.nodes(data=True))
    simple.add_edges_from((source, target) for source, target in observed.edges())
    cycles = nx.simple_cycles(simple, length_bound=config.max_cycle_length)
    findings = []
    seen: set[tuple[str, ...]] = set()
    for cycle in cycles:
        if len(cycle) < 3:
            continue
        canonical = _canonical_cycle([str(node) for node in cycle])
        if canonical in seen:
            continue
        seen.add(canonical)
        txn_ids: list[str] = []
        edge_support: list[dict] = []
        used_edges: list[dict] = []
        for index, source in enumerate(canonical):
            target = canonical[(index + 1) % len(canonical)]
            edges = graph.get_edge_data(source, target, default={})
            amounts = []
            edge_txn_ids = []
            for edge in edges.values():
                amounts.append(float(edge["amount"]))
                edge_txn_ids.extend(edge.get("txn_ids", [edge["txn_id"]]))
                used_edges.append(edge)
            txn_ids.extend(edge_txn_ids)
            edge_support.append(
                {"source": source, "target": target, "edge_count": len(edges), "amounts": amounts}
            )
        path = list(canonical) + [canonical[0]]
        findings.append(
            make_finding(
                connection,
                7,
                list(canonical),
                txn_ids,
                f"The directed account graph contains a {len(canonical)}-account cycle: {' → '.join(path)}.",
                {"cycle": path, "edge_support": edge_support, **edge_confidence_details(used_edges)},
                config=config,
            )
        )
        if len(findings) >= config.maximum_findings_per_pattern:
            break
    return findings
