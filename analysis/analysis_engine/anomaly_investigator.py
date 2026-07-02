# New module for analysis phase final implementation.
"""Pattern 22: capped LLM-investigated anomaly leads."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import numpy as np
import pandas as pd

from .config import AnalysisConfig
from .database import fetch_transactions
from .llm_client import GroqKeyRotatingClient
from .models import Finding, pattern_key
from .utils import safe_ratio


def _zscore(series: pd.Series) -> pd.Series:
    clean = pd.to_numeric(series, errors="coerce").fillna(0.0)
    std = float(clean.std(ddof=0))
    if std <= 0:
        return pd.Series([0.0] * len(clean), index=clean.index)
    return (clean - float(clean.mean())) / std


def detect_llm_investigated_anomalies(
    connection: sqlite3.Connection,
    baseline: dict[str, Any],
    findings_by_pattern: dict[str, list[Finding]],
    config: AnalysisConfig,
    llm_client: GroqKeyRotatingClient | None = None,
    manual_account_ids: list[str] | None = None,
) -> list[Finding]:
    accounts = pd.read_sql_query("SELECT * FROM accounts", connection)
    if accounts.empty:
        return [_run_level_status("not_triggered", "No accounts were available for anomaly investigation.")]

    matched_accounts: set[str] = set()
    for key, findings in findings_by_pattern.items():
        try:
            pid = int(key.split("_", 1)[0])
        except (ValueError, IndexError):
            continue
        if pid < 1 or pid > 19:
            continue
        for finding in findings:
            if getattr(finding, "evidence_strength", "") == "strong":
                matched_accounts.update(finding.accounts)

    velocity = pd.to_numeric(accounts["transaction_count"], errors="coerce").fillna(0.0)
    volume = pd.to_numeric(accounts["total_volume"], errors="coerce").fillna(0.0)
    throughput = pd.to_numeric(accounts["throughput_ratio"], errors="coerce").fillna(0.0)
    counterparties = pd.to_numeric(accounts["unique_counterparty_count"], errors="coerce").fillna(0.0)
    retention = accounts.apply(
        lambda row: abs(float(row.total_credit) - float(row.total_debit)) / max(float(row.total_credit), float(row.total_debit), 1.0),
        axis=1,
    )
    accounts = accounts.copy()
    accounts["anomaly_score"] = (
        _zscore(velocity).clip(lower=0)
        + _zscore(volume).clip(lower=0)
        + _zscore(throughput).abs()
        + _zscore(counterparties).clip(lower=0)
        + _zscore(retention).abs()
    )
    cutoff = float(accounts["anomaly_score"].quantile(config.llm_role_b_auto_percentile))
    auto = accounts[
        (accounts["anomaly_score"] >= cutoff)
        & (~accounts["account_id"].astype(str).isin(matched_accounts))
    ].sort_values(["anomaly_score", "account_id"], ascending=[False, True])
    auto_ids = auto["account_id"].astype(str).head(config.llm_role_b_max_auto_accounts).tolist()
    manual_ids = list(dict.fromkeys(str(a) for a in (manual_account_ids or []) if str(a)))[: config.llm_role_b_max_manual_accounts]
    trigger_ids = list(dict.fromkeys(auto_ids + manual_ids))

    thresholds = {
        "anomaly_percentile": config.llm_role_b_auto_percentile,
        "score_cutoff": cutoff,
        "max_auto_accounts": config.llm_role_b_max_auto_accounts,
        "max_manual_accounts": config.llm_role_b_max_manual_accounts,
    }
    if not trigger_ids:
        return [
            _run_level_status(
                "not_triggered",
                "Pattern 22 did not trigger because no high-anomaly account had zero strong-tier deterministic findings.",
                thresholds,
            )
        ]

    findings: list[Finding] = []
    unavailable = llm_client is None or not llm_client.available
    for account_id in trigger_ids:
        row = accounts[accounts["account_id"].astype(str) == account_id].iloc[0]
        account_txns = fetch_transactions(
            connection,
            "account_id = ? AND eligible_for_detection = 1",
            (account_id,),
        ).sort_values(["date", "time", "source_order", "row_id"])
        txn_ids = account_txns["txn_id"].astype(str).head(200).tolist()
        details = {
            "trigger_reason": "manual" if account_id in manual_ids and account_id not in auto_ids else "automatic_score_based",
            "trigger_status": "triggered_no_finding",
            "anomaly_score": float(row["anomaly_score"]),
            "runtime_thresholds": thresholds,
            "llm_unavailable": unavailable,
            "source_documents": [],
            "explanation_source": "template",
        }
        explanation = (
            f"Account {account_id} met the Pattern 22 anomaly trigger, but no additional "
            "AI-assisted lead was added because the LLM was unavailable or disabled."
            if unavailable
            else f"Account {account_id} met the Pattern 22 anomaly trigger for manual review."
        )
        if not unavailable:
            sample = account_txns[
                ["txn_id", "date", "narration", "debit_amount", "credit_amount", "balance", "counterparty_account"]
            ].head(300).to_dict(orient="records")
            result = llm_client.chat_json(
                [
                    {
                        "role": "system",
                        "content": (
                            "You are reviewing one anomalous bank account that matched no deterministic "
                            "fraud pattern. Return JSON with keys found_notable_pattern, explanation, txn_ids. "
                            "Only cite transaction IDs present in the input."
                        ),
                    },
                    {"role": "user", "content": json.dumps({"account_id": account_id, "transactions": sample}, default=str)},
                ]
                ,
                call_context="pattern_22_investigation",
            )
            details.setdefault("llm_rotation_events", []).extend(result.rotation_events)
            if result.ok:
                try:
                    payload = json.loads(result.content)
                except json.JSONDecodeError:
                    payload = {}
                if payload.get("found_notable_pattern"):
                    details["trigger_status"] = "triggered_with_finding"
                    explanation = str(payload.get("explanation") or explanation)
                    cited = [str(t) for t in payload.get("txn_ids", []) if str(t) in set(account_txns["txn_id"].astype(str))]
                    txn_ids = cited or txn_ids
                    details["explanation_source"] = "groq"
                else:
                    explanation = str(payload.get("explanation") or "The LLM reviewed the anomaly trigger and found no specific additional pattern.")
                    details["explanation_source"] = "groq"
            else:
                details["llm_errors"] = [result.error]

        findings.append(
            Finding(
                pattern_id=22,
                pattern_name="llm_investigated_anomalies",
                accounts=[account_id],
                txn_ids=txn_ids,
                explanation=explanation,
                confidence_tier="low",
                detection_method="llm_investigated_anomaly",
                details=details,
            )
        )
    return findings


def _run_level_status(
    status: str,
    explanation: str,
    thresholds: dict[str, Any] | None = None,
) -> Finding:
    return Finding(
        pattern_id=22,
        pattern_name="llm_investigated_anomalies",
        accounts=[],
        txn_ids=[],
        explanation=explanation,
        confidence_tier="low",
        detection_method="llm_investigated_anomaly",
        details={
            "trigger_status": status,
            "trigger_reason": "run_level",
            "runtime_thresholds": thresholds or {},
            "source_documents": [],
            "explanation_source": "template",
        },
    )
