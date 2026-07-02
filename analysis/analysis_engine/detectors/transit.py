# Copied from: analysis phase cidecode hackathon/analysis phase cidecode hackathon/analysis_engine/detectors/transit.py
"""Pattern 3: pass-through/routing accounts whose inflow and outflow nearly balance."""

from __future__ import annotations

import sqlite3

import networkx as nx
import pandas as pd

from ..config import AnalysisConfig
from .common import make_finding


def detect_transit_accounts(
    connection: sqlite3.Connection,
    baseline: dict,
    graph: nx.MultiDiGraph,
    config: AnalysisConfig,
) -> list:
    del config
    thresholds = baseline["thresholds"]
    accounts = pd.read_sql_query("SELECT * FROM accounts", connection)
    findings = []
    for row in accounts.itertuples(index=False):
        if (
            float(row.throughput_ratio) < thresholds["transit_throughput_ratio_min"]
            or float(row.total_volume) < thresholds["transit_volume_min"]
            or int(row.transaction_count) < thresholds["transit_transaction_count_min"]
        ):
            continue
        node = str(row.account_id)
        incident = list(graph.in_edges(node, keys=True, data=True)) + list(
            graph.out_edges(node, keys=True, data=True)
        )
        counterparties = {edge[0] for edge in incident if edge[0] != node} | {
            edge[1] for edge in incident if edge[1] != node
        }
        if len(counterparties) < 2:
            continue
        txn_ids = [
            txn_id
            for _, _, _, edge in incident
            for txn_id in edge.get("txn_ids", [edge["txn_id"]])
        ]
        findings.append(
            make_finding(
                connection,
                3,
                [node],
                txn_ids,
                f"Account {node} processed {row.total_volume:.2f} in combined inflow/outflow with a {row.throughput_ratio:.1%} throughput ratio across multiple counterparties, consistent with pass-through use.",
                {
                    "total_credit": float(row.total_credit),
                    "total_debit": float(row.total_debit),
                    "total_volume": float(row.total_volume),
                    "throughput_ratio": float(row.throughput_ratio),
                    "counterparty_count": len(counterparties),
                    "severity_tier": "moderate",
                    "runtime_thresholds": {
                        "throughput_ratio_min": thresholds["transit_throughput_ratio_min"],
                        "volume_min": thresholds["transit_volume_min"],
                        "transaction_count_min": thresholds["transit_transaction_count_min"],
                    },
                },
            )
        )
    return findings
