"""
storage.py — Persist every extraction run to disk so the team can OPEN it.

THE PROBLEM THIS SOLVES (Problem 3):
    Before this, the clean transaction table lived only in the computer's memory
    (RAM) and disappeared the moment the program finished. Nobody on the team
    could open it, inspect it, or compare it to the ground truth. The extraction
    was effectively invisible.

THE DESIGN (decided here, like a senior engineer would):
    After every run we write a single, clearly-named folder, one per session:

        outputs/extractions/<session_id>/
            ├── clean_transactions.csv      ← the clean, verified table
            ├── flagged_transactions.csv    ← rows that failed a check (with reason)
            └── metadata.json               ← the "receipt" for the run

    WHY CSV for the tables: a CSV opens directly in Excel, Numbers, or Google
    Sheets with a double-click. A CID investigator with no coding background can
    read the extracted transactions without running a single line of code — which
    is the whole point.

    WHY JSON for the metadata: it captures the things a teammate needs to TRUST
    the run — which files were processed, how many rows each produced, which OCR
    engine read each file (Tesseract or Groq Vision), and exactly which column map
    Groq returned for each document and whether it came from Groq, the cache, or a
    fallback guess. This is what makes the pipeline auditable instead of a black box.

PRIVACY: everything here is written to the LOCAL disk only. Nothing is uploaded
anywhere. This folder holds real (de-anonymised) transaction data, so it is
git-ignored and must never be committed.

Team: Survey Corps | CIDECODE Hackathon 2026 | CID Karnataka
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

from config.settings import EXTRACTIONS_DIR

logger = logging.getLogger(__name__)
SOURCE_ACCOUNT_ID_COLUMN = "source_account_id"


def _append_column_last(df: pd.DataFrame, column: str) -> pd.DataFrame:
    """Ensure an additive output column is present and physically last."""
    out = df.copy()
    if column not in out.columns:
        out[column] = ""
    return out[[c for c in out.columns if c != column] + [column]]


def _df_to_records(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """Turns a transactions DataFrame into JSON-friendly records (dates as strings)."""
    if df is None or df.empty:
        return []
    out = df.copy()
    if "Date" in out.columns:
        out["Date"] = out["Date"].apply(
            lambda d: d.strftime("%d/%m/%Y") if pd.notna(d) and hasattr(d, "strftime") else ""
        )
    return out.to_dict(orient="records")


def persist_extraction_run(
    session_id: str,
    clean_df: pd.DataFrame,
    flagged_df: pd.DataFrame,
    per_file_records: List[Dict[str, Any]],
    summary: Dict[str, Any],
    statements: List[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """
    Writes one extraction run to disk in a human-readable, per-session folder.

    Layout:
        outputs/extractions/<session_id>/
            ├── clean_transactions.csv      ← all clean rows, across every statement
            ├── flagged_transactions.csv    ← rows held for manual review
            ├── metadata.json               ← the run's audit receipt
            └── statements/
                └── <holder>_<account>.json ← ONE structured file PER statement,
                                              with the real account identity +
                                              that statement's transactions

    The clean CSV leads with the REAL account identity columns (account_number,
    account_holder, ifsc_code, bank_name) so anyone opening it can tell, per row,
    whose account it is — never a filename or placeholder (Problems 4 & 7).

    Returns:
        dict of written paths: folder, clean_csv, flagged_csv, metadata_json,
        statements_dir.
    """
    session_dir = Path(EXTRACTIONS_DIR) / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    statements_dir = session_dir / "statements"
    statements_dir.mkdir(parents=True, exist_ok=True)

    clean_path = session_dir / "clean_transactions.csv"
    flagged_path = session_dir / "flagged_transactions.csv"
    duplicates_path = session_dir / "duplicates.csv"
    metadata_path = session_dir / "metadata.json"

    statements = statements or []

    # ── 1. Per-statement structured JSON (real identity + transactions) ───────
    # Map each REAL account number → its holder, to add a holder column to the CSV.
    holder_by_account: Dict[str, str] = {}
    statement_files = []
    for stmt in statements:
        details = stmt.get("account_details", {}) or {}
        accnum = details.get("account_number", "")
        if accnum:
            holder_by_account[accnum] = details.get("account_holder", "")
        stmt_df = stmt.get("clean_df")
        bundle = {
            "account_details": details,        # real holder, number, IFSC, bank, ...
            "transaction_count": int(len(stmt_df)) if stmt_df is not None else 0,
            "transactions": _df_to_records(stmt_df),
        }
        # Name the file by holder + account number so it is obvious whose it is.
        holder = (details.get("account_holder") or "unknown").replace(" ", "_")
        acct = (details.get("account_number") or "unknown")
        safe = re.sub(r"[^A-Za-z0-9_\-]+", "", f"{holder}_{acct}") or "statement"
        stmt_path = statements_dir / f"{safe}.json"
        with open(stmt_path, "w", encoding="utf-8") as f:
            json.dump(bundle, f, indent=2, default=str)
        statement_files.append(stmt_path.name)

    # ── 2a. Separate exact duplicates into their own CSV ──────────────────────
    # mark_duplicates() tags later copies of a transaction with `duplicate_of` (the
    # txn_id of the first occurrence). Per investigator request, duplicates are NOT
    # mixed into the clean table — they are pulled out into duplicates.csv with a
    # full audit trail (which clean row each duplicates, its date/amount/narration,
    # and the reason). The clean table then keeps only the FIRST occurrence of each
    # transaction. Nothing is lost: every duplicate is recorded in duplicates.csv.
    duplicates_out = pd.DataFrame()
    clean_df = clean_df.reset_index(drop=True)
    if "duplicate_of" in clean_df.columns and not clean_df.empty:
        rownum_by_txnid = {
            tid: i for i, tid in enumerate(clean_df.get("txn_id", pd.Series(dtype=str)).tolist())
        }
        dup_mask = clean_df["duplicate_of"].notna()
        if dup_mask.any():
            dup_records = []
            for i, row in clean_df[dup_mask].iterrows():
                debit = float(row.get("Debit", 0) or 0)
                credit = float(row.get("Credit", 0) or 0)
                amount = debit if debit > 0 else credit
                date_val = row.get("Date", "")
                if pd.notna(date_val) and hasattr(date_val, "strftime"):
                    date_val = date_val.strftime("%d/%m/%Y")
                dup_records.append({
                    "duplicate_row_number": int(i),
                    "original_row_number": rownum_by_txnid.get(row.get("duplicate_of"), ""),
                    "account_number": row.get("Account_ID", ""),
                    "date": date_val,
                    "amount": amount,
                    "debit": debit,
                    "credit": credit,
                    "narration": row.get("Narration", ""),
                    "reason_flagged": "exact_duplicate (same Date + Narration + Debit + Credit + Account)",
                    "source_account_id": row.get(SOURCE_ACCOUNT_ID_COLUMN, ""),
                })
            duplicates_out = pd.DataFrame(dup_records)
        # Keep only first occurrences in the clean table.
        clean_df = clean_df[clean_df["duplicate_of"].isna()].reset_index(drop=True)
    # Always write the file WITH headers (even when there are no duplicates) so the
    # output schema is stable and downstream readers never hit an empty file.
    if duplicates_out.empty:
        duplicates_out = pd.DataFrame(columns=[
            "duplicate_row_number", "original_row_number", "account_number",
            "date", "amount", "debit", "credit", "narration", "reason_flagged",
            "source_account_id",
        ])
    duplicates_out = _append_column_last(duplicates_out, SOURCE_ACCOUNT_ID_COLUMN)
    duplicates_out.to_csv(duplicates_path, index=False)

    # ── 2. Combined clean transactions → CSV, led by REAL identity columns ────
    # The Account_ID column already holds the REAL account number, and IFSC_Code
    # carries the IFSC, for every row. We rename them to friendly headers and add
    # the account holder, so anyone opening the CSV can tell whose account it is.
    clean_out = clean_df.copy()
    if not clean_out.empty:
        clean_out = clean_out.rename(columns={
            "Account_ID": "account_number",
            "IFSC_Code": "ifsc_code",
        })
        clean_out["account_holder"] = clean_out["account_number"].map(
            lambda a: holder_by_account.get(a, ""))
        lead = ["account_number", "account_holder", "ifsc_code"]
        present_lead = [c for c in lead if c in clean_out.columns]
        clean_out = clean_out[present_lead + [c for c in clean_out.columns if c not in present_lead]]
    clean_out = _append_column_last(clean_out, SOURCE_ACCOUNT_ID_COLUMN)
    clean_out.to_csv(clean_path, index=False)

    # ── 3. Flagged rows → CSV. Surfaced, NEVER dropped silently. ──────────────
    flagged_df = _append_column_last(flagged_df, SOURCE_ACCOUNT_ID_COLUMN)
    flagged_df.to_csv(flagged_path, index=False)

    # ── 4. Metadata → JSON: the run's audit receipt ───────────────────────────
    metadata = {
        "session_id": session_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "summary": summary,
        "files": per_file_records,
        "output_files": {
            "clean_transactions": clean_path.name,
            "flagged_transactions": flagged_path.name,
            "duplicates": duplicates_path.name,
            "statements_dir": "statements/",
            "statement_files": statement_files,
        },
        "duplicate_count": int(len(duplicates_out)),
    }
    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2, default=str)

    logger.info(
        "storage.persist_extraction_run: Wrote session '%s' → %s "
        "(%d clean rows, %d flagged rows, %d statement JSON file(s)).",
        session_id, session_dir, len(clean_df), len(flagged_df), len(statement_files),
    )

    return {
        "folder": str(session_dir),
        "clean_csv": str(clean_path),
        "flagged_csv": str(flagged_path),
        "duplicates_csv": str(duplicates_path),
        "metadata_json": str(metadata_path),
        "statements_dir": str(statements_dir),
    }
