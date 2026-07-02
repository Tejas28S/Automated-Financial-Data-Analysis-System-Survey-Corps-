"""Reusable directed money-flow graph with canonical mirrored transfers."""

from __future__ import annotations

import sqlite3

import networkx as nx
import pandas as pd

from .config import AnalysisConfig


def build_money_flow_graph(
    connection: sqlite3.Connection,
    config: AnalysisConfig,
) -> nx.MultiDiGraph:
    graph = nx.MultiDiGraph(pattern_id=8, pattern_name="money_flow_graph_construction")
    accounts = pd.read_sql_query("SELECT * FROM accounts", connection)
    observed_accounts = set(accounts["account_id"].astype(str))
    for row in accounts.itertuples(index=False):
        graph.add_node(
            str(row.account_id),
            observed_account=True,
            account_holder=str(row.account_holder or ""),
            bank_name=str(row.bank_name or ""),
            ifsc_code=str(row.ifsc_code or ""),
            total_volume=float(row.total_volume),
            throughput_ratio=float(row.throughput_ratio),
            unique_counterparty_count=int(row.unique_counterparty_count),
        )

    frame = pd.read_sql_query(
        """
        SELECT * FROM transactions
        WHERE eligible_for_detection = 1
          AND COALESCE(counterparty_account, '') != ''
        ORDER BY date, time, source_order, row_id
        """,
        connection,
    )
    consumed_pairs: set[str] = set()
    for row in frame.itertuples(index=False):
        amount = max(float(row.debit_amount), float(row.credit_amount))
        if amount <= config.money_epsilon:
            continue
        pair_id = str(row.ledger_pair_id or "")
        if pair_id:
            if pair_id in consumed_pairs or float(row.debit_amount) <= config.money_epsilon:
                continue
            pair_rows = frame[frame["ledger_pair_id"] == pair_id]
            txn_ids = pair_rows["txn_id"].astype(str).tolist()
            consumed_pairs.add(pair_id)
        else:
            txn_ids = [str(row.txn_id)]

        if float(row.debit_amount) > config.money_epsilon:
            source, target = str(row.account_id), str(row.counterparty_account)
        else:
            source, target = str(row.counterparty_account), str(row.account_id)
        if not source or not target or source == target:
            continue
        if source not in graph:
            graph.add_node(source, observed_account=source in observed_accounts)
        if target not in graph:
            graph.add_node(target, observed_account=target in observed_accounts)
        graph.add_edge(
            source,
            target,
            key=str(row.txn_id),
            amount=amount,
            date=str(row.date or ""),
            time=str(row.time or ""),
            txn_id=str(row.txn_id),
            txn_ids=txn_ids,
            confidence_tier=str(row.confidence_tier),
            flag_reason=str(row.flag_reason or ""),
            reference=str(row.reference or ""),
            ledger_pair_id=pair_id,
            source_document=str(row.doc_id or ""),
        )
    return graph


def observed_account_subgraph(graph: nx.MultiDiGraph) -> nx.MultiDiGraph:
    nodes = [node for node, data in graph.nodes(data=True) if data.get("observed_account")]
    return graph.subgraph(nodes).copy()
