"""
extraction_pipeline.py — Main orchestrator for the entire extraction phase.

This is the single entry point that the FastAPI backend will call when an
investigator uploads bank statement files. It coordinates all five components:

  COMPONENT 1 — Router (router.py):
      Inspects each file and determines whether it is a digital PDF, scanned
      PDF, Excel file, CSV, DOCX, or image. Returns a routing label.

  COMPONENT 2 — Extractor (extractor_*.py):
      Depending on the routing label, calls the appropriate extractor:
        - pdf_digital → extractor_digital_pdf.py → raw text string
        - pdf_scanned → extractor_ocr.py → raw text string (via Tesseract/Groq Vision)
        - image       → extractor_ocr.py → raw text string
        - excel_csv   → extractor_excel_csv.py → pandas DataFrame
        - docx        → extractor_docx.py → raw text string

  COMPONENT 3 — Column Identifier (column_identifier.py):
      Sends the first 40 lines of the extracted content to Groq (after anonymising
      all PII). Groq returns a JSON column map: which column holds the date,
      narration, debit, credit, and balance.

  COMPONENT 4 — Standardiser (standardiser.py):
      Uses the column map to convert the raw text or DataFrame into the unified
      standard schema: Date | Narration | Debit | Credit | Balance | Account_ID | Bank_Name

  COMPONENT 5 — Validator (validator.py):
      Runs three quality checks (date validity, balance arithmetic, debit/credit
      exclusivity). Splits the result into clean rows and flagged rows.

AFTER ALL FILES PROCESSED:
  - All per-file clean DataFrames are concatenated into one unified DataFrame.
  - Cross-file duplicate transactions are removed.
  - The unified clean DataFrame is ingested into ChromaDB for the RAG chatbot.

ERROR ISOLATION:
  Each file is processed inside its own try/except block. If one file crashes
  (corrupted PDF, password-protected file, unsupported format), it is added to
  the files_failed list and processing continues with the remaining files.
  The pipeline never lets one bad file crash the entire investigation.

Team: Survey Corps | CIDECODE Hackathon 2026 | CID Karnataka
"""

import logging
from pathlib import Path
from typing import List, Dict, Any

import pandas as pd

from config.settings import STANDARD_COLUMNS, REFERENCE_COLUMNS, require_extraction_keys
from extraction.router import route_file
from extraction.extractor_digital_pdf import extract_text_from_digital_pdf
from extraction.extractor_ocr import extract_text_with_ocr_audit
from extraction.extractor_excel_csv import extract_dataframe_from_excel_csv
from extraction.extractor_docx import extract_text_from_docx, extract_text_from_txt
from extraction.account_extractor import (
    reconcile_account_details,
    extract_account_details_from_text,
)
from extraction.identifier_vault import IdentifierVault
from extraction.column_identifier import identify_column_structure
# ALL LLM access goes through the single provider-independent interface (a future
# local-model swap touches only that module). The pipeline never imports a provider.
from extraction.llm_interface import (
    discover_schema,
    read_statement_rows,
    extract_metadata_llm,
    read_image,
    read_scanned_pdf,
    transcribe_image,
)
from extraction.image_grouping import group_images
from extraction.standardiser import (
    standardise_dataframe_direct,
    standardise_transaction_records,
    standardise_llm_transactions,
    standardise_digital_pdf_transactions,
    count_transaction_like_lines,
    build_schema_sample,
)
from extraction.validator import validate_and_clean, mark_duplicates, grade_parse, find_first_balance_break
from extraction.storage import persist_extraction_run
from extraction.sqlite_exporter import export_to_sqlite

# Set up a logger for this module.
logger = logging.getLogger(__name__)


def run_extraction_pipeline(
    files: List[Dict[str, str]],
    session_id: str,
    ingest_to_chromadb: bool = False,
    max_ocr_pages: int = None,
    persist: bool = True,
) -> Dict[str, Any]:
    """
    Main orchestrator for the extraction phase.

    Processes every uploaded file through the five-component pipeline:
        Component 1 — Route file to correct extractor
        Component 2 — Extract raw text or DataFrame (OCR records which engine ran)
        Component 3 — Identify column structure (Groq, cached, source recorded)
        Component 4 — Standardise all rows into unified schema
        Component 5 — Validate and clean, separate flagged rows

    After all files are processed:
        - Combines all per-file DataFrames into one unified DataFrame
        - Removes cross-file duplicates
        - PERSISTS clean table + flagged rows + metadata to disk (Problem 3)
        - Optionally ingests into ChromaDB (RAG phase — OFF by default here)

    Parameters:
        files (list[dict]): List of file metadata dicts, each containing:
            {"file_path": str, "account_id": str, "bank_name": str}
        session_id (str): Unique identifier for this investigation session.
        ingest_to_chromadb (bool): If True, embed the clean rows into ChromaDB.
            Default False: ChromaDB belongs to the RAG phase and downloads a model
            / does heavy work, so extraction keeps it OFF to stay bounded and not
            overheat the laptop. Turn it on only when the RAG phase needs it.
        max_ocr_pages (int): Safety cap on how many pages of a scanned PDF to OCR
            (None = all). Tests pass a small number to stay bounded.
        persist (bool): If True (default), write the run to outputs/extractions/.

    Returns:
        dict: clean_df, flagged_df, counts, files_processed, files_failed,
              session_id, per_file (visibility records), and storage_paths.
    """
    # Fail fast with a readable message if GROQ1/GROQ2 are missing from .env.
    require_extraction_keys()

    logger.info(
        "extraction_pipeline.run_extraction_pipeline: "
        "Starting extraction for session '%s' with %d file(s).",
        session_id,
        len(files),
    )

    # NOTE: The system reads account identity ONLY from each statement's own
    # content. The reference files in synthetic_dataset_full_mentoring/ (accounts
    # _master.csv, transactions_master.csv, ground_truth.json, case_briefs.txt) are
    # for the investigator's MANUAL verification and are never read by this code.

    # Accumulate results across all files
    all_clean_dfs: List[pd.DataFrame] = []
    all_flagged_dfs: List[pd.DataFrame] = []
    files_failed: List[str] = []
    files_processed: int = 0
    # One visibility record per file: route, OCR engine + confidence, column map
    # + where it came from (groq/cache/fallback), and row counts. Saved to disk.
    per_file_records: List[Dict[str, Any]] = []
    # Per-statement bundles (real account details + that statement's clean rows),
    # used to write one structured JSON per statement at the end (Problems 4 & 7).
    statements: List[Dict[str, Any]] = []

    # ── Split images out for the dedicated image pipeline ─────────────────────
    # Image uploads need a CROSS-FILE step (grouping continuation pages of one
    # statement), so they are processed together as a batch AFTER the normal
    # per-file loop. Every other format (PDF / Excel / CSV / DOCX) is unchanged.
    # Partition is by extension — identical to how the router labels images — so no
    # PDF is ever re-routed here.
    _IMAGE_EXTS = {".png", ".jpg", ".jpeg"}
    image_files = [f for f in files
                   if Path(f.get("file_path", "")).suffix.lower() in _IMAGE_EXTS]
    other_files = [f for f in files
                   if Path(f.get("file_path", "")).suffix.lower() not in _IMAGE_EXTS]

    for file_index, file_info in enumerate(other_files, start=1):
        file_path = file_info.get("file_path", "")
        account_id = file_info.get("account_id", f"ACC{file_index:03d}")
        bank_name = file_info.get("bank_name", "Unknown Bank")

        logger.info(
            "extraction_pipeline.run_extraction_pipeline: "
            "Processing file %d of %d: '%s'",
            file_index,
            len(other_files),
            Path(file_path).name,
        )

        try:
            clean_df, flagged_df, file_record = _process_single_file(
                file_path=file_path,
                account_id=account_id,
                bank_name=bank_name,
                max_ocr_pages=max_ocr_pages,
            )

            if clean_df is not None and not clean_df.empty:
                all_clean_dfs.append(clean_df)

            if flagged_df is not None and not flagged_df.empty:
                all_flagged_dfs.append(flagged_df)

            files_processed += 1
            per_file_records.append(file_record)
            # Bundle this statement's real identity with its own clean rows so we
            # can write one structured JSON per statement (Problems 4 & 7).
            statements.append({
                "file": file_record["file"],
                "account_details": file_record.get("account_details", {}),
                "clean_df": clean_df if clean_df is not None else pd.DataFrame(),
            })

            logger.info(
                "extraction_pipeline.run_extraction_pipeline: "
                "File %d complete: %d clean rows, %d flagged rows.",
                file_index,
                len(clean_df) if clean_df is not None else 0,
                len(flagged_df) if flagged_df is not None else 0,
            )

        except Exception as file_error:
            # If this file fails completely, record it and move on.
            # One bad file must never stop the entire investigation.
            logger.error(
                "extraction_pipeline.run_extraction_pipeline: "
                "File %d FAILED completely: '%s'. Error: %s. Continuing with remaining files.",
                file_index,
                file_path,
                file_error,
            )
            files_failed.append(file_path)
            per_file_records.append({
                "file": Path(file_path).name,
                "account_id": account_id,
                "bank_name": bank_name,
                "status": "FAILED",
                "error": str(file_error),
            })
            continue

    # ── IMAGE BATCH: transcribe → group → parse (one statement per group) ─────
    # Images are handled together because grouping continuation pages of the same
    # statement is a cross-file decision. Each resulting group becomes ONE statement
    # bundle, accumulated exactly like a normal file's result.
    if image_files:
        try:
            image_results = _process_image_batch(image_files, max_ocr_pages)
        except Exception as batch_error:
            logger.error(
                "extraction_pipeline.run_extraction_pipeline: image batch FAILED: %s. "
                "Continuing with the rest of the run.", batch_error,
            )
            image_results = []
            for f in image_files:
                files_failed.append(f.get("file_path", ""))
                per_file_records.append({
                    "file": Path(f.get("file_path", "")).name,
                    "account_id": f.get("account_id", ""),
                    "bank_name": f.get("bank_name", ""),
                    "status": "FAILED",
                    "error": str(batch_error),
                })

        for clean_df, flagged_df, file_record in image_results:
            if clean_df is not None and not clean_df.empty:
                all_clean_dfs.append(clean_df)
            if flagged_df is not None and not flagged_df.empty:
                all_flagged_dfs.append(flagged_df)
            files_processed += 1
            per_file_records.append(file_record)
            statements.append({
                "file": file_record["file"],
                "account_details": file_record.get("account_details", {}),
                "clean_df": clean_df if clean_df is not None else pd.DataFrame(),
            })
            logger.info(
                "extraction_pipeline.run_extraction_pipeline: "
                "image group '%s' complete: %d clean, %d flagged.",
                file_record.get("file"),
                len(clean_df) if clean_df is not None else 0,
                len(flagged_df) if flagged_df is not None else 0,
            )

    # ── Combine all per-file DataFrames into one unified DataFrame ──────────
    logger.info(
        "extraction_pipeline.run_extraction_pipeline: "
        "All files processed. Combining %d clean DataFrames...",
        len(all_clean_dfs),
    )

    if all_clean_dfs:
        unified_clean_df = pd.concat(all_clean_dfs, ignore_index=True)
        # Re-tag duplicates across the WHOLE set (within- and cross-file) without
        # deleting anything — every row is kept and duplicates carry duplicate_of
        # (Problem 6). This assigns globally-consistent txn_ids too.
        unified_clean_df = mark_duplicates(unified_clean_df)
        dup_total = unified_clean_df["duplicate_of"].notna().sum()
        logger.info(
            "extraction_pipeline.run_extraction_pipeline: "
            "Unified clean DataFrame: %d rows (%d tagged as duplicates, none dropped).",
            len(unified_clean_df), dup_total,
        )
    else:
        unified_clean_df = pd.DataFrame(
            columns=STANDARD_COLUMNS + REFERENCE_COLUMNS + ["is_reversed", "txn_id", "duplicate_of"])
        logger.warning(
            "extraction_pipeline.run_extraction_pipeline: "
            "No clean rows produced from any file."
        )

    if all_flagged_dfs:
        unified_flagged_df = pd.concat(all_flagged_dfs, ignore_index=True)
    else:
        unified_flagged_df = pd.DataFrame(columns=STANDARD_COLUMNS + REFERENCE_COLUMNS + ["flag_reason"])

    # Present Date as a calendar date only (no false "00:00:00") and guarantee the
    # separate Time column exists. Validation already ran on the datetime values.
    unified_clean_df = _finalise_date_time(unified_clean_df)
    unified_flagged_df = _finalise_date_time(unified_flagged_df)

    # ── Ingest clean transactions into ChromaDB (OFF by default) ──────────────
    # ChromaDB is the RAG chatbot's store, not part of extraction. It downloads an
    # embedding model and does heavy work, so we keep it OFF here to stay bounded.
    # It is only imported and run when a caller explicitly opts in.
    if ingest_to_chromadb and not unified_clean_df.empty:
        logger.info(
            "extraction_pipeline.run_extraction_pipeline: "
            "Ingesting %d transactions into ChromaDB for session '%s'...",
            len(unified_clean_df),
            session_id,
        )
        try:
            # Imported lazily so a normal extraction run never loads the heavy
            # embedding stack at all.
            from extraction.chromadb_ingestor import ingest_transactions_to_chromadb
            ingest_transactions_to_chromadb(unified_clean_df, session_id)
            logger.info(
                "extraction_pipeline.run_extraction_pipeline: "
                "ChromaDB ingestion complete: %d vectors stored.",
                len(unified_clean_df),
            )
        except Exception as chroma_error:
            # ChromaDB failure should not crash the pipeline.
            logger.error(
                "extraction_pipeline.run_extraction_pipeline: "
                "ChromaDB ingestion failed: %s. "
                "The clean DataFrame is still available for analysis.",
                chroma_error,
            )

    # ── Build the result summary ──────────────────────────────────────────────
    total_rows = len(unified_clean_df) + len(unified_flagged_df)
    summary = {
        "total_rows": total_rows,
        "clean_rows": len(unified_clean_df),
        "flagged_rows": len(unified_flagged_df),
        "files_processed": files_processed,
        "files_failed": files_failed,
        # Trust-at-a-glance: how many LLM calls the whole run cost (should scale with
        # documents, not rows), and the per-file balance-reconciliation rate and the
        # tier each file resolved at — the proof of correctness for the team/jury.
        "total_llm_calls": sum(r.get("llm_calls", 0) for r in per_file_records),
        "reconciliation_by_file": {
            r.get("file"): r.get("reconciliation_rate")
            for r in per_file_records if "reconciliation_rate" in r
        },
        "tier_by_file": {
            r.get("file"): r.get("tier")
            for r in per_file_records if "tier" in r
        },
    }

    # ── Persist to disk so the team can OPEN the output (Problems 3, 4, 7) ─────
    storage_paths = {}
    if persist:
        storage_paths = persist_extraction_run(
            session_id=session_id,
            clean_df=unified_clean_df,
            flagged_df=unified_flagged_df,
            per_file_records=per_file_records,
            summary=summary,
            statements=statements,
        )

    # ── SQLite export — ready for the analysis phase ──────────────────────────
    sqlite_path = ""
    if persist and not unified_clean_df.empty:
        try:
            from config.settings import OUTPUT_DIR
            sqlite_path = export_to_sqlite(
                clean_df=unified_clean_df,
                session_id=session_id,
                output_dir=OUTPUT_DIR,
                per_file_records=per_file_records,
            )
            storage_paths["sqlite_db"] = sqlite_path
            logger.info(
                "extraction_pipeline.run_extraction_pipeline: "
                "SQLite database written → %s", sqlite_path,
            )
        except Exception as sqlite_err:
            logger.error(
                "extraction_pipeline.run_extraction_pipeline: "
                "SQLite export failed (non-fatal): %s", sqlite_err,
            )

    result = {
        "clean_df": unified_clean_df,
        "flagged_df": unified_flagged_df,
        "total_rows": total_rows,
        "clean_rows": len(unified_clean_df),
        "flagged_rows": len(unified_flagged_df),
        "files_processed": files_processed,
        "files_failed": files_failed,
        "session_id": session_id,
        "per_file": per_file_records,
        "storage_paths": storage_paths,
        "sqlite_path": sqlite_path,
    }

    logger.info(
        "extraction_pipeline.run_extraction_pipeline: "
        "Session '%s' complete. "
        "Files: %d processed, %d failed. "
        "Rows: %d total, %d clean, %d flagged.",
        session_id,
        files_processed,
        len(files_failed),
        total_rows,
        len(unified_clean_df),
        len(unified_flagged_df),
    )

    return result


def _details_from_meta(meta: Dict[str, str], account_id: str, bank_name: str) -> Dict[str, Any]:
    """Maps the LLM's account-metadata JSON to our reconciled account_details."""
    content_details = {
        "account_holder": meta.get("account_holder_name", ""),
        "account_number": meta.get("account_number", ""),
        "ifsc_code": meta.get("ifsc_code", ""),
        "bank_name": meta.get("bank_name", ""),
        "branch": meta.get("branch_name", ""),
        "account_type": "",
        "statement_period": meta.get("statement_period", ""),
        "opening_balance": meta.get("opening_balance", ""),
        "closing_balance": meta.get("closing_balance", ""),
    }
    details = reconcile_account_details(content_details, account_id, bank_name)
    details["branch_address"] = meta.get("branch_address", "")
    details["currency"] = meta.get("currency", "")
    return details


def _finalise_date_time(df: pd.DataFrame) -> pd.DataFrame:
    """
    Formats the Date column as a calendar date only (DD/MM/YYYY) so the output never
    shows a misleading "00:00:00", and guarantees a separate Time column exists.
    """
    if df is None:
        return df
    df = df.copy()
    if "Time" not in df.columns:
        df["Time"] = ""
    if "Date" in df.columns and len(df):
        dt = pd.to_datetime(df["Date"], errors="coerce", dayfirst=True)
        df["Date"] = dt.dt.strftime("%d/%m/%Y").fillna("")
    return df


def _extract_text_transactions(
    raw_text: str,
    file_record: Dict[str, Any],
    account_id: str,
    bank_name: str,
    details: Dict[str, Any],
) -> tuple:
    """
    The tiered escalation ladder for ANY text source (digital PDF, scanned-PDF OCR
    text, DOCX). One unified path; the validator (grade_parse) decides how far to
    escalate, so the cheapest correct method is always used:

        Tier 2  cheap deterministic parse (default schema)        — no LLM
        Tier 3  grade_parse — the balance-reconciliation referee   — no LLM
        Tier 4  LLM schema discovery on a small SAMPLE → reparse   — 1 LLM call
        Tier 5  LLM reads the FULL statement (last resort)         — 1 LLM call

    At every step we keep the parse with the BEST reconciliation rate, so escalating
    can only improve the result, never worsen it. Tokens scale with documents, not
    rows: a clean statement costs zero transaction LLM calls.

    Returns (standard_df, grade_dict).
    """
    bank = details.get("bank_name") or bank_name
    opening = details.get("opening_balance", "") or ""
    expected = count_transaction_like_lines(raw_text)
    file_record["expected_txn_lines"] = expected
    file_record.setdefault("llm_calls", 0)

    # ── Tier 2: cheap deterministic parse with safe default assumptions ───────
    df = standardise_digital_pdf_transactions(raw_text, account_id, bank, opening, {})
    grade = grade_parse(df, expected_rows=expected)
    file_record["tier"] = "cheap_parse"
    file_record["column_map"] = {"engine": "deterministic_default"}
    file_record["column_map_source"] = "deterministic"

    # ── Tier 4: LLM describes the layout from a SAMPLE; deterministic reparse ──
    if grade["verdict"] != "PASS":
        sample = build_schema_sample(raw_text)
        schema = discover_schema(sample, file_record["file"])
        file_record["llm_calls"] += 1
        if schema.get("source") in ("groq", "cache"):
            df_s = standardise_digital_pdf_transactions(raw_text, account_id, bank, opening, schema)
            grade_s = grade_parse(df_s, expected_rows=expected)
            # Adopt only if STRICTLY better by score (rate × completeness), so a sparser
            # schema parse can never displace a fuller one and the cheap deterministic
            # parse stays the source of truth on a tie.
            if grade_s["score"] > grade["score"]:
                df, grade = df_s, grade_s
                file_record["tier"] = "schema_reparse"
                file_record["column_map"] = {
                    "engine": "llm_schema",
                    "schema": {k: v for k, v in schema.items() if k != "source"},
                }
                file_record["column_map_source"] = schema.get("source")

    # ── Tier 5: last resort — LLM reads the whole statement; keep the better ──
    if grade["verdict"] != "PASS":
        structured = read_statement_rows(raw_text, file_record["file"])
        file_record["llm_calls"] += 1
        llm_df = standardise_llm_transactions(
            structured.get("transactions", []), account_id, bank, opening)
        grade_l = grade_parse(llm_df, expected_rows=expected)
        # Replace the deterministic parse ONLY if the LLM read is STRICTLY better by
        # score (rate × completeness). This is the core regression fix: a degenerate
        # 1-row parse used to score a meaningless 1.0 on bare rate and made the pipeline
        # REJECT the LLM's correct, complete read (0.99 ≥ 1.0 is false). Comparing by
        # score keeps the deterministic result authoritative unless the LLM clearly
        # improves it — and stops a sparse LLM result from ever winning either.
        if len(llm_df) and grade_l["score"] > grade["score"]:
            df, grade = llm_df, grade_l
            file_record["tier"] = "llm_full_read"
            file_record["column_map"] = {"engine": "llm_structurer_fallback"}
            file_record["column_map_source"] = structured.get("source")
            file_record["llm_txn_count"] = len(structured.get("transactions", []))
            # Surface any tail the LLM could not read (never silently dropped).
            if structured.get("unprocessed_tail_lines"):
                file_record["unprocessed_tail_lines"] = structured["unprocessed_tail_lines"]

    file_record["reconciliation_rate"] = round(grade["reconciliation_rate"], 3)
    file_record["ordering"] = grade["ordering"]
    file_record["has_balance_column"] = grade["has_balance_column"]
    return df, grade


def _process_single_file(
    file_path: str,
    account_id: str,
    bank_name: str,
    max_ocr_pages: int = None,
) -> tuple:
    """
    Processes a single bank statement file end to end.

    Per source type:
      • IMAGE (.jpg/.png): read directly with the vision LLM (NO Tesseract) — it
        returns both the account identity and the transactions from the picture.
      • DIGITAL PDF / DOCX: pull the text, read the account identity from the header
        with regex, then use Groq to identify columns and standardise the rows.
      • SCANNED PDF: Tesseract→Groq Vision OCR, then the same text path.
      • EXCEL / CSV: read the table directly, Groq identifies the columns.

    Account identity (holder, number, IFSC, bank, branch...) is read ONLY from the
    statement's own content — never from the filename or any reference file. Each
    transaction row is then stamped with the REAL account number and IFSC code.
    Where a source simply does not print them (e.g. a plain Excel/CSV table), those
    fields are "UNKNOWN" — we never invent them.

    Parameters:
        file_path (str): Absolute path to the uploaded bank statement file.
        account_id (str): A per-file reference label from the caller (used only as a
            traceback handle / fallback; it is NOT used as the account identity).
        bank_name (str): Bank name hint the investigator supplied for the upload.
        max_ocr_pages (int): Safety cap on scanned-PDF pages to OCR (None = all).

    Returns:
        tuple: (clean_df, flagged_df, file_record). file_record carries the
               visibility info AND the final real account_details for storage.
    """
    file_record: Dict[str, Any] = {
        "file": Path(file_path).name,
        "account_ref": account_id,
        "bank_name": bank_name,
        "status": "ok",
        "ocr": "n/a",
    }

    # ── Component 1: Route the file ───────────────────────────────────────
    route = route_file(file_path)
    file_record["route"] = route
    logger.info("extraction_pipeline._process_single_file: Routed to: '%s'", route)

    raw_text: str = ""
    raw_df: pd.DataFrame = None
    content_details: Dict[str, str] = {}       # identity read from the document itself
    standard_df: pd.DataFrame = None
    grade: Dict[str, Any] = None               # the validator's verdict for this file
    file_record["llm_calls"] = 0

    # ── IMAGE PATH and SCANNED PDF PATH: Vision model reads the pixels ──────────
    # Scanned PDFs now follow the same Vision-JSON path as image files instead of
    # the old Tesseract→raw-text→GROQ1-structuring path. Benefits:
    #   • Uses only GROQ2 (separate daily quota from GROQ1)
    #   • Each page result is cached — re-runs on the same PDF cost 0 tokens
    #   • Eliminates the multi-chunk GROQ1 calls that exhausted 100k TPD on 6 pages
    if route in ("image", "pdf_scanned"):
        if route == "image":
            vision = read_image(file_path)
            file_record["tier"] = "vision"
            file_record["llm_calls"] = 1 if vision.get("source") == "groq" else 0
        else:  # pdf_scanned
            vision = read_scanned_pdf(file_path, max_pages=max_ocr_pages)
            file_record["tier"] = "vision_scanned"
            file_record["llm_calls"] = vision.get("llm_calls", 0)

        content_details = vision.get("account_details", {})
        file_record["ocr"] = {
            "engine": vision.get("engine"),
            "source": vision.get("source"),
            "raw_chars": vision.get("raw_chars"),
            "pages_processed": vision.get("pages_processed"),
            "total_pages": vision.get("total_pages"),
        }
        file_record["column_map_source"] = "vision"
        details = reconcile_account_details(content_details, account_id, bank_name)
        standard_df = standardise_transaction_records(
            vision.get("transactions", []), account_id, details["bank_name"] or bank_name,
        )
        grade = grade_parse(standard_df, expected_rows=len(vision.get("transactions", [])) or None)
        file_record["reconciliation_rate"] = round(grade["reconciliation_rate"], 3)
        file_record["has_balance_column"] = grade["has_balance_column"]
        file_record["ordering"] = grade["ordering"]
        file_record["raw_text_chars"] = vision.get("raw_chars", 0)

    else:
        # ── Tier 0: extract raw content for the text/table sources ───────────
        if route == "pdf_digital":
            raw_text = extract_text_from_digital_pdf(file_path)
        elif route == "docx":
            raw_text = extract_text_from_docx(file_path)
        elif route == "text":
            raw_text = extract_text_from_txt(file_path)
        elif route == "excel_csv":
            raw_df = extract_dataframe_from_excel_csv(file_path, account_id, bank_name)
        else:
            raise ValueError(f"Unknown route '{route}' returned by router.")

        file_record["raw_text_chars"] = len(raw_text) if raw_text else 0

        if raw_df is not None and not raw_df.empty:
            # ── EXCEL / CSV: a structured table. ─────────────────────────────
            # Identity: read from the sheet's own metadata block (Account Number,
            # IFSC, Holder, … printed above the table) when present; otherwise it
            # stays blank — never invented. Both cases are handled.
            content_details = raw_df.attrs.get("statement_metadata", {}) or {}
            details = reconcile_account_details(content_details, account_id, bank_name)
            file_record["metadata_source"] = (
                "excel_metadata_block" if content_details else "none")

            # Columns: map DETERMINISTICALLY from the header names first (no LLM,
            # no token-quota dependency, and it never positionally mis-assigns a
            # "Ref" column to Debit). Fall back to the Groq column mapper ONLY when
            # the headers are not recognisable.
            inferred = raw_df.attrs.get("inferred_column_map", {}) or {}
            core_map = {k: v for k, v in inferred.items()
                        if k in ("date", "narration", "debit", "credit", "balance")}
            is_complete = ("date" in core_map and "balance" in core_map
                           and ("debit" in core_map or "credit" in core_map))

            if is_complete:
                column_map = core_map
                file_record["llm_calls"] = 0
                file_record["column_map_source"] = "deterministic_headers"
                file_record["tier"] = "excel_deterministic"
            else:
                helper_cols = [c for c in ("Account_ID", "Bank_Name") if c in raw_df.columns]
                column_id_text = raw_df.drop(columns=helper_cols).head(40).to_csv(index=False)
                vault = IdentifierVault(details)
                col_result = identify_column_structure(vault.redact(column_id_text), file_path)
                file_record["llm_calls"] = 1 if col_result.get("source") == "groq" else 0
                column_map = vault.restore(col_result["column_map"])
                file_record["column_map_source"] = col_result["source"]
                file_record["tier"] = "excel_columnmap"

            file_record["column_map"] = column_map
            standard_df = standardise_dataframe_direct(
                raw_df, column_map, account_id, details["bank_name"] or bank_name)
            # Grade the table parse too (catches a mis-mapped column).
            grade = grade_parse(standard_df, expected_rows=len(raw_df))
            file_record["reconciliation_rate"] = round(grade["reconciliation_rate"], 3)
            file_record["has_balance_column"] = grade["has_balance_column"]
            file_record["ordering"] = grade["ordering"]

        else:
            # ── TEXT (digital PDF / scanned PDF / DOCX): ONE unified path ──────
            # Tier 1 — metadata is read CODE-FIRST (local regex, fully private). The
            # LLM is used only as a fallback when the regex finds no identity at all.
            content_details = extract_account_details_from_text(raw_text)
            if not (content_details.get("account_holder")
                    or content_details.get("account_number")
                    or content_details.get("ifsc_code")):
                header_text = "\n".join(raw_text.splitlines()[:40])
                meta = extract_metadata_llm(header_text, file_path)
                file_record["llm_calls"] = file_record.get("llm_calls", 0) + 1
                details = _details_from_meta(meta, account_id, bank_name)
                file_record["metadata_source"] = "llm_fallback"
            else:
                details = reconcile_account_details(content_details, account_id, bank_name)
                file_record["metadata_source"] = "regex"

            # Tiers 2–5 — cheap parse, validate, escalate only on failure.
            standard_df, grade = _extract_text_transactions(
                raw_text, file_record, account_id, bank_name, details)

    # ── Chronological order: if the statement is newest-first, flip it to
    # oldest-first (preserving within-day order) so the validator's running-balance
    # check and the analysis phase always see time moving forward.
    if (grade is not None and grade.get("ordering") == "newest_first"
            and standard_df is not None and not standard_df.empty):
        standard_df = standard_df.iloc[::-1].reset_index(drop=True)
        logger.info(
            "extraction_pipeline._process_single_file: '%s' was newest-first — "
            "reordered to chronological (oldest-first).", Path(file_path).name,
        )

    # Derive the closing balance from the last transaction when the header did not
    # print one (works for every bank — the last running balance IS the closing).
    if standard_df is not None and not standard_df.empty and "Balance" in standard_df.columns:
        bals = standard_df["Balance"].dropna()
        if len(bals) and not details.get("closing_balance"):
            details["closing_balance"] = f"{float(bals.iloc[-1]):.2f}"

    # ── Stamp every row with the REAL account number + IFSC (Problem: identity) ─
    # The account column now holds the actual bank account number read from the
    # statement, NOT the filename-derived reference. If the document did not show
    # the number / IFSC (common for plain Excel/CSV), we mark it UNKNOWN — never
    # guessed, never taken from a reference file.
    real_account = details.get("account_number") or ""
    if not real_account or real_account.upper() == "UNREADABLE":
        # Keep it unique-per-file so two unknown statements never merge together.
        real_account = f"UNKNOWN-{Path(file_path).stem}"
    real_ifsc = details.get("ifsc_code") or "UNKNOWN"

    standard_df["Account_ID"] = real_account   # the account column = real number
    standard_df["IFSC_Code"] = real_ifsc       # IFSC column on every transaction

    # ── Component 5: Validate and clean (shared by all routes) ────────────
    clean_df, flagged_df = validate_and_clean(standard_df)

    # The final, REAL account identity for this statement (Problems 2 & 4).
    details["account_number"] = real_account
    file_record["account_details"] = details
    file_record["rows_standardised"] = len(standard_df)
    file_record["rows_clean"] = len(clean_df)
    file_record["rows_flagged"] = len(flagged_df)

    # ── Validation (requirements 8 & 9) ───────────────────────────────────────
    rows_into_output = len(clean_df) + len(flagged_df)
    llm_rows = file_record.get("llm_txn_count", len(standard_df))
    text_for_check = (raw_text or "")
    file_record["validation"] = {
        "llm_transactions": llm_rows,
        "rows_in_output": rows_into_output,
        "all_rows_accounted_for": rows_into_output >= len(standard_df),
        "rows_dropped_unparseable_date": max(0, llm_rows - len(standard_df)),
        "account_number_in_text": bool(details.get("account_number", "")
                                       and details["account_number"] in text_for_check),
        "ifsc_in_text": bool(details.get("ifsc_code", "")
                             and details["ifsc_code"] in text_for_check),
    }

    # ── Transaction count audit (Priority 4) ─────────────────────────────────
    expected = file_record.get("expected_txn_lines", 0) or 0
    extracted = len(standard_df)
    completeness = round(extracted / expected, 3) if expected > 0 else 1.0
    count_audit: Dict[str, Any] = {
        "expected_transaction_count": expected,
        "extracted_transaction_count": extracted,
        "clean_transaction_count": len(clean_df),
        "flagged_transaction_count": len(flagged_df),
        "completeness_ratio": completeness,
    }
    if completeness < 0.85 and expected > 0:
        logger.warning(
            "extraction_pipeline._process_single_file: LOW COMPLETENESS '%s' — "
            "extracted %d of %d expected transaction-like lines (ratio %.2f).",
            Path(file_path).name, extracted, expected, completeness,
        )

    # ── First balance break (Priority 5) ──────────────────────────────────────
    # Surface the exact row where the balance chain first breaks, so the root
    # cause is immediately visible rather than buried in hundreds of flagged rows.
    balance_mismatch_rows = flagged_df[
        flagged_df.get("flag_reason", pd.Series(dtype=str)) == "balance_mismatch"
    ] if not flagged_df.empty and "flag_reason" in flagged_df.columns else pd.DataFrame()

    first_break = None
    if not balance_mismatch_rows.empty:
        # Run find_first_balance_break on the full standard_df (pre-split) so
        # the chain walk sees every row in chronological order.
        first_break = find_first_balance_break(standard_df)

    count_audit["first_balance_break"] = first_break
    file_record["transaction_count_audit"] = count_audit

    logger.info(
        "extraction_pipeline._process_single_file: '%s' → %d clean, %d flagged "
        "(holder=%r account_number=%r).",
        Path(file_path).name, len(clean_df), len(flagged_df),
        details.get("account_holder"), details.get("account_number"),
    )

    return clean_df, flagged_df, file_record


def _process_image_batch(
    image_files: List[Dict[str, str]],
    max_ocr_pages: int = None,
) -> List[tuple]:
    """
    The redesigned IMAGE pipeline (image route only — PDF/Excel/CSV are untouched).

        Stage 1  Vision → TEXT      transcribe each image (vision reads pixels only;
                                    it does NOT structure transactions or emit JSON).
        Stage 2  GROUP              decide which images are pages of ONE statement vs
                                    separate statements (deterministic; see
                                    image_grouping.py). Biased toward keeping apart.
        Stage 3+4  PARSE            each group's combined text runs through the SAME
                                    proven text ladder the digital-PDF path uses
                                    (cheap parse → validate → LLM schema discovery on
                                    a sample → deterministic reparse → full read), so
                                    code does the structuring and tokens scale with
                                    documents, not rows.

    Returns a list of (clean_df, flagged_df, file_record) — ONE per statement group.
    """
    # ── Stage 1: transcribe every image to text (cached per image) ────────────
    items: List[Dict[str, Any]] = []
    for idx, fi in enumerate(image_files):
        path = fi.get("file_path", "")
        try:
            res = transcribe_image(path)
        except Exception as e:
            logger.error("extraction_pipeline._process_image_batch: transcription failed "
                         "for '%s': %s", Path(path).name, e)
            res = {"text": "", "source": "failed"}
        items.append({
            "name": Path(path).name,
            "path": path,
            "account_id": fi.get("account_id", f"ACC{idx + 1:03d}"),
            "bank_name": fi.get("bank_name", "Unknown Bank"),
            "text": res.get("text", ""),
            "source": res.get("source"),
        })

    # ── Stage 2: group images into statements ─────────────────────────────────
    groups = group_images(items)

    # ── Stage 3+4: parse each group into one statement bundle ─────────────────
    results: List[tuple] = []
    for group_index, group in enumerate(groups, start=1):
        members = [items[i] for i in group["indices"]]
        try:
            results.append(_process_image_group(members, group, group_index))
        except Exception as e:
            logger.error("extraction_pipeline._process_image_batch: group %d (%s) failed: %s",
                         group_index, group.get("names"), e)
            # Surface the failure as an empty bundle so nothing is silently lost.
            fr = {
                "file": f"image_group_{group_index} ({', '.join(group.get('names', []))})",
                "route": "image", "status": "FAILED", "error": str(e),
                "image_group": {"members": group.get("names"), "reason": group.get("reason"),
                                "confidence": group.get("confidence")},
            }
            results.append((pd.DataFrame(), pd.DataFrame(), fr))
    return results


def _process_image_group(
    members: List[Dict[str, Any]],
    group: Dict[str, Any],
    group_index: int,
) -> tuple:
    """
    Turns ONE group of images (the pages of a single statement) into a clean/flagged
    pair. The combined transcription is treated exactly like any other text source:
    identity is read code-first (regex), transactions go through the tiered ladder,
    and — as an accuracy floor — if the ladder cannot reconcile the parse we fall
    back to the original Vision→JSON reader per image (so we never do worse than the
    old pipeline).
    """
    combined_text = "\n".join(m["text"] for m in members if m.get("text"))
    names = [m["name"] for m in members]
    account_id = members[0]["account_id"]
    bank_name = members[0]["bank_name"]

    file_record: Dict[str, Any] = {
        "file": f"image_group_{group_index} ({', '.join(names)})",
        "account_ref": account_id,
        "bank_name": bank_name,
        "status": "ok",
        "route": "image",
        "ocr": "n/a",
        "tier": "image_transcribe_parse",
        "column_map_source": "image_text_ladder",
        # Stage-1 vision calls that actually spent tokens (cache hits are free).
        "llm_calls": sum(1 for m in members if m.get("source") == "groq"),
        "image_group": {
            "members": names,
            "reason": group.get("reason"),
            "confidence": group.get("confidence"),
        },
    }

    # ── Tier 1 — identity, code-first (regex, fully local); LLM only if empty ──
    content_details = extract_account_details_from_text(combined_text)
    if not (content_details.get("account_holder")
            or content_details.get("account_number")
            or content_details.get("ifsc_code")):
        header_text = "\n".join(combined_text.splitlines()[:40])
        meta = extract_metadata_llm(header_text, file_record["file"])
        file_record["llm_calls"] = file_record.get("llm_calls", 0) + 1
        details = _details_from_meta(meta, account_id, bank_name)
        file_record["metadata_source"] = "llm_fallback"
    else:
        details = reconcile_account_details(content_details, account_id, bank_name)
        file_record["metadata_source"] = "regex"

    # ── Tiers 2–5 — the shared text ladder (cheap parse → validate → escalate) ─
    standard_df, grade = _extract_text_transactions(
        combined_text, file_record, account_id, bank_name, details)

    # ── Accuracy floor — fall back to the original Vision→JSON reader ──────────
    # Only if the text ladder could NOT reconcile (verdict FAIL). We read each image
    # as structured JSON (the old path), merge, and keep it ONLY if it reconciles at
    # least as well — so this can never make the result worse.
    if grade is None or grade.get("verdict") != "PASS":
        fb_rows: List[Dict[str, Any]] = []
        fb_calls = 0
        for m in members:
            try:
                vision = read_image(m["path"])
            except Exception as e:
                logger.warning("extraction_pipeline._process_image_group: JSON fallback "
                               "failed for '%s': %s", m["name"], e)
                continue
            if vision.get("source") == "groq":
                fb_calls += 1
            if not (details.get("account_number") or details.get("ifsc_code")):
                vd = vision.get("account_details") or {}
                if vd:
                    details = reconcile_account_details(vd, account_id, bank_name)
            fb_rows.extend(vision.get("transactions", []))

        if fb_rows:
            fb_df = standardise_transaction_records(
                fb_rows, account_id, details.get("bank_name") or bank_name)
            fb_grade = grade_parse(fb_df, expected_rows=len(fb_rows) or None)
            # Compare on score (rate × completeness), not bare rate, so a sparse vision
            # read can't out-score a fuller text-ladder parse; ≥ keeps the vision floor
            # as the tie-breaker (its original accuracy-floor intent).
            better = (grade is None
                      or fb_grade["score"] >= grade["score"])
            if len(fb_df) and better:
                standard_df, grade = fb_df, fb_grade
                file_record["tier"] = "image_vision_json_fallback"
                file_record["column_map_source"] = "vision_json"
                file_record["llm_calls"] = file_record.get("llm_calls", 0) + fb_calls
                file_record["reconciliation_rate"] = round(fb_grade["reconciliation_rate"], 3)
                file_record["has_balance_column"] = fb_grade["has_balance_column"]
                file_record["ordering"] = fb_grade["ordering"]

    if standard_df is None:
        from extraction.standardiser import _create_empty_standard_dataframe
        standard_df = _create_empty_standard_dataframe()

    # ── Chronological order: flip a newest-first statement to oldest-first ─────
    if (grade is not None and grade.get("ordering") == "newest_first"
            and not standard_df.empty):
        standard_df = standard_df.iloc[::-1].reset_index(drop=True)

    # Derive the closing balance from the last running balance when not printed.
    if not standard_df.empty and "Balance" in standard_df.columns:
        bals = standard_df["Balance"].dropna()
        if len(bals) and not details.get("closing_balance"):
            details["closing_balance"] = f"{float(bals.iloc[-1]):.2f}"

    # ── Stamp the REAL account number + IFSC on every row ─────────────────────
    real_account = details.get("account_number") or ""
    if not real_account or real_account.upper() == "UNREADABLE":
        real_account = f"UNKNOWN-image_group_{group_index}"
    real_ifsc = details.get("ifsc_code") or "UNKNOWN"
    standard_df["Account_ID"] = real_account
    standard_df["IFSC_Code"] = real_ifsc

    # ── Validate and clean (shared by all routes) ─────────────────────────────
    clean_df, flagged_df = validate_and_clean(standard_df)

    details["account_number"] = real_account
    file_record["account_details"] = details
    file_record["rows_standardised"] = len(standard_df)
    file_record["rows_clean"] = len(clean_df)
    file_record["rows_flagged"] = len(flagged_df)

    rows_into_output = len(clean_df) + len(flagged_df)
    file_record["validation"] = {
        "llm_transactions": file_record.get("llm_txn_count", len(standard_df)),
        "rows_in_output": rows_into_output,
        "all_rows_accounted_for": rows_into_output >= len(standard_df),
        "account_number_in_text": bool(details.get("account_number", "")
                                       and details["account_number"] in combined_text),
        "ifsc_in_text": bool(details.get("ifsc_code", "")
                             and details["ifsc_code"] in combined_text),
    }

    # Transaction count audit + first balance break for image groups.
    expected_img = file_record.get("expected_txn_lines", 0) or 0
    extracted_img = len(standard_df)
    completeness_img = round(extracted_img / expected_img, 3) if expected_img > 0 else 1.0
    img_break = None
    if not flagged_df.empty and "flag_reason" in flagged_df.columns:
        if (flagged_df["flag_reason"] == "balance_mismatch").any():
            img_break = find_first_balance_break(standard_df)
    file_record["transaction_count_audit"] = {
        "expected_transaction_count": expected_img,
        "extracted_transaction_count": extracted_img,
        "clean_transaction_count": len(clean_df),
        "flagged_transaction_count": len(flagged_df),
        "completeness_ratio": completeness_img,
        "first_balance_break": img_break,
    }

    logger.info(
        "extraction_pipeline._process_image_group: '%s' → %d clean, %d flagged "
        "(holder=%r account_number=%r, tier=%s).",
        file_record["file"], len(clean_df), len(flagged_df),
        details.get("account_holder"), details.get("account_number"),
        file_record.get("tier"),
    )
    return clean_df, flagged_df, file_record
