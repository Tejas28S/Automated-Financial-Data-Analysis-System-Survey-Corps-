# Copied from: analysis phase cidecode hackathon/analysis phase cidecode hackathon/analysis_engine/detectors/accumulation.py
"""Pattern 4: fund pooling account with many-source inflow and limited onward outflow."""

from __future__ import annotations

import sqlite3

import networkx as nx
import pandas as pd

from ..config import AnalysisConfig
from ..utils import safe_ratio
from .common import make_finding


def detect_accumulation_accounts(
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
        ratio = safe_ratio(float(row.total_debit), float(row.total_credit))
        if (
            float(row.total_credit) < thresholds["accumulation_credit_min"]
            or ratio > thresholds["accumulation_outflow_to_inflow_max"]
            or int(row.unique_counterparty_count) < thresholds["accumulation_unique_counterparty_min"]
        ):
            continue
        node = str(row.account_id)
        inbound = list(graph.in_edges(node, keys=True, data=True))
        inbound_sources = {source for source, _, _, _ in inbound}
        if len(inbound_sources) < 2:
            continue
        txn_ids = [
            txn_id
            for _, _, _, edge in inbound
            for txn_id in edge.get("txn_ids", [edge["txn_id"]])
        ]
        findings.append(
            make_finding(
                connection,
                4,
                [node],
                txn_ids,
                f"Account {node} received {row.total_credit:.2f} from many counterparties while debiting only {ratio:.1%} of total credits, consistent with accumulation.",
                {
                    "total_credit": float(row.total_credit),
                    "total_debit": float(row.total_debit),
                    "outflow_to_inflow_ratio": ratio,
                    "unique_counterparty_count": int(row.unique_counterparty_count),
                    "observed_inbound_source_count": len(inbound_sources),
                    "runtime_thresholds": {
                        "credit_min": thresholds["accumulation_credit_min"],
                        "outflow_to_inflow_max": thresholds["accumulation_outflow_to_inflow_max"],
                        "unique_counterparty_min": thresholds["accumulation_unique_counterparty_min"],
                    },
                },
            )
        )
    return findings
