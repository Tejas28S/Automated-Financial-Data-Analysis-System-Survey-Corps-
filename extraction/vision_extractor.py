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
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

from groq import Groq

from config.settings import GROQ2_KEY, GROQ_VISION_MODEL, LLM_CACHE_DIR
# Reuse the size-bounded encoder + non-retryable check (no circular import:
# extractor_ocr does not import this module).
from extraction.extractor_ocr import _bounded_jpeg_data_url, _is_nonretryable, OCR_RENDER_DPI

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 2

# Bump this if the prompt/output shape changes, so old cache entries are ignored.
_CACHE_VERSION = "v1"
# Separate cache version for the text-transcription path (Stage 1 of the new image
# pipeline) so its cache never collides with the JSON reader's cache.
_TRANSCRIBE_CACHE_VERSION = "vtext_v1"

# Stage 1 prompt — the vision model's ONLY job is faithful transcription. It does
# NOT classify columns, split debit/credit, or emit JSON. Keeping output as plain,
# date-anchored lines means the proven deterministic text parser (the same one the
# digital-PDF path uses) can process every row downstream — the model reads pixels,
# code does the structuring. Shorter per-row output than JSON also means fewer
# truncations on long statements (more rows fit before the token cap).
VISION_TRANSCRIBE_PROMPT = (
    "You are a bank-statement TRANSCRIPTION engine for a police financial "
    "investigation. Read THIS IMAGE and transcribe it as plain text. Do NOT analyse, "
    "classify, summarise, or output JSON or a table grid.\n"
    "\n"
    "Output EXACTLY two sections, in this order:\n"
    "\n"
    "ACCOUNT DETAILS:\n"
    "Write one 'Label: value' line for each identity field that is visible — "
    "Account Holder, Account Number, IFSC, Bank Name, Branch, Account Type, "
    "Statement Period, Opening Balance, Closing Balance. Omit a field that is not "
    "shown. If this image is a continuation page with no account header at all, "
    "write the single line: ACCOUNT DETAILS: (none on this page)\n"
    "\n"
    "TRANSACTIONS:\n"
    "Write ONE LINE per transaction row, in the exact top-to-bottom order shown.\n"
    "Each line MUST begin with the transaction date, then the description text, then "
    "the money values exactly as printed, with the RUNNING BALANCE as the LAST number "
    "on the line. Separate fields with single spaces. Keep amounts exactly as printed "
    "(keep commas and decimals, e.g. 1,250.00).\n"
    "\n"
    "RULES:\n"
    "- Transcribe EVERY transaction row you can see; never skip or summarise a row.\n"
    "- If a debit/withdrawal OR credit/deposit cell is EMPTY, write NOTHING for it "
    "(no 0, no dash) — write only the amount that actually has a value, then the "
    "balance. So a normal row has just two numbers: the amount and the balance.\n"
    "- If a value is blurred and truly unreadable, write UNREADABLE in its place; "
    "never drop the row.\n"
    "- Do NOT output column headers, table borders, pipes, bullets, or row numbers.\n"
    "- Output nothing except the two sections described above.\n"
)

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
    '    {"date": "DD/MM/YYYY", "narration": "", "reference_number": "", "cheque_number": "", "debit": "", "credit": "", "balance": ""}\n'
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
    "- If a row shows a cheque/instrument number put it in 'cheque_number'; if it "
    "shows a transaction reference / RRN / UTR / transaction id put it in "
    "'reference_number'. Keep both OUT of the narration.\n"
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

    from extraction.key_pool import VISION_POOL, is_daily_quota_error, AllKeysExhausted
    logger.info("vision_extractor.extract_statement_from_image: reading '%s' (Groq vision)", Path(image_path).name)
    try:
        client, _pool_key = VISION_POOL.client()
    except AllKeysExhausted:
        raise RuntimeError(
            "No usable GROQ vision key found in .env — add GROQ2 (and optionally GROQ5) "
            "before reading statement images."
        )
    # Downscale + JPEG-encode so a full-resolution phone photo / large scan stays
    # under Groq Vision's request-size limit (avoids HTTP 413 "Entity Too Large").
    data_url = _bounded_jpeg_data_url(image_path)

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
            if is_daily_quota_error(err):
                VISION_POOL.mark_dead(_pool_key)
                try:
                    client, _pool_key = VISION_POOL.client()
                    logger.info("vision_extractor: rotated to a fresh vision key — retrying.")
                    continue
                except AllKeysExhausted:
                    logger.error("vision_extractor: all vision keys exhausted — stopping.")
                    break
            if _is_nonretryable(err):
                logger.error("vision_extractor.extract_statement_from_image: non-retryable "
                             "error (payload too large) — not retrying.")
                break
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)

    logger.error("vision_extractor.extract_statement_from_image: all attempts failed for '%s'", image_path)
    return {"account_details": {}, "transactions": [], "engine": "failed", "raw_chars": 0}


def transcribe_image_to_text(image_path: str) -> Dict[str, Any]:
    """
    Stage 1 of the redesigned image pipeline: VISION → TEXT ONLY.

    Sends a statement image to the Groq vision model and asks ONLY for a faithful
    plain-text transcription (an account-details header block + one date-anchored
    line per transaction). The model does NOT assign columns, split debit/credit,
    or emit JSON — that structuring is done downstream by deterministic code, the
    same proven parser the digital-PDF path uses.

    Why this is better than asking vision for JSON directly:
      • Completeness — shorter per-row output than JSON means fewer truncations on
        long statements (more rows fit before the token cap), and a deterministic
        parser never "gives up" mid-list the way a JSON generator can.
      • Auditability — the structuring step is plain code, not a model decision.
      • Continuation — the text of several images can be grouped and parsed together.

    The result is cached on disk keyed by the image bytes (re-running the same image
    costs 0 tokens). Returns:
        {"text": <transcription>, "engine": "groq_vision_text",
         "source": "groq"|"cache", "raw_chars": int}
    On total failure returns text="" with engine="failed" so the caller can fall
    back to the JSON reader (the accuracy floor) without crashing.
    """
    with open(image_path, "rb") as f:
        image_bytes = f.read()
    cache_key = hashlib.md5(
        image_bytes + f"{GROQ_VISION_MODEL}{_TRANSCRIBE_CACHE_VERSION}".encode("utf-8")
    ).hexdigest()
    cache_file = LLM_CACHE_DIR / f"vtext_{cache_key}.json"

    if cache_file.exists():
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["engine"] = "groq_vision_text"
            data["source"] = "cache"
            logger.info(
                "vision_extractor.transcribe_image_to_text: CACHE HIT for '%s' "
                "(no API call, 0 tokens).", Path(image_path).name,
            )
            return data
        except Exception as e:
            logger.warning("vision_extractor: bad transcription cache %s (%s); re-reading.",
                           cache_file, e)
            cache_file.unlink(missing_ok=True)

    from extraction.key_pool import VISION_POOL, is_daily_quota_error, AllKeysExhausted
    logger.info("vision_extractor.transcribe_image_to_text: reading '%s' (Groq vision, text)",
                Path(image_path).name)
    try:
        client, _pool_key = VISION_POOL.client()
    except AllKeysExhausted:
        raise RuntimeError(
            "No usable GROQ vision key found in .env — add GROQ2 (and optionally GROQ5)."
        )
    data_url = _bounded_jpeg_data_url(image_path)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=GROQ_VISION_MODEL,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_url}},
                        {"type": "text", "text": VISION_TRANSCRIBE_PROMPT},
                    ],
                }],
                temperature=0,        # deterministic reading
                max_tokens=8000,      # plenty of room; text is shorter than JSON per row
            )
            raw = response.choices[0].message.content.strip()
            data = {
                "text": raw,
                "engine": "groq_vision_text",
                "raw_chars": len(raw),
                "source": "groq",
            }
            try:
                with open(cache_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, default=str)
            except Exception as e:
                logger.warning("vision_extractor: could not write transcription cache %s (%s)",
                               cache_file, e)
            logger.info(
                "vision_extractor.transcribe_image_to_text: transcribed '%s' → %d chars",
                Path(image_path).name, len(raw),
            )
            return data
        except Exception as err:
            logger.warning(
                "vision_extractor.transcribe_image_to_text: attempt %d failed: %s",
                attempt, err,
            )
            if is_daily_quota_error(err):
                VISION_POOL.mark_dead(_pool_key)
                try:
                    client, _pool_key = VISION_POOL.client()
                    logger.info("vision_extractor: rotated to a fresh vision key — retrying.")
                    continue
                except AllKeysExhausted:
                    logger.error("vision_extractor: all vision keys exhausted — stopping.")
                    break
            if _is_nonretryable(err):
                logger.error("vision_extractor.transcribe_image_to_text: non-retryable "
                             "error — not retrying.")
                break
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)

    logger.error("vision_extractor.transcribe_image_to_text: all attempts failed for '%s'",
                 image_path)
    return {"text": "", "engine": "failed", "raw_chars": 0, "source": "failed"}


def extract_structured_from_scanned_pdf(
    file_path: str, max_pages: int = None
) -> Dict[str, Any]:
    """
    Scanned PDF → structured JSON via Vision model only (zero GROQ1 calls).

    Renders each page with PyMuPDF at OCR_RENDER_DPI, then sends each page to
    extract_statement_from_image (GROQ2 Vision). Account details come from the
    first page that has them; transactions are merged across all pages in order.
    Each page result is cached on disk — re-running the same PDF costs 0 tokens.

    This replaces the old dual-LLM path (Tesseract raw text → GROQ1 structuring)
    which burned the entire daily GROQ1 quota on a 6-page statement.
    """
    import fitz  # PyMuPDF

    doc = fitz.open(file_path)
    total_pages = len(doc)
    n_pages = min(total_pages, max_pages) if max_pages else total_pages

    all_account_details: Dict[str, Any] = {}
    all_transactions: List[Dict[str, Any]] = []
    fresh_calls = 0
    tmp_dir = tempfile.mkdtemp(prefix="survey_scanned_")

    try:
        for page_num in range(n_pages):
            page = doc[page_num]
            zoom = OCR_RENDER_DPI / 72.0
            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img_path = Path(tmp_dir) / f"page_{page_num + 1}.png"
            pix.save(str(img_path))

            result = extract_statement_from_image(str(img_path))
            if result.get("source") == "groq":
                fresh_calls += 1

            if not all_account_details and result.get("account_details"):
                all_account_details = result["account_details"]

            all_transactions.extend(result.get("transactions", []))
            logger.info(
                "vision_extractor.extract_structured_from_scanned_pdf: "
                "page %d/%d → %d txn(s), engine=%s, source=%s",
                page_num + 1, n_pages, len(result.get("transactions", [])),
                result.get("engine", "?"), result.get("source", "?"),
            )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        doc.close()

    return {
        "account_details": all_account_details,
        "transactions": all_transactions,
        "engine": "groq_vision_scanned",
        "raw_chars": sum(len(str(t)) for t in all_transactions),
        "source": "groq" if fresh_calls else "cache",
        "pages_processed": n_pages,
        "total_pages": total_pages,
        "llm_calls": fresh_calls,
    }
