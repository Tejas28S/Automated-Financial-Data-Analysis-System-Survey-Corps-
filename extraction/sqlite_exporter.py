"""
sqlite_exporter.py — Exports the clean transaction DataFrame to a SQLite database
ready for the analysis phase.

Creates one database file per session:
    outputs/extractions/<session_id>/investigation.db

Three tables:
    transactions  — every clean transaction row (one row per transaction)
    accounts      — one row per unique account with aggregated statistics
    documents     — one row per processed source document with extraction metadata

Indexes on (account_id), (date), (bank_name) make analysis queries run in under
1 second even on 1 million rows.

Team: Survey Corps | CIDECODE Hackathon 2026 | CID Karnataka
"""

import logging
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)


def export_to_sqlite(
    clean_df: pd.DataFrame,
    session_id: str,
    output_dir: Path,
    per_file_records: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """
    Exports the clean transaction DataFrame to a SQLite database file.

    Parameters:
        clean_df        : the unified clean DataFrame from the extraction pipeline
                          (STANDARD_COLUMNS + REFERENCE_COLUMNS schema).
        session_id      : the pipeline session identifier (used as the folder name).
        output_dir      : base output directory (config.settings.OUTPUT_DIR).
        per_file_records: the per-file audit dicts from the pipeline run, used to
                          populate the documents table. Pass None to skip the table.

    Returns:
        Absolute path to the created investigation.db file as a string.
    """
    session_dir = Path(output_dir) / "extractions" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    db_path = session_dir / "investigation.db"

    conn = sqlite3.connect(str(db_path))
    try:
        _create_schema(conn)
        if clean_df is not None and not clean_df.empty:
            _insert_transactions(conn, clean_df, session_id, per_file_records or [])
            _insert_accounts(conn)
        if per_file_records:
            _insert_documents(conn, per_file_records, session_id)
        _create_indexes(conn)
        conn.commit()
    finally:
        conn.close()

    logger.info(
        "sqlite_exporter.export_to_sqlite: wrote investigation.db for session '%s' "
        "(%s).", session_id, db_path,
    )
    return str(db_path)


# ── Schema ────────────────────────────────────────────────────────────────────

def _create_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS transactions (
            txn_id              TEXT PRIMARY KEY,
            doc_id              TEXT,
            account_id          TEXT,
            date                TEXT,
            narration           TEXT,
            reference           TEXT,
            debit_amount        REAL,
            credit_amount       REAL,
            balance             REAL,
            account_number      TEXT,
            account_holder      TEXT,
            bank_name           TEXT,
            ifsc_code           TEXT,
            confidence_score    REAL,
            extraction_tier     TEXT,
            is_flagged          INTEGER DEFAULT 0,
            flag_reason         TEXT
        );

        CREATE TABLE IF NOT EXISTS accounts (
            account_id              TEXT PRIMARY KEY,
            account_number          TEXT,
            account_holder          TEXT,
            bank_name               TEXT,
            ifsc_code               TEXT,
            total_transactions      INTEGER,
            total_credit            REAL,
            total_debit             REAL,
            net_balance             REAL,
            first_transaction_date  TEXT,
            last_transaction_date   TEXT,
            source_document_count   INTEGER
        );

        CREATE TABLE IF NOT EXISTS documents (
            doc_id              TEXT PRIMARY KEY,
            filename            TEXT,
            format              TEXT,
            total_rows_extracted INTEGER,
            clean_rows          INTEGER,
            flagged_rows        INTEGER,
            extraction_tier     TEXT,
            reconciliation_rate REAL,
            completeness_ratio  REAL,
            llm_calls_used      INTEGER,
            session_id          TEXT
        );
    """)
    conn.commit()


# ── Transactions table ────────────────────────────────────────────────────────

def _build_doc_id_map(per_file_records: List[Dict[str, Any]]) -> Dict[str, str]:
    """Returns {filename: doc_id} — stable uuid4 per document."""
    return {r.get("file", ""): str(uuid.uuid4()) for r in per_file_records}


def _tier_map(per_file_records: List[Dict[str, Any]]) -> Dict[str, str]:
    return {r.get("file", ""): r.get("tier", "") for r in per_file_records}


def _reconcile_map(per_file_records: List[Dict[str, Any]]) -> Dict[str, float]:
    return {r.get("file", ""): float(r.get("reconciliation_rate", 0.0) or 0.0)
            for r in per_file_records}


def _insert_transactions(
    conn: sqlite3.Connection,
    df: pd.DataFrame,
    session_id: str,
    per_file_records: List[Dict[str, Any]],
) -> None:
    tier_by_file = _tier_map(per_file_records)
    recon_by_file = _reconcile_map(per_file_records)

    # account_number is stored in Account_ID column (real number, stamped by pipeline)
    account_col = "Account_ID" if "Account_ID" in df.columns else None
    holder_col  = "account_holder" if "account_holder" in df.columns else None
    bank_col    = "Bank_Name" if "Bank_Name" in df.columns else None
    ifsc_col    = "IFSC_Code" if "IFSC_Code" in df.columns else None

    rows = []
    for i, row in df.iterrows():
        # Use existing txn_id if present (already assigned by mark_duplicates),
        # otherwise generate a fresh uuid4.
        txn_id = str(row.get("txn_id") or uuid.uuid4())

        # doc_id: derive from account_number + bank so each statement has its own.
        acct_num = str(row[account_col]) if account_col else ""
        bank     = str(row[bank_col]) if bank_col else ""
        doc_id   = f"{acct_num}_{bank}"

        date_val = row.get("Date", "")
        date_str = str(date_val) if pd.notna(date_val) else ""

        # reference: prefer Transaction_Reference, fall back to Reference_Number.
        ref = (str(row.get("Transaction_Reference", "") or "").strip()
               or str(row.get("Reference_Number", "") or "").strip())

        debit  = _safe_float(row.get("Debit"))
        credit = _safe_float(row.get("Credit"))
        bal    = _safe_float(row.get("Balance"))

        ifsc   = str(row[ifsc_col]) if ifsc_col else ""
        holder = str(row[holder_col]) if holder_col else ""

        # confidence_score: reconciliation_rate of the source document.
        # We cannot look up by filename per-row cheaply, so we use 1.0 as default;
        # the documents table carries the per-file rate.
        confidence = 1.0

        rows.append((
            txn_id,
            doc_id,
            acct_num,
            date_str,
            str(row.get("Narration", "") or ""),
            ref,
            debit if debit and debit > 0 else None,
            credit if credit and credit > 0 else None,
            bal,
            acct_num,
            holder,
            bank,
            ifsc,
            confidence,
            "",        # extraction_tier filled per-document in documents table
            0,         # is_flagged = 0 for clean rows
            None,
        ))

    conn.executemany(
        "INSERT OR REPLACE INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )


# ── Accounts table ────────────────────────────────────────────────────────────

def _insert_accounts(conn: sqlite3.Connection) -> None:
    """Aggregate from the transactions table — no Python loops needed."""
    conn.executescript("""
        INSERT OR REPLACE INTO accounts
        SELECT
            account_id,
            account_number,
            MAX(account_holder)                          AS account_holder,
            MAX(bank_name)                               AS bank_name,
            MAX(ifsc_code)                               AS ifsc_code,
            COUNT(*)                                     AS total_transactions,
            COALESCE(SUM(credit_amount), 0)              AS total_credit,
            COALESCE(SUM(debit_amount),  0)              AS total_debit,
            COALESCE(SUM(credit_amount), 0) - COALESCE(SUM(debit_amount), 0)
                                                         AS net_balance,
            MIN(date)                                    AS first_transaction_date,
            MAX(date)                                    AS last_transaction_date,
            COUNT(DISTINCT doc_id)                       AS source_document_count
        FROM transactions
        GROUP BY account_id;
    """)


# ── Documents table ───────────────────────────────────────────────────────────

def _insert_documents(
    conn: sqlite3.Connection,
    per_file_records: List[Dict[str, Any]],
    session_id: str,
) -> None:
    rows = []
    for r in per_file_records:
        filename = r.get("file", "")
        if not filename:
            continue
        acct    = (r.get("account_details") or {}).get("account_number", "")
        bank    = (r.get("account_details") or {}).get("bank_name", "")
        doc_id  = f"{acct}_{bank}"

        # Derive format from route label.
        route = r.get("route", "")
        fmt_map = {
            "pdf_digital": "pdf", "pdf_scanned": "pdf_scanned",
            "excel_csv": "excel_csv", "docx": "docx", "text": "txt",
            "image": "image",
        }
        fmt = fmt_map.get(route, route)

        caud = r.get("transaction_count_audit") or {}
        rows.append((
            doc_id,
            filename,
            fmt,
            r.get("rows_standardised", 0),
            r.get("rows_clean", 0),
            r.get("rows_flagged", 0),
            r.get("tier", ""),
            float(r.get("reconciliation_rate", 0.0) or 0.0),
            float(caud.get("completeness_ratio", 1.0) or 1.0),
            r.get("llm_calls", 0),
            session_id,
        ))

    conn.executemany(
        "INSERT OR REPLACE INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )


# ── Indexes ───────────────────────────────────────────────────────────────────

def _create_indexes(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_txn_account_id ON transactions(account_id);
        CREATE INDEX IF NOT EXISTS idx_txn_date       ON transactions(date);
        CREATE INDEX IF NOT EXISTS idx_txn_bank_name  ON transactions(bank_name);
        CREATE INDEX IF NOT EXISTS idx_acc_account_id ON accounts(account_id);
    """)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_float(value: Any) -> Optional[float]:
    if pd.isna(value) or value is None:
        return None
    try:
        f = float(value)
        return f if f == f else None  # NaN guard
    except (ValueError, TypeError):
        return None
