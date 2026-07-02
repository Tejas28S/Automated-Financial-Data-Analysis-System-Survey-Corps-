"""Persisted SQLite boundary. Later stages never return to source CSVs."""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator

import pandas as pd

from .config import AnalysisConfig


TRANSACTION_COLUMNS = [
    "source_order", "txn_id", "doc_id", "account_id", "account_id_normalized",
    "date", "date_raw", "time", "narration", "narration_normalized", "reference",
    "reference_alt", "debit_amount", "credit_amount", "balance", "account_number",
    "account_holder", "bank_name", "ifsc_code", "confidence_score", "extraction_tier",
    "flag_reason", "duplicate_of_txn_id", "source_bucket", "eligible_for_detection",
    "confidence_tier", "exclusion_reason", "source_file", "source_row_number",
    "source_page", "is_reversed_source_label", "raw_payload_json",
]


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE transactions (
    row_id INTEGER PRIMARY KEY,
    source_order INTEGER NOT NULL,
    txn_id TEXT NOT NULL,
    doc_id TEXT,
    account_id TEXT,
    account_id_normalized TEXT,
    date TEXT,
    date_raw TEXT,
    time TEXT,
    narration TEXT,
    narration_normalized TEXT,
    reference TEXT,
    reference_alt TEXT,
    debit_amount REAL NOT NULL DEFAULT 0,
    credit_amount REAL NOT NULL DEFAULT 0,
    balance REAL,
    account_number TEXT,
    account_holder TEXT,
    bank_name TEXT,
    ifsc_code TEXT,
    confidence_score REAL,
    extraction_tier TEXT,
    flag_reason TEXT,
    duplicate_of_txn_id TEXT,
    source_bucket TEXT NOT NULL,
    eligible_for_detection INTEGER NOT NULL,
    confidence_tier TEXT NOT NULL,
    exclusion_reason TEXT,
    source_file TEXT NOT NULL,
    source_row_number INTEGER NOT NULL,
    source_page TEXT,
    is_reversed_source_label INTEGER NOT NULL DEFAULT 0,
    raw_payload_json TEXT,
    counterparty_account TEXT,
    counterparty_ifsc TEXT,
    counterparty_name_raw TEXT,
    counterparty_resolution_method TEXT,
    ledger_pair_id TEXT,
    balance_validation_status TEXT
);

CREATE TABLE accounts (
    account_id TEXT PRIMARY KEY,
    account_holder TEXT,
    bank_name TEXT,
    ifsc_code TEXT,
    transaction_count INTEGER NOT NULL DEFAULT 0,
    total_credit REAL NOT NULL DEFAULT 0,
    total_debit REAL NOT NULL DEFAULT 0,
    total_volume REAL NOT NULL DEFAULT 0,
    throughput_ratio REAL NOT NULL DEFAULT 0,
    unique_counterparty_count INTEGER NOT NULL DEFAULT 0,
    first_date TEXT,
    last_date TEXT
);

CREATE TABLE documents (
    doc_id TEXT PRIMARY KEY,
    source_file TEXT,
    transaction_count INTEGER NOT NULL DEFAULT 0,
    eligible_count INTEGER NOT NULL DEFAULT 0,
    excluded_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE baseline_summary (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
);

CREATE TABLE balance_validation (
    row_id INTEGER PRIMARY KEY,
    txn_id TEXT NOT NULL,
    account_id TEXT,
    status TEXT NOT NULL,
    previous_txn_id TEXT,
    expected_balance REAL,
    actual_balance REAL,
    difference REAL,
    explanation TEXT,
    FOREIGN KEY(row_id) REFERENCES transactions(row_id)
);

CREATE TABLE counterparty_cache (
    narration_pattern TEXT PRIMARY KEY,
    result_json TEXT NOT NULL,
    provider TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE possible_same_owner (
    normalized_name TEXT NOT NULL,
    counterparty_name_raw TEXT NOT NULL,
    account_numbers_json TEXT NOT NULL,
    transaction_ids_json TEXT NOT NULL,
    PRIMARY KEY(normalized_name, account_numbers_json)
);

CREATE INDEX idx_transactions_account_id ON transactions(account_id);
CREATE INDEX idx_transactions_date ON transactions(date);
CREATE INDEX idx_transactions_eligible ON transactions(eligible_for_detection);
CREATE INDEX idx_transactions_reference ON transactions(reference);
CREATE INDEX idx_transactions_account_date ON transactions(account_id, date, source_order);
"""


def initialize_database(path: str | Path, config: AnalysisConfig) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {int(config.sqlite_busy_timeout_ms)}")
    connection.executescript(SCHEMA)
    return connection


def load_transactions(connection: sqlite3.Connection, frame: pd.DataFrame) -> None:
    placeholders = ",".join("?" for _ in TRANSACTION_COLUMNS)
    columns = ",".join(TRANSACTION_COLUMNS)
    rows = []
    for record in frame[TRANSACTION_COLUMNS].to_dict(orient="records"):
        values = []
        for column in TRANSACTION_COLUMNS:
            value = record[column]
            if pd.isna(value):
                value = None
            if isinstance(value, bool):
                value = int(value)
            values.append(value)
        rows.append(values)
    connection.executemany(
        f"INSERT INTO transactions ({columns}) VALUES ({placeholders})",
        rows,
    )
    connection.execute(
        """
        INSERT INTO documents(doc_id, source_file, transaction_count, eligible_count, excluded_count)
        SELECT COALESCE(NULLIF(doc_id, ''), 'unknown'), MIN(source_file), COUNT(*),
               SUM(eligible_for_detection), SUM(CASE WHEN eligible_for_detection = 0 THEN 1 ELSE 0 END)
        FROM transactions
        GROUP BY COALESCE(NULLIF(doc_id, ''), 'unknown')
        """
    )
    connection.commit()


def persist_baseline(connection: sqlite3.Connection, summary: dict[str, Any]) -> None:
    connection.execute("DELETE FROM baseline_summary")
    connection.executemany(
        "INSERT INTO baseline_summary(key, value_json) VALUES (?, ?)",
        [(key, json.dumps(value, default=str, sort_keys=True)) for key, value in summary.items()],
    )
    connection.commit()


def fetch_transactions(
    connection: sqlite3.Connection,
    where: str = "1=1",
    parameters: tuple[Any, ...] = (),
) -> pd.DataFrame:
    return pd.read_sql_query(
        f"SELECT * FROM transactions WHERE {where} ORDER BY source_order, row_id",
        connection,
        params=parameters,
    )


@contextmanager
def database_connection(path: str | Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()
