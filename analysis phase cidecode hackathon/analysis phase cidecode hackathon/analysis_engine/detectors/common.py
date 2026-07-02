"""Finding construction helpers that preserve confidence and provenance."""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable

from ..models import Finding, PATTERN_CATALOG
from ..utils import lowest_confidence


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
) -> Finding:
    context = transaction_context(connection, txn_ids)
    finding_details = dict(details or {})
    finding_details["source_documents"] = context["documents"]
    if context["flag_reasons"]:
        finding_details["lower_confidence_flag_reasons"] = context["flag_reasons"]
    return Finding(
        pattern_id=pattern_id,
        pattern_name=PATTERN_CATALOG[pattern_id],
        accounts=accounts,
        txn_ids=txn_ids,
        explanation=explanation,
        confidence_tier=context["confidence_tier"],
        details=finding_details,
    )
