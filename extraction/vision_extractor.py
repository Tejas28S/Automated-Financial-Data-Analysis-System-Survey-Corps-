"""
vision_extractor.py — Read bank-statement IMAGES directly with a vision LLM.

WHY THIS REPLACES OCR FOR IMAGES (Problem 1):
    Tesseract OCR works for clean scans but fails badly on the real-world images
    investigators actually receive: WhatsApp forwards, mobile-camera photos of a
    laptop screen, screenshots at an angle. On those, OCR returns garbage and the
    pipeline ended up reading the *filename* instead of the picture — so the
    account showed up as "whatsapp image" / "unknown".

    For image files we now skip Tesseract entirely and send the image straight to
    the Groq vision model, asking it to read EVERY field it can see and return a
    single structured JSON object. Blurred areas are read as best as possible;
    only the parts that genuinely cannot be read at all are marked "UNREADABLE"
    (we never silently skip a blurred section).

WHAT IT READS FROM THE IMAGE CONTENT (Problem 2):
    Not just the transaction rows — also the account identity printed on the
    statement: account holder, account number, IFSC code, bank name, branch,
    account type, statement period, opening and closing balance. These come from
    the document itself, never from the filename.

PRIVACY (Problem 3 — the one honest exception):
    A vision model has to see the pixels to read them, so the raw image is the one
    unavoidable place data leaves the machine — this is the exception the CID
    privacy rules already grant (INSTRUCTIONS §6). We limit it to image files,
    use the dedicated GROQ2 key, and after the call the real identifiers live only
    on the local machine. There is NO second external call for an image, so no
    other service ever sees the data.

Team: Survey Corps | CIDECODE Hackathon 2026 | CID Karnataka
"""

import base64
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List

from groq import Groq

from config.settings import GROQ2_KEY, GROQ_VISION_MODEL, LLM_CACHE_DIR

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 2

# Bump this if the prompt/output shape changes, so old cache entries are ignored.
_CACHE_VERSION = "v1"

# The exact instruction we give the vision model. It is written as a strict OCR +
# structuring task so the model returns data, not conversation.
VISION_EXTRACTION_PROMPT = (
    "You are a bank-statement reading engine for a police financial investigation.\n"
    "Read THIS IMAGE of a bank statement and return ALL information you can see.\n"
    "\n"
    "Return ONLY a single JSON object (no markdown, no backticks, no commentary) "
    "with exactly this shape:\n"
    "{\n"
    '  "account_details": {\n'
    '    "account_holder": "",\n'
    '    "account_number": "",\n'
    '    "ifsc_code": "",\n'
    '    "bank_name": "",\n'
    '    "branch": "",\n'
    '    "account_type": "",\n'
    '    "statement_period": "",\n'
    '    "opening_balance": "",\n'
    '    "closing_balance": ""\n'
    "  },\n"
    '  "transactions": [\n'
    '    {"date": "DD/MM/YYYY", "narration": "", "debit": "", "credit": "", "balance": ""}\n'
    "  ]\n"
    "}\n"
    "\n"
    "RULES:\n"
    "- Read every field and every transaction row you can see, top to bottom.\n"
    "- If an area is blurred, still read as much as you can make out.\n"
    "- Put the literal word UNREADABLE only in the specific field/value you truly "
    "cannot read at all. Never drop or skip a blurred row — include it with "
    "UNREADABLE in the parts you cannot read.\n"
    "- For each transaction, put the amount in 'debit' if money left the account, "
    "in 'credit' if money came in; leave the other one empty.\n"
    "- Use empty string \"\" for a field that is simply not present on the statement.\n"
)


def _image_to_data_url(image_path: str) -> str:
    """Reads an image file and encodes it as a base64 data URL for the vision API."""
    ext = Path(image_path).suffix.lower()
    mime = "image/png" if ext == ".png" else "image/jpeg"
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return f"data:{mime};base64,{b64}"


def _parse_vision_json(raw: str) -> Dict[str, Any]:
    """
    Turns the model's text reply into a dict, tolerating stray markdown fences.

    Returns an empty-but-valid structure if the reply cannot be parsed as JSON,
    so callers always get a usable shape rather than an exception.
    """
    cleaned = raw.strip()
    if "```" in cleaned:
        cleaned = cleaned.replace("```json", "").replace("```", "").strip()
    # Trim anything before the first '{' / after the last '}' (models sometimes add a note).
    if "{" in cleaned and "}" in cleaned:
        cleaned = cleaned[cleaned.index("{"): cleaned.rindex("}") + 1]
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        logger.warning("vision_extractor._parse_vision_json: could not parse JSON: %s", e)
        return {"account_details": {}, "transactions": [], "_parse_error": True}

    data.setdefault("account_details", {})
    data.setdefault("transactions", [])
    return data


def extract_statement_from_image(image_path: str) -> Dict[str, Any]:
    """
    Sends a statement image to the Groq vision model and returns structured data.

    Parameters:
        image_path (str): Absolute path to a .jpg/.jpeg/.png statement image.

    Returns:
        dict: {
            "account_details": {... the fields read from the image ...},
            "transactions": [ {date, narration, debit, credit, balance}, ... ],
            "engine": "groq_vision",
            "raw_chars": int,        # length of the model's raw reply (for visibility)
        }
        On total failure returns empty account_details/transactions with engine
        "failed" so the pipeline can record it without crashing.

    Raises:
        RuntimeError: if the GROQ2 vision key is missing (fail loud, not silent).
    """
    # ── Cache check FIRST — a repeat of the same image costs ZERO tokens ──────
    # The cache key is the image's own bytes (so the exact same photo always hits
    # the cache) plus the model + prompt version. Re-running tests/demos on the
    # same images therefore makes no new Groq calls.
    with open(image_path, "rb") as f:
        image_bytes = f.read()
    cache_key = hashlib.md5(
        image_bytes + f"{GROQ_VISION_MODEL}{_CACHE_VERSION}".encode("utf-8")
    ).hexdigest()
    cache_file = LLM_CACHE_DIR / f"vision_{cache_key}.json"

    if cache_file.exists():
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["engine"] = "groq_vision"
            data["source"] = "cache"  # re-used — no API call, no tokens spent
            logger.info(
                "vision_extractor.extract_statement_from_image: CACHE HIT for '%s' "
                "(no API call, 0 tokens).", Path(image_path).name,
            )
            return data
        except Exception as e:
            logger.warning("vision_extractor: bad cache file %s (%s); re-reading.", cache_file, e)
            cache_file.unlink(missing_ok=True)

    if not GROQ2_KEY:
        raise RuntimeError(
            "GROQ2 key not found in .env — add it before reading statement images "
            "(it is the key used for the vision reader)."
        )

    logger.info("vision_extractor.extract_statement_from_image: reading '%s' (Groq vision)", Path(image_path).name)
    client = Groq(api_key=GROQ2_KEY)
    data_url = f"data:{('image/png' if Path(image_path).suffix.lower() == '.png' else 'image/jpeg')};base64," \
               + base64.b64encode(image_bytes).decode("utf-8")

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=GROQ_VISION_MODEL,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_url}},
                        {"type": "text", "text": VISION_EXTRACTION_PROMPT},
                    ],
                }],
                temperature=0,       # deterministic reading, no creativity
                max_tokens=8000,     # room for many transaction rows
                # Force the model to emit syntactically valid JSON so parsing is
                # reliable even when a narration contains odd characters.
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content.strip()
            data = _parse_vision_json(raw)
            data["engine"] = "groq_vision"
            data["raw_chars"] = len(raw)
            data["source"] = "groq"  # a fresh API call (spent tokens)
            # Save to cache so the next run on this exact image costs nothing.
            try:
                with open(cache_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, default=str)
            except Exception as e:
                logger.warning("vision_extractor: could not write cache %s (%s)", cache_file, e)
            logger.info(
                "vision_extractor.extract_statement_from_image: "
                "read %d transaction(s); account_number=%s",
                len(data.get("transactions", [])),
                data.get("account_details", {}).get("account_number", "?"),
            )
            return data
        except Exception as err:
            logger.warning(
                "vision_extractor.extract_statement_from_image: attempt %d failed: %s",
                attempt, err,
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)

    logger.error("vision_extractor.extract_statement_from_image: all attempts failed for '%s'", image_path)
    return {"account_details": {}, "transactions": [], "engine": "failed", "raw_chars": 0}
