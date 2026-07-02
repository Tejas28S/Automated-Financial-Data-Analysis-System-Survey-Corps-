"""Pattern 11: accounts where money comes in and quickly goes out with very little retained balance.

Unlike Pattern 5 (transit) which uses aggregate throughput ratio, this pattern
examines temporal proximity — credits followed by near-equal debits within a
short window — to identify high-throughput pass-through behaviour.
"""

from __future__ import annotations

import sqlite3

import networkx as nx
import numpy as np
import pandas as pd

from ..config import AnalysisConfig
from ..utils import safe_ratio
from .common import make_finding


def detect_high_throughput_pass_through(
    connection: sqlite3.Connection,
    baseline: dict,
    graph: nx.MultiDiGraph,
    config: AnalysisConfig,
) -> list:
    thresholds = baseline["thresholds"]
    accounts = pd.read_sql_query("SELECT * FROM accounts", connection)
    findings = []

    for row in accounts.itertuples(index=False):
        total_credit = float(row.total_credit)
        total_debit = float(row.total_debit)
        total_volume = float(row.total_volume)

        if total_volume < thresholds["high_throughput_volume_min"]:
            continue

        # Retention is how much stays; pass-through means very little stays
        if total_credit < config.money_epsilon:
            continue
        retention = safe_ratio(abs(total_credit - total_debit), total_credit)
        if retention > config.high_throughput_retention_max:
            continue

        # Must have multiple counterparties on both sides
        node = str(row.account_id)
        in_edges = list(graph.in_edges(node, data=True))
        out_edges = list(graph.out_edges(node, data=True))
        in_sources = {src for src, _, _ in in_edges if src != node}
        out_targets = {tgt for _, tgt, _ in out_edges if tgt != node}

        if len(in_sources) < config.high_throughput_min_counterparties:
            continue
        if len(out_targets) < 1:
            continue

        # Gather transaction IDs from all incident edges
        txn_ids = []
        for edges in (in_edges, out_edges):
            for edge_tuple in edges:
                edge_data = edge_tuple[2] if len(edge_tuple) > 2 else {}
                txn_ids.extend(edge_data.get("txn_ids", [edge_data.get("txn_id", "")]))
        txn_ids = [tid for tid in txn_ids if tid]

        findings.append(
            make_finding(
                connection,
                11,
                [node],
                txn_ids,
                f"Account {node} received {total_credit:.2f} from {len(in_sources)} sources "
                f"and disbursed {total_debit:.2f} to {len(out_targets)} targets, "
                f"retaining only {retention:.1%} of inflow — consistent with high-throughput pass-through.",
                {
                    "total_credit": total_credit,
                    "total_debit": total_debit,
                    "retention_ratio": retention,
                    "inbound_source_count": len(in_sources),
                    "outbound_target_count": len(out_targets),
                    "runtime_thresholds": {
                        "volume_min": thresholds["high_throughput_volume_min"],
                        "retention_max": config.high_throughput_retention_max,
                        "min_counterparties": config.high_throughput_min_counterparties,
                    },
                },
            )
        )
        if len(findings) >= config.maximum_findings_per_pattern:
            break

    return findings
