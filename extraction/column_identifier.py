"""
column_identifier.py — LLM-powered column structure identification for bank statements.

The biggest challenge in processing bank statements from different banks is that
each bank uses different column names and layouts:

  SBI:    "Txn Date | Description     | Debit  | Credit | Balance"
  HDFC:   "Date     | Narration       | Chq No | W/Drl  | Deposit | Balance"
  Canara: "Date     | Particulars     | Ref No | Debit  | Credit  | Balance"
  Kotak:  "Date     | Description     | Ref    | Withdrawal | Deposit | Balance"

The standardiser needs to know WHICH column position corresponds to each
standard field (Date, Narration, Debit, Credit, Balance) so it can extract
the right values from the right columns.

This module solves this by sending the first 40 lines of the document (the
header and first few rows) to Groq (a fast LLM API) and asking it to identify
the column structure. Groq returns a JSON object like:
    {"date": 0, "narration": 1, "debit": 2, "credit": 3, "balance": 4}

PRIVACY: Before sending anything to Groq, we call anonymiser.py to replace all
account numbers, names, IFSC codes, and phone numbers with placeholder codes.
Real financial data NEVER leaves the local machine through this module.

CACHING: The Groq API is called exactly once per document. The response is
saved to disk in storage/llm_cache/. On the next run with the same document,
the cached result is used and no API call is made. This saves API quota during
testing and ensures reproducibility.

Team: Survey Corps | CIDECODE Hackathon 2026 | CID Karnataka
"""

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Dict, Optional, Any

from groq import Groq

from config.settings import (
    GROQ1_KEY,  # Column identification uses the GROQ1 key only (see INSTRUCTIONS.md §7)
    GROQ_MODEL,
    COLUMN_ID_SAMPLE_LINES,
    LLM_CACHE_DIR,
)
from extraction.anonymiser import anonymise_text

# Set up a logger for this module.
logger = logging.getLogger(__name__)

# ── Default column mapping fallback ──────────────────────────────────────────
# If Groq is unavailable or returns malformed JSON after 3 retries,
# we fall back to this default mapping. This assumes the most common
# Indian bank statement layout: Date | Narration | Debit | Credit | Balance.
# The pipeline will continue with this fallback rather than crashing.
DEFAULT_COLUMN_MAP: Dict[str, Any] = {
    "date": 0,
    "narration": 1,
    "debit": 2,
    "credit": 3,
    "balance": 4,
}

# ── Groq API retry settings ────────────────────────────────────────────────
MAX_RETRIES = 3           # Number of times to retry the API call if it fails
RETRY_DELAY_SECONDS = 2   # Seconds to wait between retry attempts


def identify_column_structure(raw_text: str, file_path: str) -> Dict[str, Any]:
    """
    Uses the Groq LLM (key GROQ1) to identify which column in the bank statement
    corresponds to Date, Narration, Debit, Credit, and Balance.

    Called once per document regardless of how many rows it has. Only the first
    ~40 lines are sent to Groq, after anonymisation. The answer is cached on disk
    so re-runs of the same document make ZERO new API calls.

    VISIBILITY (Problem 1): instead of just returning a map, this returns a small
    result dict that says WHERE the map came from, so a hardcoded guess can never
    again be mistaken for a real Groq answer:

        {
          "column_map": {"date": 0, "narration": 1, "debit": 2, ...},
          "source": "groq" | "cache" | "fallback" | "empty",
          "cache_file": "<md5>.json",
          "groq_called": True/False,
        }

      - "groq"     → a fresh Groq API call produced this map (groq_called=True)
      - "cache"    → re-used a previous Groq answer from disk (no API call)
      - "fallback" → Groq failed/returned nonsense; we used the default map.
                     This is surfaced loudly, never hidden.
      - "empty"    → the document had no usable text to analyse.

    Parameters:
        raw_text (str): Full extracted text from the document.
                        Only the first COLUMN_ID_SAMPLE_LINES lines are used.
        file_path (str): Path to the original file, used only for log messages.

    Returns:
        dict: result dict described above. Downstream code reads result["column_map"].
    """
    logger.info(
        "column_identifier.identify_column_structure: "
        "Identifying column structure for '%s'",
        Path(file_path).name,
    )

    # ── Step 1: Extract the sample lines to send to Groq ──────────────────
    # We split the full text into lines and take only the first 40 lines.
    # This is always enough to see the header row and a few data rows,
    # which is all the LLM needs to understand the column structure.
    all_lines = [line for line in raw_text.splitlines() if line.strip()]
    sample_lines = all_lines[:COLUMN_ID_SAMPLE_LINES]
    sample_text = "\n".join(sample_lines)

    if not sample_text.strip():
        logger.warning(
            "column_identifier.identify_column_structure: "
            "Empty text received for '%s'. Returning default column map (source=empty).",
            Path(file_path).name,
        )
        return {
            "column_map": DEFAULT_COLUMN_MAP.copy(),
            "source": "empty",
            "cache_file": None,
            "groq_called": False,
        }

    # ── Step 2: Check if we have a cached result for this document ─────────
    # The cache key is the MD5 hash of the sample text. Two documents with
    # identical headers and first rows will share a cache entry. Re-running the
    # whole test suite therefore costs zero new Groq calls.
    cache_key = hashlib.md5(sample_text.encode("utf-8")).hexdigest()
    cache_file = LLM_CACHE_DIR / f"{cache_key}.json"

    if cache_file.exists():
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                cached_map = json.load(f)
            logger.info(
                "column_identifier.identify_column_structure: "
                "CACHE HIT for '%s' → %s (no API call made).",
                Path(file_path).name,
                cached_map,
            )
            return {
                "column_map": cached_map,
                "source": "cache",
                "cache_file": cache_file.name,
                "groq_called": False,
            }
        except Exception as cache_error:
            # If the cache file is corrupted, delete it and make a fresh API call.
            logger.warning(
                "column_identifier.identify_column_structure: "
                "Failed to read cache file '%s': %s. Making fresh API call.",
                cache_file,
                cache_error,
            )
            cache_file.unlink(missing_ok=True)

    # ── Step 3: Anonymise the sample text before sending to Groq ──────────
    # PRIVACY RULE: Account numbers, names, and other identifiers must be
    # replaced with placeholders before the text leaves this machine.
    # The LLM only needs to see the column structure, not real account numbers.
    anonymised_sample, _mapping = anonymise_text(sample_text)
    # NOTE: We intentionally discard the mapping here (_mapping) because
    # this is the anonymised sample for the LLM — we do not need to
    # de-anonymise the LLM's response (it only returns column indices/names).

    # ── Step 4: Call Groq API with retry logic ──────────────────────────────
    # Returns (map, source) where source is "groq" (real answer) or "fallback".
    column_map, source = _call_groq_with_retry(anonymised_sample, file_path)

    # ── Step 5: Save the result to cache (only real Groq answers) ────────────
    # We never cache a fallback guess — otherwise a one-off Groq outage would be
    # frozen onto the document forever. Caching only real answers means a later
    # re-run can still reach Groq and get the genuine map.
    if source == "groq":
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(column_map, f, indent=2)
            logger.info(
                "column_identifier.identify_column_structure: "
                "Saved Groq column map to cache: %s",
                cache_file.name,
            )
        except Exception as save_error:
            logger.warning(
                "column_identifier.identify_column_structure: "
                "Failed to save cache file '%s': %s",
                cache_file,
                save_error,
            )

    return {
        "column_map": column_map,
        "source": source,
        "cache_file": cache_file.name if source == "groq" else None,
        "groq_called": True,
    }


def _call_groq_with_retry(anonymised_sample: str, file_path: str) -> tuple:
    """
    Calls the Groq API (key GROQ1) to identify column structure, up to 3 retries.

    If the API call fails (network error, rate limit, invalid JSON response),
    it waits 2 seconds and tries again. After 3 failures, it logs a warning
    and returns the DEFAULT_COLUMN_MAP so the pipeline can continue — but it
    tags the result as "fallback" so this is never mistaken for a real answer.

    WHAT IS SENT TO GROQ:
        - The system prompt (instructions for the task)
        - The first 40 lines of the anonymised bank statement text
    WHAT IS NOT SENT TO GROQ:
        - Real account numbers (replaced with ACCT_1, ACCT_2, etc.)
        - Real names (replaced with NAME_1, NAME_2, etc.)
        - The full document (only 40 lines are sent)

    Parameters:
        anonymised_sample (str): The anonymised first 40 lines of the statement.
        file_path (str): Path to original file, used only for log messages.

    Returns:
        tuple: (column_map, source) where source is "groq" if Groq answered, or
               "fallback" if all retries failed and we used the default map.

    Raises:
        RuntimeError: if the GROQ1 key is missing — we refuse to silently pretend
                      Groq ran. (run_extraction_pipeline checks this up front too.)
    """
    # Build the client from the TEXT key pool (column identification is a text call).
    # With one configured key this is identical to before; with several it can rotate
    # off a daily-quota-exhausted key. Fails loudly if no key is configured at all.
    from extraction.key_pool import TEXT_POOL, is_daily_quota_error, AllKeysExhausted
    try:
        client, _pool_key = TEXT_POOL.client()
    except AllKeysExhausted:
        raise RuntimeError(
            "No usable GROQ text key found in .env — add GROQ1 (and optionally GROQ4) "
            "before running extraction (used for column identification)."
        )

    # ── System prompt: tells Groq what its job is ────────────────────────
    # This is the instruction we give the AI before showing it the document.
    system_prompt = (
        "You are a bank statement structure analyser for CID Karnataka police investigations.\n"
        "Your job is to identify which column in the bank statement contains which field.\n"
        "You will receive the first 30-40 lines of a bank statement.\n"
        "Return ONLY a JSON object with no explanation, no markdown, no backticks.\n"
        "The JSON must have exactly these keys: date, narration, debit, credit, balance.\n"
        "IF the first line is a header row of column names (e.g. CSV/Excel), each\n"
        "value MUST be the EXACT column header name as a string (copy it verbatim) —\n"
        "this is far more reliable than counting positions. Only use a 0-based integer\n"
        "index when there are no column-name headers at all.\n"
        "Watch out for distractor columns like 'Ref', 'Chq No', 'Ref No' — they are\n"
        "NOT debit/credit/balance; do not map any field to them.\n"
        "If a field is not present, set its value to null.\n"
        "\n"
        "Example response:\n"
        '{"date": 0, "narration": 1, "debit": 2, "credit": 3, "balance": 4}'
    )

    # ── User message: the actual document sample ─────────────────────────
    # This is what the AI sees as the "document" to analyse.
    # It contains only the anonymised text — no real account numbers.
    user_message = (
        f"Here are the first {len(anonymised_sample.splitlines())} lines of a bank statement. "
        f"Identify the column structure:\n\n{anonymised_sample}"
    )

    # ── Retry loop ────────────────────────────────────────────────────────
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info(
                "column_identifier._call_groq_with_retry: "
                "Calling Groq API (attempt %d of %d) for '%s'",
                attempt,
                MAX_RETRIES,
                Path(file_path).name,
            )

            # Make the API call to Groq.
            # We send the anonymised sample text and ask for a JSON column map.
            response = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                temperature=0,  # Temperature 0 means deterministic output (no randomness)
                max_tokens=200,  # The response is just a short JSON object
            )

            # Extract the text content from the API response.
            response_text = response.choices[0].message.content.strip()

            logger.info(
                "column_identifier._call_groq_with_retry: "
                "Groq response: %s",
                response_text,
            )

            # ── Parse the JSON response ───────────────────────────────────
            # Groq should return pure JSON, but sometimes it includes
            # markdown code fences (```json ... ```) — we strip those.
            cleaned_response = response_text
            if "```" in cleaned_response:
                # Remove markdown code blocks that some LLMs add unnecessarily
                cleaned_response = cleaned_response.replace("```json", "").replace("```", "").strip()

            column_map = json.loads(cleaned_response)

            # Validate that the response has the required keys.
            required_keys = {"date", "narration", "debit", "credit", "balance"}
            if not required_keys.issubset(column_map.keys()):
                raise ValueError(
                    f"Groq response is missing required keys. "
                    f"Got: {list(column_map.keys())}. "
                    f"Expected: {list(required_keys)}"
                )

            logger.info(
                "column_identifier._call_groq_with_retry: "
                "Successfully identified column map: %s",
                column_map,
            )
            return column_map, "groq"

        except json.JSONDecodeError as json_error:
            logger.warning(
                "column_identifier._call_groq_with_retry: "
                "Groq returned invalid JSON on attempt %d: %s. Response was: '%s'",
                attempt,
                json_error,
                response_text if 'response_text' in locals() else "N/A",
            )

        except Exception as api_error:
            logger.warning(
                "column_identifier._call_groq_with_retry: "
                "API call failed on attempt %d: %s",
                attempt,
                api_error,
            )
            # Daily-quota error → mark this key dead and rotate to the next text key.
            if is_daily_quota_error(api_error):
                TEXT_POOL.mark_dead(_pool_key)
                try:
                    client, _pool_key = TEXT_POOL.client()
                    logger.info("column_identifier: rotated to a fresh text key — retrying.")
                    continue
                except AllKeysExhausted:
                    logger.error("column_identifier: all text keys exhausted — stopping.")
                    break

        # Wait before retrying to avoid hitting rate limits immediately.
        if attempt < MAX_RETRIES:
            logger.info(
                "column_identifier._call_groq_with_retry: "
                "Waiting %d seconds before retry...",
                RETRY_DELAY_SECONDS,
            )
            time.sleep(RETRY_DELAY_SECONDS)

    # ── All retries exhausted ─────────────────────────────────────────────
    logger.warning(
        "column_identifier._call_groq_with_retry: "
        "All %d Groq API attempts failed for '%s'. "
        "Falling back to default column map (source=fallback, surfaced in metadata): %s",
        MAX_RETRIES,
        Path(file_path).name,
        DEFAULT_COLUMN_MAP,
    )
    return DEFAULT_COLUMN_MAP.copy(), "fallback"
