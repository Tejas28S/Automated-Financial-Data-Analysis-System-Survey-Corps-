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

from config.settings import STANDARD_COLUMNS, require_extraction_keys
from extraction.router import route_file
from extraction.extractor_digital_pdf import extract_text_from_digital_pdf
from extraction.extractor_ocr import extract_text_with_ocr_audit
from extraction.extractor_excel_csv import extract_dataframe_from_excel_csv
from extraction.extractor_docx import extract_text_from_docx
from extraction.vision_extractor import extract_statement_from_image
from extraction.account_extractor import reconcile_account_details
from extraction.identifier_vault import IdentifierVault
from extraction.column_identifier import identify_column_structure
from extraction.llm_structurer import (
    structure_statement,
    extract_account_metadata,
    discover_transaction_schema,
)
from extraction.standardiser import (
    standardise_transactions,
    standardise_dataframe_direct,
    standardise_transaction_records,
    standardise_llm_transactions,
    standardise_digital_pdf_transactions,
    count_transaction_like_lines,
    DATE_AT_LINE_START_PATTERN,
)
from extraction.validator import validate_and_clean, mark_duplicates
from extraction.storage import persist_extraction_run

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

    for file_index, file_info in enumerate(files, start=1):
        file_path = file_info.get("file_path", "")
        account_id = file_info.get("account_id", f"ACC{file_index:03d}")
        bank_name = file_info.get("bank_name", "Unknown Bank")

        logger.info(
            "extraction_pipeline.run_extraction_pipeline: "
            "Processing file %d of %d: '%s'",
            file_index,
            len(files),
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
            columns=STANDARD_COLUMNS + ["is_reversed", "txn_id", "duplicate_of"])
        logger.warning(
            "extraction_pipeline.run_extraction_pipeline: "
            "No clean rows produced from any file."
        )

    if all_flagged_dfs:
        unified_flagged_df = pd.concat(all_flagged_dfs, ignore_index=True)
    else:
        unified_flagged_df = pd.DataFrame(columns=STANDARD_COLUMNS + ["flag_reason"])

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

    # ── IMAGE PATH (Problem 1 + 2): vision LLM reads the whole picture ─────
    if route == "image":
        # No Tesseract for images — send the picture straight to the vision model.
        vision = extract_statement_from_image(file_path)
        content_details = vision.get("account_details", {})
        file_record["ocr"] = {
            "engine": vision.get("engine"),
            "source": vision.get("source"),   # "groq" (spent tokens) or "cache" (free)
            "raw_chars": vision.get("raw_chars"),
        }
        file_record["column_map_source"] = "vision"  # the vision model returns structured rows
        # Build the identity purely from what the image showed (no reference file).
        details = reconcile_account_details(content_details, account_id, bank_name)
        standard_df = standardise_transaction_records(
            vision.get("transactions", []), account_id, details["bank_name"] or bank_name,
        )
        file_record["raw_text_chars"] = vision.get("raw_chars", 0)

    else:
        # ── Component 2: Extract raw content for the text/table sources ───
        if route == "pdf_digital":
            raw_text = extract_text_from_digital_pdf(file_path)
        elif route == "pdf_scanned":
            raw_text, ocr_audit = extract_text_with_ocr_audit(file_path, max_pages=max_ocr_pages)
            file_record["ocr"] = ocr_audit
        elif route == "docx":
            raw_text = extract_text_from_docx(file_path)
        elif route == "excel_csv":
            raw_df = extract_dataframe_from_excel_csv(file_path, account_id, bank_name)
        else:
            raise ValueError(f"Unknown route '{route}' returned by router.")

        file_record["raw_text_chars"] = len(raw_text)

        if raw_df is not None and not raw_df.empty:
            # ── EXCEL / CSV: already a structured table (not OCR). Groq maps the
            # columns, then deterministic pandas standardises. These files carry no
            # header identity, so the real account number / IFSC stay UNKNOWN.
            content_details = {}
            details = reconcile_account_details(content_details, account_id, bank_name)
            helper_cols = [c for c in ("Account_ID", "Bank_Name") if c in raw_df.columns]
            column_id_text = raw_df.drop(columns=helper_cols).head(40).to_csv(index=False)
            vault = IdentifierVault(details)
            col_result = identify_column_structure(vault.redact(column_id_text), file_path)
            column_map = vault.restore(col_result["column_map"])
            file_record["column_map"] = column_map
            file_record["column_map_source"] = col_result["source"]
            standard_df = standardise_dataframe_direct(
                raw_df, column_map, account_id, details["bank_name"] or bank_name)

        elif route == "pdf_digital":
            # ── DIGITAL PDF: original design — LLM for DISCOVERY only, code for bulk.
            # Metadata is read by the LLM (unchanged). For transactions, the LLM sees
            # only a small SAMPLE of rows and returns a parsing schema; deterministic
            # code then parses EVERY row with that schema. A statement with thousands
            # of transactions therefore costs just two LLM calls (metadata + schema),
            # never one call per row.
            meta = extract_account_metadata(raw_text, file_path)
            details = _details_from_meta(meta, account_id, bank_name)

            # Take the first ~35 transaction rows (lines starting with a date) as the
            # discovery sample — the LLM never sees the whole statement.
            sample_lines = [ln for ln in raw_text.splitlines()
                            if DATE_AT_LINE_START_PATTERN.match(ln.strip())][:35]
            schema = discover_transaction_schema("\n".join(sample_lines), file_path)

            file_record["column_map"] = {"engine": "llm_schema_discovery", "schema":
                                         {k: v for k, v in schema.items() if k != "source"}}
            file_record["column_map_source"] = schema.get("source", "groq")

            standard_df = standardise_digital_pdf_transactions(
                raw_text, account_id, details["bank_name"] or bank_name,
                details.get("opening_balance", ""), schema)

            # ── GENERALIZATION GUARD (format-agnostic) ───────────────────────
            # The deterministic parser above assumes a layout (date at line start,
            # balance = last token, 2-decimal amounts). On an UNSEEN bank whose
            # layout breaks any of those assumptions it silently returns far fewer
            # rows than the statement actually contains — the exact failure where
            # "raw text has transactions but the output is empty". We detect this
            # WITHOUT naming any bank: count the lines that look like transactions
            # and, if the parser captured far fewer, re-extract with the LLM
            # structurer (the same path scanned PDFs use), which reads any layout.
            expected_rows = count_transaction_like_lines(raw_text)
            if expected_rows >= 3 and len(standard_df) < 0.6 * expected_rows:
                logger.warning(
                    "extraction_pipeline._process_single_file: deterministic digital-PDF "
                    "parser under-extracted for '%s' (%d rows vs ~%d transaction-like "
                    "lines). Falling back to the LLM structurer for generalisation.",
                    Path(file_path).name, len(standard_df), expected_rows,
                )
                structured = structure_statement(raw_text, file_path)
                llm_df = standardise_llm_transactions(
                    structured.get("transactions", []), account_id,
                    details["bank_name"] or bank_name, details.get("opening_balance", ""))
                # Only adopt the LLM result if it actually recovered more rows, so a
                # rate-limited/empty LLM reply can never make the output worse.
                if len(llm_df) > len(standard_df):
                    standard_df = llm_df
                    file_record["column_map"] = {"engine": "llm_structurer_fallback"}
                    file_record["column_map_source"] = structured.get("source")
                    file_record["llm_txn_count"] = len(structured.get("transactions", []))

        else:
            # ── SCANNED PDF / DOCX: unchanged — LLM-first structuring (per scope).
            structured = structure_statement(raw_text, file_path)
            details = _details_from_meta(structured.get("account_details", {}), account_id, bank_name)
            file_record["column_map"] = {"engine": "llm_structurer"}
            file_record["column_map_source"] = structured.get("source")
            file_record["llm_txn_count"] = len(structured.get("transactions", []))
            standard_df = standardise_llm_transactions(
                structured.get("transactions", []), account_id,
                details["bank_name"] or bank_name, details.get("opening_balance", ""))

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
    # 9: every transaction the LLM identified must end up in the output (clean +
    #    flagged). Any gap is rows dropped for an unparseable date — surfaced here.
    # 8: the key account identifiers must be traceable back to the raw text.
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
    logger.info(
        "extraction_pipeline._process_single_file: '%s' → %d clean, %d flagged "
        "(holder=%r account_number=%r).",
        Path(file_path).name, len(clean_df), len(flagged_df),
        details.get("account_holder"), details.get("account_number"),
    )

    return clean_df, flagged_df, file_record
