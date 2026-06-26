"""
llm_structurer.py — LLM-first document understanding for TEXT bank statements.

THE DESIGN (decided with the team):
    OCR / text extraction only pulls the raw text out of a statement. It does NOT
    try to understand it. The Groq LLM (GROQ1) then reads that full text and does
    the semantic work: it tells account metadata apart from transaction rows apart
    from headers/footers/page-numbers/summaries, and returns ONE strict JSON object:

        {
          "account_details": {account_holder_name, account_number, ifsc_code,
                              branch_name, branch_address, bank_name,
                              statement_period, currency, opening_balance,
                              closing_balance},
          "transactions": [{date, time, description, reference_number,
                            cheque_number, debit, credit, balance,
                            transaction_type}]
        }

    The clean JSON is the single source of truth — the CSV is generated ONLY from
    this validated JSON, never straight from OCR text. This is what makes the
    extraction generalise to ANY bank layout without per-bank regex.

LARGE STATEMENTS:
    A statement can have hundreds of rows — more than one LLM reply can hold. So we
    read the account metadata once (from the header) and read the transaction rows
    in CHUNKS of lines, merging the results. Each unique document is cached on disk,
    so re-running the same statement makes zero new API calls.

PRIVACY NOTE:
    Because the LLM must read the holder's name and the narrations, the statement
    text is sent to Groq. This is an explicit, team-approved choice favouring
    generalisation over the earlier "anonymise-first" rule. Everything else stays
    local; results are cached locally.

Team: Survey Corps | CIDECODE Hackathon 2026 | CID Karnataka
"""

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List

from groq import Groq

from config.settings import GROQ_EXTRACTION_KEY_POOL, GROQ_MODEL, LLM_CACHE_DIR

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 2

# Index of the currently active key in the pool — advances on 429 rate-limit errors.
_pool_idx: int = 0
_CACHE_VERSION = "structurer_v1"

# How many text lines to send per transaction-reading chunk. Kept SMALL so a single
# request (input + reserved output tokens) stays well under the free-tier limit of
# 12,000 tokens/minute — a larger chunk triggers HTTP 413 "request too large" and can
# never succeed (60-line chunks with 8k reserved output were ~16-24k tokens each).
CHUNK_LINES = 30
# A high SAFETY ceiling so a pathologically huge document can never run truly
# unbounded — but this is NOT the old silent cap. If a statement is so large it
# exceeds this, the unread tail is REPORTED (result["unprocessed_tail_lines"]) so
# the pipeline flags it for manual review; rows are never silently dropped.
# ~200 chunks × 60 lines ≈ 12,000 lines, far above any real statement.
SAFETY_MAX_CHUNKS = 200

# Account-metadata field names the LLM must return.
META_FIELDS = [
    "account_holder_name", "account_number", "ifsc_code", "branch_name",
    "branch_address", "bank_name", "statement_period", "currency",
    "opening_balance", "closing_balance",
]
# Transaction field names the LLM must return per row.
TXN_FIELDS = [
    "date", "time", "description", "reference_number", "cheque_number",
    "debit", "credit", "balance", "transaction_type",
]

_META_PROMPT = (
    "You are reading the HEADER of a bank statement for a police investigation.\n"
    "From the text, extract ONLY the account-level metadata. Return ONLY a JSON\n"
    "object with exactly these keys (use \"\" when a field is not present):\n"
    "{\n"
    '  "account_holder_name":"", "account_number":"", "ifsc_code":"",\n'
    '  "branch_name":"", "branch_address":"", "bank_name":"",\n'
    '  "statement_period":"", "currency":"", "opening_balance":"", "closing_balance":""\n'
    "}\n"
    "Read the FULL account holder name. Do not include transaction rows here.\n"
)

_TXN_PROMPT = (
    "You are reading lines from a bank statement for a police investigation.\n"
    "Identify ONLY the actual transaction rows. IGNORE headers, column titles,\n"
    "footers, page numbers, summary/relationship sections, and account metadata.\n"
    "Return ONLY a JSON object: {\"transactions\":[ ... ]} where each transaction is\n"
    "{\n"
    '  "date":"", "time":"", "description":"", "reference_number":"",\n'
    '  "cheque_number":"", "debit":"", "credit":"", "balance":"",\n'
    '  "transaction_type":""\n'
    "}\n"
    "Rules: put the amount in 'debit' if money LEFT the account (Dr / withdrawal),\n"
    "in 'credit' if money CAME IN (Cr / deposit); leave the other empty. Separate a\n"
    "date from a time. Put a cheque/instrument number in 'cheque_number' and a\n"
    "transaction reference / RRN / UTR / transaction id in 'reference_number' — keep\n"
    "both OUT of the description. Use \"\" for anything not present. A row that wraps\n"
    "across lines is ONE transaction.\n"
)


def _client(key: str) -> Groq:
    if not key:
        raise RuntimeError(
            "No GROQ extraction key available — add at least GROQ1 to .env."
        )
    return Groq(api_key=key)


def _call_json(system_prompt: str, user_text: str, max_tokens: int = 4000) -> Dict[str, Any]:
    """
    One Groq call that is forced to return a valid JSON object.

    Key rotation: on a 429 rate-limit error (per-minute OR daily quota) the pool
    index advances to the next available key and the call is retried immediately.
    All GROQ1–GROQ5 keys in .env are tried before giving up.

    413 (payload too large) is never retried — rotating keys cannot fix a request
    that is too big for the model's context window.
    """
    global _pool_idx

    if not GROQ_EXTRACTION_KEY_POOL:
        raise RuntimeError(
            "No GROQ extraction keys found in .env — add GROQ1 (and optionally "
            "GROQ3–GROQ5) before running extraction."
        )

    # We allow up to (pool_size × MAX_RETRIES) total attempts so every key gets
    # at least MAX_RETRIES tries before the call is abandoned.
    pool_size = len(GROQ_EXTRACTION_KEY_POOL)
    total_attempts = pool_size * MAX_RETRIES

    for attempt in range(1, total_attempts + 1):
        key = GROQ_EXTRACTION_KEY_POOL[_pool_idx % pool_size]
        client = _client(key)
        key_label = f"key[{_pool_idx % pool_size}]"
        try:
            resp = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text},
                ],
                temperature=0,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
            return json.loads(resp.choices[0].message.content)
        except Exception as err:
            es = str(err).lower()
            logger.warning(
                "llm_structurer._call_json: attempt %d/%d (%s) failed: %s",
                attempt, total_attempts, key_label, err,
            )

            # 413 = payload too large — rotating keys cannot fix this.
            if "413" in es or "too large" in es or "context_length_exceeded" in es:
                logger.error(
                    "llm_structurer._call_json: request too large (%s) — not retrying.",
                    key_label,
                )
                break

            # 429 = rate-limited — rotate to the next key in the pool.
            if "429" in es or "rate_limit" in es or "rate limit" in es:
                _pool_idx += 1
                next_label = f"key[{_pool_idx % pool_size}]"
                logger.warning(
                    "llm_structurer._call_json: rate-limited on %s — rotating to %s.",
                    key_label, next_label,
                )
                # No sleep needed — the new key has its own fresh quota.
                continue

            # Transient error — wait and retry on the same key.
            if attempt < total_attempts:
                time.sleep(RETRY_DELAY_SECONDS)

    return {}


def extract_account_metadata(raw_text: str, file_path: str = "") -> Dict[str, Any]:
    """
    Reads ONLY the account-level metadata from a statement's text (one Groq call on
    the header). This is the metadata path the team confirmed is working — it is
    shared by both structure_statement() and the digital-PDF schema-discovery flow,
    so metadata extraction stays identical everywhere.
    """
    lines = [ln for ln in (raw_text or "").splitlines() if ln.strip()]
    if not lines:
        return {f: "" for f in META_FIELDS}
    cache_key = hashlib.md5(("META" + "\n".join(lines[:60]) + _CACHE_VERSION).encode("utf-8")).hexdigest()
    cache_file = LLM_CACHE_DIR / f"meta_{cache_key}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            cache_file.unlink(missing_ok=True)
    # max_tokens kept small: the metadata JSON is tiny, and Groq's daily token
    # limit counts the RESERVED output tokens — a big reservation wastes quota.
    meta_raw = _call_json(_META_PROMPT, "\n".join(lines[:60]), max_tokens=900)
    account_details = {f: str(meta_raw.get(f, "") or "").strip() for f in META_FIELDS}
    # Only cache a REAL answer. A failed/empty call (e.g. rate limit) must never be
    # cached, or the empty result would be frozen onto this document.
    if any(account_details.values()):
        try:
            cache_file.write_text(json.dumps(account_details, indent=2), encoding="utf-8")
        except Exception:
            pass
    return account_details


# Schema-discovery prompt: the LLM looks at a SAMPLE of transaction rows and
# describes how to parse them — it never sees the full statement.
_SCHEMA_PROMPT = (
    "You are shown a SMALL SAMPLE of transaction rows from a bank statement.\n"
    "Do NOT extract the transactions. Instead DESCRIBE how to parse them, so that\n"
    "deterministic code can read the whole statement. Return ONLY this JSON:\n"
    "{\n"
    '  "date_format":"",            // e.g. "DD-MM-YYYY", "DD Mon YYYY"\n'
    '  "has_time": false,            // true if each row has a time\n'
    '  "time_format":"",            // e.g. "HH:MM:SS" or ""\n'
    '  "debit_credit_method":"",    // one of: "drcr_marker" (amount tagged Dr/Cr),\n'
    "                                //  \"two_columns\" (separate debit & credit columns),\n"
    "                                //  \"single_amount\" (one amount, direction from balance)\n"
    '  "has_reference_number": false,\n'
    '  "has_cheque_number": false,\n'
    '  "column_order": []           // e.g. ["date","narration","reference","debit","credit","balance"]\n'
    "}\n"
)

_SCHEMA_FIELDS = {
    "date_format": "", "has_time": False, "time_format": "",
    "debit_credit_method": "single_amount", "has_reference_number": True,
    "has_cheque_number": True, "column_order": [],
}


def discover_transaction_schema(sample_text: str, file_path: str = "") -> Dict[str, Any]:
    """
    ONE Groq call on a small sample of transaction rows → a parsing schema.

    This is the only LLM step for digital-PDF transactions. The schema is then
    applied by deterministic code to every row in the statement — so a statement
    with thousands of transactions still costs just this single call. Cached.
    """
    if not sample_text.strip():
        return {**_SCHEMA_FIELDS, "source": "empty"}
    cache_key = hashlib.md5(("SCHEMA" + sample_text + _CACHE_VERSION).encode("utf-8")).hexdigest()
    cache_file = LLM_CACHE_DIR / f"schema_{cache_key}.json"
    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            data["source"] = "cache"
            return data
        except Exception:
            cache_file.unlink(missing_ok=True)

    raw = _call_json(_SCHEMA_PROMPT, sample_text, max_tokens=600)
    schema = {**_SCHEMA_FIELDS}
    for k in _SCHEMA_FIELDS:
        if k in raw:
            schema[k] = raw[k]
    # A failed call returns {} → we keep the safe defaults but do NOT cache them, so
    # the real schema is discovered on a later run once the quota is available.
    if raw:
        schema["source"] = "groq"
        try:
            cache_file.write_text(json.dumps(schema, indent=2), encoding="utf-8")
        except Exception:
            pass
    else:
        schema["source"] = "fallback"
    logger.info("llm_structurer.discover_transaction_schema: %s", {k: schema[k] for k in _SCHEMA_FIELDS})
    return schema


def structure_statement(raw_text: str, file_path: str = "") -> Dict[str, Any]:
    """
    Turns the full OCR/extracted text of a statement into strict structured data.

    Returns:
        dict: {
          "account_details": {META_FIELDS...},
          "transactions": [ {TXN_FIELDS...}, ... ],
          "source": "groq" | "cache" | "empty",
        }
    """
    lines = [ln for ln in (raw_text or "").splitlines() if ln.strip()]
    if not lines:
        return {"account_details": {f: "" for f in META_FIELDS}, "transactions": [], "source": "empty"}

    # ── Cache: keyed on the document text, so re-runs cost nothing ────────────
    cache_key = hashlib.md5((raw_text + _CACHE_VERSION).encode("utf-8")).hexdigest()
    cache_file = LLM_CACHE_DIR / f"struct_{cache_key}.json"
    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            data["source"] = "cache"
            logger.info("llm_structurer.structure_statement: CACHE HIT for '%s' (0 tokens).",
                        Path(file_path).name)
            return data
        except Exception:
            cache_file.unlink(missing_ok=True)

    logger.info("llm_structurer.structure_statement: structuring '%s' (%d lines) via Groq.",
                Path(file_path).name, len(lines))

    # ── 1. Account metadata: one call on the header (first ~60 lines) ─────────
    # max_tokens kept small: the metadata JSON is tiny, and Groq's daily token
    # limit counts the RESERVED output tokens — a big reservation wastes quota.
    meta_raw = _call_json(_META_PROMPT, "\n".join(lines[:60]), max_tokens=900)
    account_details = {f: str(meta_raw.get(f, "") or "").strip() for f in META_FIELDS}

    # ── 2. Transactions: read the whole document in line-chunks and merge ─────
    # Every chunk is read — there is NO silent cap. The SAFETY ceiling only guards
    # against a pathologically huge file, and even then the unread tail is surfaced
    # (never dropped) so the pipeline flags it for manual review.
    transactions: List[Dict[str, Any]] = []
    all_chunks = [lines[i:i + CHUNK_LINES] for i in range(0, len(lines), CHUNK_LINES)]
    chunks = all_chunks[:SAFETY_MAX_CHUNKS]
    unread_chunks = all_chunks[SAFETY_MAX_CHUNKS:]
    for idx, chunk in enumerate(chunks, start=1):
        out = _call_json(_TXN_PROMPT, "\n".join(chunk))
        rows = out.get("transactions", []) if isinstance(out, dict) else []
        for r in rows:
            if isinstance(r, dict):
                transactions.append({f: str(r.get(f, "") or "").strip() for f in TXN_FIELDS})
        logger.info("llm_structurer.structure_statement: chunk %d/%d → %d txn(s).",
                    idx, len(chunks), len(rows))

    result = {"account_details": account_details, "transactions": transactions, "source": "groq"}

    # Surface (do NOT silently drop) any tail beyond the safety ceiling.
    if unread_chunks:
        unread_lines = sum(len(c) for c in unread_chunks)
        result["unprocessed_tail_lines"] = unread_lines
        logger.warning(
            "llm_structurer.structure_statement: '%s' exceeded the safety ceiling — "
            "%d tail line(s) were NOT read and are surfaced for manual review (never "
            "dropped). Consider splitting this statement.",
            Path(file_path).name, unread_lines,
        )

    # ── 3. Cache the structured result (only if the call actually produced data,
    # so a rate-limit/empty response is never frozen onto this document) ──────
    if transactions or any(account_details.values()):
        try:
            cache_file.write_text(json.dumps(result, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning("llm_structurer.structure_statement: cache write failed: %s", e)

    logger.info("llm_structurer.structure_statement: '%s' → %d transaction(s).",
                Path(file_path).name, len(transactions))
    return result
