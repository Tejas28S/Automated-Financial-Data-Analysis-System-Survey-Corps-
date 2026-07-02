"""Pattern 4: time-respecting direct and multi-hop return flows."""

from __future__ import annotations

from datetime import date
import sqlite3

import networkx as nx

from ..config import AnalysisConfig
from .common import make_finding


def _edge_records(graph: nx.MultiDiGraph, source: str) -> list[tuple[str, str, dict]]:
    records = []
    for _, target, key, data in graph.out_edges(source, keys=True, data=True):
        if data.get("date"):
            records.append((target, key, data))
    return sorted(records, key=lambda item: (item[2].get("date", ""), item[2].get("time", ""), item[1]))


def detect_round_trips(
    connection: sqlite3.Connection,
    baseline: dict,
    graph: nx.MultiDiGraph,
    config: AnalysisConfig,
) -> list:
    observed = {
        node for node, data in graph.nodes(data=True) if data.get("observed_account")
    }
    window_days = int(baseline["thresholds"]["round_trip_window_days"])
    retention = float(baseline["thresholds"]["round_trip_min_retention_ratio"])
    findings = []
    seen_paths: set[tuple[str, ...]] = set()

    def search(
        origin: str,
        current: str,
        path_nodes: list[str],
        path_edges: list[dict],
        start_date: date,
        last_date: date,
    ) -> None:
        if len(findings) >= config.maximum_findings_per_pattern:
            return
        if len(path_edges) >= config.max_round_trip_hops:
            return
        for target, _, edge in _edge_records(graph, current):
            if target not in observed:
                continue
            edge_date = date.fromisoformat(edge["date"])
            if edge_date < last_date or (edge_date - start_date).days > window_days:
                continue
            amounts = [float(item["amount"]) for item in path_edges] + [float(edge["amount"])]
            if min(amounts) / max(amounts) < retention:
                continue
            if target == origin and len(path_edges) + 1 >= 2:
                edge_path = path_edges + [edge]
                canonical = tuple(str(item["txn_id"]) for item in edge_path)
                if canonical in seen_paths:
                    continue
                seen_paths.add(canonical)
                txn_ids = [
                    txn_id
                    for item in edge_path
                    for txn_id in item.get("txn_ids", [item["txn_id"]])
                ]
                accounts = path_nodes + [origin]
                duration = (edge_date - start_date).days
                findings.append(
                    make_finding(
                        connection,
                        4,
                        accounts,
                        txn_ids,
                        f"Money left {origin}, moved through {' → '.join(accounts[1:-1]) or 'a direct counterparty'}, and returned to {origin} within {duration} day(s) at comparable amounts.",
                        {
                            "path": accounts,
                            "duration_days": duration,
                            "edge_amounts": amounts,
                            "runtime_window_days": window_days,
                            "runtime_minimum_retention_ratio": retention,
                        },
                    )
                )
                continue
            if target in path_nodes:
                continue
            search(
                origin,
                target,
                path_nodes + [target],
                path_edges + [edge],
                start_date,
                edge_date,
            )

    for origin in sorted(observed):
        for target, _, edge in _edge_records(graph, origin):
            if target not in observed or target == origin:
                continue
            edge_date = date.fromisoformat(edge["date"])
            search(origin, target, [origin, target], [edge], edge_date, edge_date)
    return findings
