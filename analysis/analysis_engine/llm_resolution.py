# New module for analysis phase final implementation.
"""Capped optional Role A counterparty assist hook.

The current deterministic counterparty resolver remains primary. This module
reviews only pre-filtered unresolved transaction/account candidates. A positive
LLM result updates counterparty fields so Pattern 8 can create low-confidence
``llm_inferred`` graph edges; this module never creates findings directly.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import sqlite3
from typing import Any

import pandas as pd

from .config import AnalysisConfig
from .llm_client import GroqKeyRotatingClient
from .utils import normalize_text


@dataclass
class CounterpartyCandidate:
    row_id: int
    txn_id: str
    account_id: str
    candidate_account_id: str
    candidate_holder: str
    narration: str
    date: str
    amount: float
    direction: str
    prefilter_score: float


@dataclass
class LLMAssistSummary:
    candidate_count: int = 0
    attempted_calls: int = 0
    inferred_edges: int = 0
    skipped_due_to_cap: int = 0
    unavailable: bool = False
    rotation_events: list[dict[str, Any]] | None = None

    def to_dict(self) -> dict[str, int | bool | list[dict[str, Any]]]:
        return {
            "candidate_count": self.candidate_count,
            "attempted_calls": self.attempted_calls,
            "inferred_edges": self.inferred_edges,
            "skipped_due_to_cap": self.skipped_due_to_cap,
            "unavailable": self.unavailable,
            "rotation_events": self.rotation_events or [],
        }


def _tokens(value: str) -> set[str]:
    return {
        token
        for token in normalize_text(value).split()
        if len(token) >= 3 and token not in {"THE", "AND", "BANK", "TRANSFER", "PAYMENT"}
    }


def _overlap_score(narration: str, holder: str) -> float:
    narration_tokens = _tokens(narration)
    holder_tokens = _tokens(holder)
    if not narration_tokens or not holder_tokens:
        return 0.0
    return len(narration_tokens & holder_tokens) / len(holder_tokens)


def build_counterparty_candidates(
    connection: sqlite3.Connection,
    limit: int | None = None,
) -> list[CounterpartyCandidate]:
    """Build cheap pre-filter candidates from unresolved eligible rows.

    The pre-filter is intentionally conservative: it only proposes candidate
    accounts when an observed account holder has token overlap with the
    unresolved narration. The LLM receives only one transaction and one
    candidate account at a time.
    """

    transactions = pd.read_sql_query(
        """
        SELECT row_id, txn_id, account_id, date, narration, debit_amount, credit_amount
        FROM transactions
        WHERE eligible_for_detection = 1
          AND COALESCE(counterparty_account, '') = ''
          AND COALESCE(narration, '') != ''
        """,
        connection,
    )
    accounts = pd.read_sql_query(
        """
        SELECT account_id, account_holder
        FROM accounts
        WHERE COALESCE(account_id, '') != ''
          AND COALESCE(account_holder, '') != ''
        """,
        connection,
    )
    candidates: list[CounterpartyCandidate] = []
    for row in transactions.itertuples(index=False):
        narration = str(row.narration or "")
        amount = max(float(row.debit_amount or 0), float(row.credit_amount or 0))
        direction = "debit" if float(row.debit_amount or 0) > 0 else "credit"
        for account in accounts.itertuples(index=False):
            candidate_account_id = str(account.account_id)
            if candidate_account_id == str(row.account_id):
                continue
            score = _overlap_score(narration, str(account.account_holder or ""))
            if score <= 0:
                continue
            candidates.append(
                CounterpartyCandidate(
                    row_id=int(row.row_id),
                    txn_id=str(row.txn_id),
                    account_id=str(row.account_id),
                    candidate_account_id=candidate_account_id,
                    candidate_holder=str(account.account_holder or ""),
                    narration=narration[:500],
                    date=str(row.date or ""),
                    amount=amount,
                    direction=direction,
                    prefilter_score=round(score, 4),
                )
            )
    candidates.sort(key=lambda item: (-item.prefilter_score, item.txn_id, item.candidate_account_id))
    return candidates[:limit] if limit is not None else candidates


def run_capped_counterparty_assist(
    config: AnalysisConfig,
    llm_client: GroqKeyRotatingClient | None,
    connection: sqlite3.Connection | None = None,
    candidate_count: int | None = None,
) -> LLMAssistSummary:
    """Run capped Role A candidate review, if an LLM client is available."""

    candidates = build_counterparty_candidates(connection) if connection is not None else []
    if candidate_count is None:
        candidate_count = len(candidates)

    summary = LLMAssistSummary(candidate_count=candidate_count)
    if candidate_count > config.llm_role_a_max_calls:
        summary.skipped_due_to_cap = candidate_count - config.llm_role_a_max_calls
    if llm_client is None or not llm_client.available:
        summary.unavailable = True
        return summary
    if connection is None:
        return summary

    for candidate in candidates[: config.llm_role_a_max_calls]:
        result = llm_client.chat_json(
            [
                {
                    "role": "system",
                    "content": (
                        "You compare one unresolved bank transaction narration with one observed "
                        "account holder. Return JSON only: is_match boolean and reasoning string. "
                        "Do not infer beyond the supplied transaction and candidate account."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(asdict(candidate), default=str, sort_keys=True),
                },
            ]
            ,
            call_context="counterparty_llm_assist",
        )
        summary.attempted_calls += 1
        if result.rotation_events:
            if summary.rotation_events is None:
                summary.rotation_events = []
            summary.rotation_events.extend(result.rotation_events)
        if not result.ok:
            continue
        try:
            payload = json.loads(result.content)
        except json.JSONDecodeError:
            continue
        if not bool(payload.get("is_match")):
            continue
        reasoning = str(payload.get("reasoning", "")).strip()
        connection.execute(
            """
            UPDATE transactions
            SET counterparty_account = ?,
                counterparty_name_raw = ?,
                counterparty_resolution_method = ?,
                counterparty_resolution_confidence = ?,
                llm_reasoning = ?
            WHERE row_id = ?
            """,
            (
                candidate.candidate_account_id,
                candidate.candidate_holder,
                "llm_inferred",
                0.40,
                reasoning,
                candidate.row_id,
            ),
        )
        summary.inferred_edges += 1
    connection.commit()
    return summary
