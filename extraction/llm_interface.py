"""
llm_interface.py — The single seam between the extraction pipeline and the LLM.

WHY THIS EXISTS:
    The pipeline must not care WHICH LLM provider is used. Everything the pipeline
    needs from an LLM goes through the four functions below. Today they delegate to
    the Groq-backed modules (llm_structurer, column_identifier, vision_extractor),
    which keep their own on-disk caching and retry logic. To move to a LOCAL model
    later, you re-point these four wrappers — the pipeline code never changes.

    (This is the deliberately THIN facade: the proven provider code is reused as-is
    rather than rewritten, which is the lowest-risk way to gain provider
    independence. The pipeline imports ONLY this module for LLM work.)

WHERE THE LLM IS USED (and where it is NOT):
    - discover_schema()      → Tier 4: reads a SMALL sample, returns a parsing
                               schema. Called once per statement, only when the
                               cheap deterministic parse fails the validator.
    - read_statement_rows()  → Tier 5 last resort: reads the full statement text
                               only when schema-driven parsing still fails.
    - extract_metadata_llm() → Tier 1 fallback: only when the local regex reader
                               could not find the account identity.
    - read_image()           → image route only (pixels cannot be parsed by code).
    The LLM never sees every row of a statement that the deterministic engine could
    already parse — tokens scale with documents, not rows.

PRIVACY (consistent across every text route):
    Text sent for schema discovery and full-statement reading is ANONYMISED here
    first — account numbers, names, IFSC codes and phone numbers become
    placeholders — because those tasks only need the SHAPE of the data, not real
    identities. Narration keywords (UPI/NEFT/SALARY/…) are preserved, so accuracy
    is unaffected. The two unavoidable exceptions are documented: the vision call
    (a model must see the pixels) and the rare metadata fallback (it exists to read
    the real holder name). Everything else stays on the local machine.

Team: Survey Corps | CIDECODE Hackathon 2026 | CID Karnataka
"""

import logging
from typing import Any, Dict

from extraction.anonymiser import anonymise_text
from extraction.llm_structurer import (
    discover_transaction_schema,
    extract_account_metadata,
    structure_statement,
)
from extraction.vision_extractor import (
    extract_statement_from_image,
    extract_structured_from_scanned_pdf,
    transcribe_image_to_text,
)

logger = logging.getLogger(__name__)


def discover_schema(sample_text: str, file_path: str = "") -> Dict[str, Any]:
    """
    Tier 4: send a SMALL, anonymised, representative sample of transaction lines to
    the LLM and get back a parsing schema (date format, column order, debit/credit
    method, balance position, whether narrations wrap, reference/cheque presence).
    The schema then DRIVES the deterministic engine over every row — the LLM never
    parses the rows itself.
    """
    anonymised, _mapping = anonymise_text(sample_text or "")
    schema = discover_transaction_schema(anonymised, file_path)
    logger.info("llm_interface.discover_schema: source=%s date_format=%r method=%r",
                schema.get("source"), schema.get("date_format"),
                schema.get("debit_credit_method"))
    return schema


def extract_metadata_llm(header_text: str, file_path: str = "") -> Dict[str, Any]:
    """
    Tier 1 fallback: the LLM reads the header and returns account metadata. Used
    ONLY when the local regex reader (account_extractor) could not find the
    identity. This is a documented privacy exception — reading the real holder name
    is the whole point — so it is kept rare and limited to the header.
    """
    return extract_account_metadata(header_text, file_path)


def read_statement_rows(raw_text: str, file_path: str = "") -> Dict[str, Any]:
    """
    Tier 5 (last resort): the LLM reads the FULL statement text and returns
    structured transaction rows. Used only when the cheap parse AND the
    schema-driven reparse both fail the validator — i.e. a layout deterministic
    code genuinely cannot handle.

    The text is anonymised first (identifiers masked, narration words kept). Only
    the transactions are consumed here; account identity comes from Tier 1, so
    masking the holder name does no harm. Nothing is silently truncated: every
    chunk is read, and if a very large statement hits the safety ceiling the unread
    tail is reported so the pipeline can flag it (see llm_structurer).
    """
    anonymised, _mapping = anonymise_text(raw_text or "")
    result = structure_statement(anonymised, file_path)
    return result


def read_image(image_path: str) -> Dict[str, Any]:
    """
    Image route: a vision model reads the picture directly. This is the one
    unavoidable place raw data leaves the machine (a model must see the pixels);
    it is limited to image files and uses the dedicated vision key.
    """
    return extract_statement_from_image(image_path)


def transcribe_image(image_path: str) -> Dict[str, Any]:
    """
    Image route, Stage 1: a vision model TRANSCRIBES the picture to plain text (it
    does not structure transactions). Like read_image this is the one unavoidable
    place raw pixels leave the machine — limited to image files, dedicated vision
    key. The structuring afterwards is deterministic code on the returned text.
    """
    return transcribe_image_to_text(image_path)


def read_scanned_pdf(file_path: str, max_pages: int = None) -> Dict[str, Any]:
    """
    Scanned PDF route: Vision model reads each rendered page (GROQ2 only).
    Eliminates the old dual-LLM path (Tesseract text → GROQ1 structuring chunks)
    that burned the entire daily GROQ1 quota on a 6-page statement.
    Results are cached per page — re-runs cost 0 tokens.
    """
    return extract_structured_from_scanned_pdf(file_path, max_pages=max_pages)
