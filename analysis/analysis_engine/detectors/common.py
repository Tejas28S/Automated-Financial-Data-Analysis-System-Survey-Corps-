# Copied from: analysis phase cidecode hackathon/analysis phase cidecode hackathon/analysis_engine/detectors/common.py
"""Finding construction helpers that preserve confidence and provenance."""

from __future__ import annotations

import sqlite3
import math
import re
from typing import Any, Iterable

from ..config import AnalysisConfig
from ..models import Finding, PATTERN_CATALOG, PATTERN_EVIDENCE_STRENGTH
from ..utils import normalize_text
from ..utils import lowest_confidence


VPA_RE = re.compile(r"(?<![A-Z0-9._-])([A-Z0-9._-]{2,}@[A-Z][A-Z0-9._-]{1,})(?![A-Z0-9._-])", re.IGNORECASE)
REFERENCE_TOKEN_RE = re.compile(r"[A-Z0-9]{4,}")
NARRATION_TOKEN_RE = re.compile(r"[A-Z][A-Z0-9]{2,}")
GENERIC_NARRATION_TOKENS = {
    "ATM", "WDL", "CASH", "DEPOSIT", "BRANCH", "COUNT", "COUNTER", "CHQ", "CHEQUE",
    "CLEARING", "TXN", "NEFT", "IMPS", "UPI", "RTGS", "P2P", "POS", "DR", "CR",
    "TO", "FROM", "PAYMENT", "TRANSFER", "HDFC", "SBI", "ICICI", "AXIS", "CANARA",
    "BANK", "MYSURU", "KORAMANGALA", "JAYANAGAR", "BLR", "NO",
    "UNRELATED", "BACKGROUND", "ANOTHER", "OUT", "IN",
}


def amount_frequency_key(amount: Any) -> str:
    try:
        value = float(amount)
    except (TypeError, ValueError):
        value = 0.0
    if not math.isfinite(value):
        value = 0.0
    return f"{round(value, 2):.2f}"


def amount_commonality_details(amount: Any, baseline: dict) -> dict[str, Any]:
    commonality = baseline.get("amount_commonality", {}) or {}
    key = amount_frequency_key(amount)
    counts = commonality.get("amount_frequency_counts", {}) or {}
    high_frequency = set(str(value) for value in commonality.get("high_frequency_amounts", []) or [])
    high_frequency.update(f"{float(value):.2f}" for value in commonality.get("high_frequency_amounts", []) or [])
    return {
        "amount_frequency_key": key,
        "amount_frequency_count": int(counts.get(key, 0) or 0),
        "amount_frequency_cutoff_count": float(commonality.get("frequency_cutoff_count", 0.0) or 0.0),
        "amount_common_frequency_quantile": float(commonality.get("frequency_quantile", 0.0) or 0.0),
        "is_high_frequency_amount": key in high_frequency,
    }


def is_high_frequency_amount(amount: Any, baseline: dict) -> bool:
    return bool(amount_commonality_details(amount, baseline)["is_high_frequency_amount"])


def reference_values(*values: Any) -> set[str]:
    refs: set[str] = set()
    for value in values:
        compact = re.sub(r"[^A-Z0-9]", "", normalize_text(value or ""))
        if len(compact) >= 4:
            refs.add(compact)
    return refs


def vpa_values(*values: Any) -> set[str]:
    vpas: set[str] = set()
    for value in values:
        vpas.update(match.lower() for match in VPA_RE.findall(str(value or "")))
    return vpas


def exact_reference_or_vpa_match(left: dict[str, Any], right: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    left_refs = reference_values(left.get("reference"), left.get("reference_alt"))
    right_refs = reference_values(right.get("reference"), right.get("reference_alt"))
    shared_refs = sorted(left_refs.intersection(right_refs))
    left_vpas = vpa_values(left.get("narration"))
    right_vpas = vpa_values(right.get("narration"))
    shared_vpas = sorted(left_vpas.intersection(right_vpas))
    return bool(shared_refs or shared_vpas), {
        "shared_references": shared_refs,
        "shared_vpas": shared_vpas,
    }


def narration_tokens(value: Any) -> set[str]:
    tokens = set()
    for token in NARRATION_TOKEN_RE.findall(normalize_text(value or "")):
        stripped = token.strip()
        if stripped in GENERIC_NARRATION_TOKENS:
            continue
        if stripped.startswith("TXN"):
            continue
        if stripped.isdigit():
            continue
        tokens.add(stripped)
    return tokens


def narration_similarity_details(left: Any, right: Any) -> dict[str, Any]:
    left_tokens = narration_tokens(left)
    right_tokens = narration_tokens(right)
    shared = sorted(left_tokens.intersection(right_tokens))
    denominator = max(len(left_tokens.union(right_tokens)), 1)
    score = len(shared) / denominator
    return {
        "narration_similarity_score": score,
        "shared_narration_tokens": shared,
        "left_narration_tokens": sorted(left_tokens),
        "right_narration_tokens": sorted(right_tokens),
    }


def corroboration_path_for_match(
    *,
    amount: Any,
    methods: Iterable[str],
    confidence: float,
    reference_match: bool,
    narration_match: bool = False,
    strict_counterparty_link: bool = False,
    baseline: dict,
    config: AnalysisConfig,
) -> tuple[bool, str, dict[str, Any]]:
    method_set = {str(method or "") for method in methods if str(method or "")}
    commonality = amount_commonality_details(amount, baseline)
    strict_confidence = config.amount_match_strict_counterparty_confidence_min

    if reference_match:
        return True, "reference_match", {
            **commonality,
            "counterparty_resolution_confidence": float(confidence or 0.0),
            "strict_counterparty_confidence_min": strict_confidence,
        }
    if narration_match:
        return True, "narration_match", {
            **commonality,
            "counterparty_resolution_confidence": float(confidence or 0.0),
            "strict_counterparty_confidence_min": strict_confidence,
        }
    if strict_counterparty_link and float(confidence or 0.0) >= strict_confidence:
        return True, "strict_counterparty_confidence", {
            **commonality,
            "counterparty_resolution_confidence": float(confidence or 0.0),
            "strict_counterparty_confidence_min": strict_confidence,
        }
    if not commonality["is_high_frequency_amount"]:
        return True, "rare_amount_mirror_match", {
            **commonality,
            "counterparty_resolution_confidence": float(confidence or 0.0),
            "strict_counterparty_confidence_min": strict_confidence,
        }
    return False, "blocked_common_amount_mirror_match", {
        **commonality,
        "counterparty_resolution_confidence": float(confidence or 0.0),
        "strict_counterparty_confidence_min": strict_confidence,
    }


def summarize_txn_ids(
    txn_ids: list[str],
    *,
    important_txn_ids: Iterable[str] = (),
    limit: int = 30,
) -> tuple[list[str], dict[str, Any]]:
    ordered = list(dict.fromkeys(str(value) for value in txn_ids if str(value)))
    if len(ordered) <= limit:
        return ordered, {"txn_id_count": len(ordered), "txn_ids_truncated": False}

    important = [txn_id for txn_id in ordered if txn_id in set(important_txn_ids)]
    head_count = max(1, limit - len(important))
    summarized = list(dict.fromkeys(important + ordered[:head_count]))[:limit]
    return summarized, {
        "txn_id_count": len(ordered),
        "reported_txn_id_count": len(summarized),
        "txn_ids_truncated": True,
        "omitted_txn_id_count": max(0, len(ordered) - len(summarized)),
    }


def transaction_context(
    connection: sqlite3.Connection,
    txn_ids: Iterable[str],
) -> dict[str, Any]:
    ids = list(dict.fromkeys(str(value) for value in txn_ids if str(value)))
    if not ids:
        return {"confidence_tier": "high", "flag_reasons": [], "documents": []}
    placeholders = ",".join("?" for _ in ids)
    rows = connection.execute(
        f"""
        SELECT txn_id, confidence_tier, flag_reason, doc_id, source_page
        FROM transactions WHERE txn_id IN ({placeholders})
        """,
        ids,
    ).fetchall()
    reasons = sorted({str(row["flag_reason"]) for row in rows if str(row["flag_reason"] or "")})
    documents = [
        {"txn_id": str(row["txn_id"]), "doc_id": str(row["doc_id"] or ""), "page": str(row["source_page"] or "")}
        for row in rows
    ]
    return {
        "confidence_tier": lowest_confidence(row["confidence_tier"] for row in rows),
        "flag_reasons": reasons,
        "documents": documents,
    }


def make_finding(
    connection: sqlite3.Connection,
    pattern_id: int,
    accounts: list[str],
    txn_ids: list[str],
    explanation: str,
    details: dict[str, Any] | None = None,
    *,
    evidence_strength: str | None = None,
    config: AnalysisConfig | None = None,
) -> Finding:
    report_limit = getattr(config, "finding_txn_id_report_limit", 200) if config else 200
    reported_txn_ids, txn_summary = summarize_txn_ids(txn_ids, limit=report_limit)
    context = transaction_context(connection, reported_txn_ids)
    finding_details = dict(details or {})
    if txn_summary.get("txn_ids_truncated"):
        finding_details.setdefault("evidence_summary", {}).update(txn_summary)
        finding_details.setdefault("runtime_thresholds", {})["finding_txn_id_report_limit"] = report_limit
    finding_details["source_documents"] = context["documents"]
    finding_details.setdefault("explanation_source", "template")
    if context["flag_reasons"]:
        finding_details["lower_confidence_flag_reasons"] = context["flag_reasons"]
    strength = evidence_strength or PATTERN_EVIDENCE_STRENGTH.get(pattern_id, "weak")
    min_confidence = finding_details.get("counterparty_resolution_confidence")
    if (
        strength == "strong"
        and min_confidence is not None
        and float(min_confidence) < (config.strong_counterparty_confidence_min if config else 0.65)
    ):
        strength = "weak"
        finding_details["downgrade_reason"] = "low_confidence_counterparty_match"
        finding_details.setdefault("runtime_thresholds", {})[
            "strong_counterparty_confidence_min"
        ] = config.strong_counterparty_confidence_min if config else 0.65
    if context["confidence_tier"]:
        finding_details.setdefault("source_transaction_confidence_tier", context["confidence_tier"])
    return Finding(
        pattern_id=pattern_id,
        pattern_name=PATTERN_CATALOG[pattern_id],
        accounts=accounts,
        txn_ids=reported_txn_ids,
        explanation=explanation,
        confidence_tier="",
        detection_method=finding_details.pop("detection_method", "deterministic"),
        details=finding_details,
        evidence_strength=strength,
    )


def edge_confidence_details(edges: Iterable[dict[str, Any]]) -> dict[str, Any]:
    edge_list = list(edges)
    values = [
        float(edge.get("counterparty_resolution_confidence", 1.0) or 1.0)
        for edge in edge_list
    ]
    methods = sorted({
        str(edge.get("counterparty_resolution_method", "") or "")
        for edge in edge_list
        if str(edge.get("counterparty_resolution_method", "") or "")
    })
    return {
        "counterparty_resolution_confidence": min(values) if values else 1.0,
        "counterparty_resolution_methods": methods,
    }
