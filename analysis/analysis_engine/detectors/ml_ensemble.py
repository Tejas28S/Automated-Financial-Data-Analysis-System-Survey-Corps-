"""Pattern 23: lightweight graph-feature ML ensemble lead generation."""

from __future__ import annotations

import sqlite3

import networkx as nx
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.neighbors import LocalOutlierFactor

from ..config import AnalysisConfig
from ..models import Finding, pattern_key
from .common import make_finding


def _strong_accounts(findings_by_pattern: dict[str, list[Finding]]) -> set[str]:
    accounts: set[str] = set()
    for key, findings in findings_by_pattern.items():
        try:
            pid = int(key.split("_", 1)[0])
        except ValueError:
            pid = 0
        if pid in {21, 22, 23}:
            continue
        for finding in findings:
            if finding.evidence_strength == "strong":
                accounts.update(finding.accounts)
    return accounts


def detect_ml_ensemble_anomaly_leads(
    connection: sqlite3.Connection,
    baseline: dict,
    graph: nx.MultiDiGraph,
    config: AnalysisConfig,
    findings_by_pattern: dict[str, list[Finding]],
) -> list[Finding]:
    del baseline
    accounts = pd.read_sql_query("SELECT * FROM accounts", connection)
    if len(accounts) < 5:
        return []
    strong = _strong_accounts(findings_by_pattern)
    rows = []
    for row in accounts.itertuples(index=False):
        account_id = str(row.account_id)
        if account_id in strong:
            continue
        in_degree = graph.in_degree(account_id) if graph.has_node(account_id) else 0
        out_degree = graph.out_degree(account_id) if graph.has_node(account_id) else 0
        rows.append({
            "account_id": account_id,
            "transaction_count": float(row.transaction_count),
            "total_volume": float(row.total_volume),
            "throughput_ratio": float(row.throughput_ratio),
            "unique_counterparty_count": float(row.unique_counterparty_count),
            "in_degree": float(in_degree),
            "out_degree": float(out_degree),
        })
    features = pd.DataFrame(rows)
    if len(features) < 5:
        return []
    matrix = features.drop(columns=["account_id"]).replace([np.inf, -np.inf], 0).fillna(0.0)
    contamination = min(0.2, max(0.05, 2 / max(len(matrix), 1)))

    iso = IsolationForest(random_state=42, contamination=contamination).fit(matrix)
    iso_flags = iso.predict(matrix) == -1
    lof_flags = LocalOutlierFactor(n_neighbors=max(2, min(20, len(matrix) - 1)), contamination=contamination).fit_predict(matrix) == -1
    hbos_score = matrix.apply(
        lambda col: (col - col.median()).abs() / (((col - col.median()).abs().median()) or 1.0)
    )
    hbos_flags = hbos_score.sum(axis=1) >= hbos_score.sum(axis=1).quantile(1.0 - contamination)

    findings: list[Finding] = []
    for position, (_, row) in enumerate(features.iterrows()):
        fired = [
            name for name, flags in {
                "isolation_forest": iso_flags,
                "local_outlier_factor": lof_flags,
                "hbos": hbos_flags.to_numpy(),
            }.items() if bool(flags[position])
        ]
        if len(fired) < 2:
            continue
        acct = str(row["account_id"])
        corroborates = [
            f.finding_id
            for key, items in findings_by_pattern.items()
            if key != pattern_key(23)
            for f in items
            if acct in f.accounts
        ]
        findings.append(
            make_finding(
                connection,
                23,
                [acct],
                [],
                f"Account {acct} was flagged by {len(fired)} independent graph-feature anomaly models.",
                {
                    "models_fired": fired,
                    "model_count": len(fired),
                    "feature_values": row.drop(labels=["account_id"]).to_dict(),
                    "corroborates": corroborates,
                    "detection_method": "ml_ensemble_anomaly_lead",
                    "runtime_thresholds": {"consensus_min_models": 2, "contamination": contamination},
                },
                evidence_strength="lead",
            )
        )
        if len(findings) >= config.ml_lead_max_accounts:
            break
    return findings
