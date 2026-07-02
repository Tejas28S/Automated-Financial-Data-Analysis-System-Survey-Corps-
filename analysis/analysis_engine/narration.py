# New module for analysis phase final implementation.
"""Finding explanation helpers with template-first, optional Groq polish."""

from __future__ import annotations

import json
import re

from .llm_client import GroqKeyRotatingClient
from .models import Finding


_DATE_RE = re.compile(r"\b(?:\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})\b")
_ACCOUNT_TOKEN_RE = re.compile(r"\b(?:acct|account)[_-]?[A-Za-z0-9]{2,}\b", re.IGNORECASE)


def _validate_narration(text: str, finding: Finding) -> tuple[bool, str]:
    """Lightweight factual guardrail for LLM wording.

    The detector's structured evidence is authoritative. This check blocks the
    common failure mode where prose invents an account/date/value that is absent
    from the finding payload, then falls back to the deterministic template.
    """
    evidence = json.dumps(
        {
            "accounts": finding.accounts,
            "txn_ids": finding.txn_ids,
            "details": finding.details,
            "template_explanation": finding.explanation,
        },
        default=str,
        sort_keys=True,
    )
    evidence_lower = evidence.lower()
    if "$" in text or "USD" in text.upper():
        return False, "unsupported_currency_usd"
    for token in _ACCOUNT_TOKEN_RE.findall(text):
        if token.lower() not in evidence_lower:
            return False, f"unsupported_account_token:{token}"
    for date_text in _DATE_RE.findall(text):
        if date_text not in evidence:
            return False, f"unsupported_date:{date_text}"
    long_numbers = re.findall(r"\b\d{5,}\b", text.replace(",", ""))
    evidence_numbers = set(re.findall(r"\b\d{5,}\b", evidence.replace(",", "")))
    for number in long_numbers:
        if number not in evidence_numbers:
            return False, f"unsupported_numeric_claim:{number}"
    return True, "verified"


def explain_finding(finding: Finding, llm_client: GroqKeyRotatingClient | None = None) -> Finding:
    """Return the same finding with explanation_source recorded.

    The deterministic explanation already exists before this function is called.
    Groq may polish wording, but never changes the finding's accounts, txns,
    thresholds, score, or suspicious/not-suspicious decision.
    """

    finding.details.setdefault("explanation_source", "template")
    finding.narration = finding.narration or finding.explanation
    finding.narration_validation = finding.narration_validation or "verified"
    if llm_client is None or not llm_client.available:
        return finding
    payload = {
        "pattern_id": finding.pattern_id,
        "pattern_name": finding.pattern_name,
        "accounts": finding.accounts,
        "txn_ids": finding.txn_ids,
        "details": finding.details,
        "template_explanation": finding.explanation,
        "currency": "INR",
    }
    if finding.pattern_id == 8:
        payload["money_trail_narration_context"] = {
            "source_credit_context": finding.details.get("source_credit_context", {}),
            "allocations": [
                {
                    "debit_txn_id": allocation.get("debit_txn_id", ""),
                    "date": allocation.get("date", ""),
                    "debit_amount": allocation.get("debit_amount", 0),
                    "allocated_from_credit": allocation.get("allocated_from_credit", 0),
                    "narration": allocation.get("narration", ""),
                    "counterparty_account": allocation.get("counterparty_account", ""),
                    "counterparty_name_raw": allocation.get("counterparty_name_raw", ""),
                }
                for allocation in finding.details.get("allocations", [])[:20]
            ],
        }
    result = llm_client.chat_json(
        [
            {
                "role": "system",
                "content": (
                    "Given one finalized forensic finding as JSON, write a factual plain-English "
                    "investigator narration. Do not merely paraphrase the template_explanation; "
                    "add one sentence explaining investigative significance using only supplied "
                    "evidence. For money-trail findings, mention supplied narration/counterparty "
                    "context such as UPI handles, payee names, or NEFT names when present. "
                    "Currency is always ₹ INR; never use $, USD, dollars, or an unspecified foreign "
                    "currency. Do not invent account IDs, amounts, dates, severity, or links. "
                    "Return JSON with key explanation."
                ),
            },
            {"role": "user", "content": json.dumps(payload, default=str, sort_keys=True)},
        ],
        call_context="narration",
    )
    if not result.ok:
        finding.details.setdefault("llm_errors", []).append(result.error)
        finding.details.setdefault("llm_rotation_events", []).extend(result.rotation_events)
        return finding
    try:
        explanation = str(json.loads(result.content).get("explanation", "")).strip()
    except json.JSONDecodeError:
        explanation = ""
    if explanation:
        ok, validation = _validate_narration(explanation, finding)
        if ok:
            finding.narration = explanation
            finding.narration_validation = validation
            finding.details["explanation_source"] = "groq"
        else:
            finding.narration = finding.explanation
            finding.narration_validation = f"failed_fallback_used:{validation}"
            finding.details.setdefault("llm_validation_errors", []).append(validation)
        finding.details.setdefault("llm_rotation_events", []).extend(result.rotation_events)
    return finding
