"""
extractor_excel_csv.py — Direct reading of Excel and CSV bank statement files.

Excel (.xlsx, .xls) and CSV (.csv) files are already structured in rows and columns,
so we do not need OCR or LLM processing to read them. Pandas — a Python data
analysis library — can read these files directly into a DataFrame (a table).

However, the column names in these files vary from bank to bank:
  - SBI may call a column "Withdrawal Amt" while HDFC calls it "Debit"
  - Some banks have separate "Value Date" and "Transaction Date" columns
  - The narration column might be called "Description", "Particulars", or "Remarks"

This module reads the raw file and attaches the Account_ID and Bank_Name
provided by the investigator. The standardiser (standardiser.py) then uses
the Groq column mapping to rename columns to the unified schema.

For Excel files, use openpyxl engine.
For CSV files, try multiple text encodings — Indian bank systems sometimes
produce CSV files with Windows-specific encoding that is not plain UTF-8.

Team: Survey Corps | CIDECODE Hackathon 2026 | CID Karnataka
"""

import csv
import io
import logging
import re
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

# Set up a logger for this module.
logger = logging.getLogger(__name__)

# A value that looks like a calendar date (DD/MM/YYYY, DD-MM-YY, YYYY-MM-DD, …).
_DATE_LIKE = re.compile(r"^\d{1,4}[/\-.]\d{1,2}[/\-.]\d{2,4}")

# ── Metadata-block labels (key:value rows printed above the transaction table) ──
# Maps a label spelling (lower-cased) to the identity field it populates. A bank
# export may carry this block or not — both cases are handled. Generic banking
# vocabulary only (no bank names), so the anti-overfitting guard is unaffected.
_META_LABELS = {
    "account_number": ["account number", "account no", "a/c number", "a/c no",
                       "acct number", "acct no", "account #"],
    "account_holder": ["account holder", "account name", "customer name",
                       "holder name", "a/c holder", "name of account holder"],
    "ifsc_code": ["ifsc code", "ifsc"],
    "branch": ["branch name", "branch address", "branch"],
    "account_type": ["account type", "a/c type", "scheme"],
    "bank_name": ["bank name", "bank"],
    "currency": ["currency"],
    "opening_balance": ["opening balance", "brought forward"],
    "closing_balance": ["closing balance", "carried forward"],
    "statement_period": ["statement period", "period"],
}

# ── Column-header keyword map (header label → standard field), priority order ───
# Used to map the transaction table's columns DETERMINISTICALLY from their header
# names — no LLM needed for a normal Excel/CSV with a header row. Each tuple is
# (standard_field, [keyword spellings, most specific first]).
_COL_KEYWORDS = [
    ("date", ["transaction date", "txn date", "value date", "posting date",
              "tran date", "date"]),
    ("narration", ["narration", "description", "particulars", "remarks",
                   "transaction details", "details"]),
    ("balance", ["closing balance", "running balance", "available balance",
                 "balance"]),
    ("debit", ["withdrawal amt", "withdrawal", "withdrawals", "debit amount",
               "debit", "w/drl", "dr amount", "dr"]),
    ("credit", ["deposit amt", "deposit", "deposits", "credit amount",
                "credit", "cr amount", "cr"]),
    ("cheque_number", ["cheque number", "cheque no", "chq no", "instrument no",
                       "chq"]),
    ("reference_number", ["chq/ref no", "reference no", "reference number",
                          "reference", "ref no", "utr", "rrn", "ref"]),
]


def _is_blank(val) -> bool:
    s = str(val).strip().lower()
    return s == "" or s == "nan" or s == "none"


def _is_numeric(val) -> bool:
    """True if the value parses as a number (ignoring thousands separators / ₹)."""
    s = str(val).strip().replace(",", "").replace("₹", "").lstrip("-")
    if not s:
        return False
    try:
        float(s)
        return True
    except ValueError:
        return False


def _row_is_all_text(cells) -> bool:
    """A header row: ≥2 non-empty cells, NONE of which is a number or a date."""
    nonempty = [c for c in cells if not _is_blank(c)]
    if len(nonempty) < 2:
        return False
    return all(not _is_numeric(c) and not _DATE_LIKE.match(str(c).strip()) for c in nonempty)


def _row_has_data(cells) -> bool:
    """A data row: ≥2 non-empty cells, at least one of which is a number or a date."""
    nonempty = [c for c in cells if not _is_blank(c)]
    if len(nonempty) < 2:
        return False
    return any(_is_numeric(c) or _DATE_LIKE.match(str(c).strip()) for c in nonempty)


def _detect_header_index(rows: List[list]) -> int:
    """
    Finds the row that is the REAL column header in a table that may have
    letterhead / metadata / comment lines above it.

    Bank exports often prepend a key:value metadata block (Account Number, IFSC,
    Account Holder, Period …) above the actual "Date,Narration,Debit,Credit,Balance"
    header. Those rows confuse pandas (it infers the wrong width and errors with
    "Expected 1 fields in line 4, saw 5") or get mistaken for the header.

    The header is identified by SHAPE, not by a naive "first all-text row":
      • The transaction table dominates the file, so its column count (the modal
        width of data rows) is the true table width.
      • The header is the FIRST row that is (a) entirely text labels, (b) about as
        WIDE as the table, and (c) immediately followed by a data row.
    The width test is what rejects a 2-column "Account Type | Savings" metadata
    row while keeping a 6-column "Date|Narration|Ref|Withdrawal|Deposit|Balance".

    Returns the 0-based index of the header row (0 = no metadata to skip).
    """
    if not rows:
        return 0

    widths = [len([c for c in r if not _is_blank(c)]) for r in rows]
    data_flags = [_row_has_data(r) for r in rows]

    # Table width = the most common width among data rows (they dominate the file).
    data_widths = [w for w, d in zip(widths, data_flags) if d]
    table_width = Counter(data_widths).most_common(1)[0][0] if data_widths else max(widths)
    min_header_width = max(2, table_width - 1)

    # Header = first WIDE all-text row immediately above a data row (skip blanks).
    for i in range(len(rows)):
        if not _row_is_all_text(rows[i]) or widths[i] < min_header_width:
            continue
        j = i + 1
        while j < len(rows) and widths[j] == 0:
            j += 1
        if j < len(rows) and data_flags[j]:
            return i

    # Fallback: the text row directly above the first data row, else that data row.
    for i, is_data in enumerate(data_flags):
        if is_data:
            if i > 0 and _row_is_all_text(rows[i - 1]):
                return i - 1
            return i

    # Last resort: first row with at least two non-empty cells.
    for i, w in enumerate(widths):
        if w >= 2:
            return i
    return 0


def _norm_label(s) -> str:
    """Lower-case, collapse whitespace, drop a trailing colon — for label matching."""
    return re.sub(r"\s+", " ", str(s).strip().lower()).rstrip(":").strip()


def _parse_metadata_block(rows: List[list]) -> Dict[str, str]:
    """
    Reads the key:value identity block printed ABOVE the transaction table.

    Each metadata row is a label in the first cell and its value in the next
    (e.g. "Account Number | 0318607509560006", "IFSC Code | KKBK0064735").
    Returns a dict keyed by the standard identity fields. Empty when the sheet
    carries no such block (which is fine — identity then stays blank, never
    invented).
    """
    details: Dict[str, str] = {}
    period_from = period_to = ""

    for r in rows:
        cells = [str(c).strip() for c in r if not _is_blank(c)]
        if not cells:
            continue

        if len(cells) >= 2:
            # Two-cell layout: label in the first cell, value in the next.
            label = _norm_label(cells[0])
            value = cells[1].strip()
        else:
            # Single-cell layout: "Account No: 12345" or a "# Account No: 12345"
            # comment line. Strip a leading comment marker and split on the colon.
            text = cells[0].lstrip("#").strip()
            if ":" not in text:
                continue
            label_part, _, value = text.partition(":")
            label = _norm_label(label_part)
            value = value.strip()

        if not value:
            continue

        # "Period From" / "Period To" are combined into one statement_period.
        if label.startswith("period from") or label == "from":
            period_from = value
            continue
        if label.startswith("period to") or label == "to":
            period_to = value
            continue

        for field, variants in _META_LABELS.items():
            if any(label == v or label.startswith(v) for v in variants):
                details.setdefault(field, value)
                break

    if (period_from or period_to) and "statement_period" not in details:
        details["statement_period"] = f"{period_from} to {period_to}".strip()

    return details


def _infer_column_map(columns) -> Dict[str, str]:
    """
    Maps the transaction table's columns to standard fields by their HEADER NAMES,
    deterministically (no LLM). This is what makes Excel/CSV extraction independent
    of the Groq token quota and immune to the positional-fallback bug where a "Ref"
    column was assigned to "Debit".

    Returns {standard_field: actual_column_name}. Reference/cheque columns ARE
    detected here too, but the caller passes only the core money fields to the
    standardiser — reference/cheque are resolved separately and semantically.
    """
    cols = [str(c) for c in columns]
    normed = {c: re.sub(r"\s+", " ", c.strip().lower()) for c in cols}
    used = set()
    cmap: Dict[str, str] = {}

    for field, keywords in _COL_KEYWORDS:
        chosen = None
        # Exact header match first (most reliable).
        for kw in keywords:
            for c in cols:
                if c not in used and normed[c] == kw:
                    chosen = c
                    break
            if chosen:
                break
        # Then substring match, but skip 2-char keywords ("dr"/"cr") to avoid
        # accidental hits inside unrelated words.
        if not chosen:
            for kw in keywords:
                if len(kw) <= 2:
                    continue
                for c in cols:
                    if c not in used and kw in normed[c]:
                        chosen = c
                        break
                if chosen:
                    break
        if chosen:
            cmap[field] = chosen
            used.add(chosen)

    return cmap


def extract_dataframe_from_excel_csv(
    file_path: str,
    account_id: str,
    bank_name: str,
) -> pd.DataFrame:
    """
    Reads an Excel or CSV bank statement directly into a pandas DataFrame.

    Excel and CSV files already contain structured tabular data, so no
    OCR or LLM processing is needed. The data is read directly and
    immediately passed to the standardiser.

    Handles both .xlsx/.xls (via openpyxl) and .csv (via pandas read_csv).
    Tries multiple encodings for CSV files: utf-8, latin-1, cp1252.
    The cp1252 encoding is common in files generated by older Windows-based
    Indian banking software.

    Attaches Account_ID and Bank_Name to every row so the unified DataFrame
    always knows which account each transaction belongs to.

    Parameters:
        file_path (str): Absolute path to the Excel or CSV file.
        account_id (str): Investigator-provided account identifier
                          (e.g., "ACC001" or "SBI_RAVI_KUMAR").
        bank_name (str): Investigator-provided bank name (e.g., "SBI", "HDFC").

    Returns:
        pd.DataFrame: Raw DataFrame with original column names plus
                      Account_ID and Bank_Name columns added.
                      Returns empty DataFrame if the file cannot be read.
    """
    path = Path(file_path)
    extension = path.suffix.lower()

    logger.info(
        "extractor_excel_csv.extract_dataframe_from_excel_csv: "
        "Reading file '%s' for account '%s' at bank '%s'",
        path.name,
        account_id,
        bank_name,
    )

    df: Optional[pd.DataFrame] = None
    metadata: Dict[str, str] = {}

    # ── Excel files (.xlsx or .xls) ───────────────────────────────────────────
    if extension in (".xlsx", ".xls"):
        df, metadata = _read_excel_file(file_path)

    # ── CSV files (.csv) ──────────────────────────────────────────────────────
    elif extension == ".csv":
        df, metadata = _read_csv_file(file_path)

    else:
        logger.error(
            "extractor_excel_csv.extract_dataframe_from_excel_csv: "
            "Unexpected extension '%s' — this extractor only handles Excel and CSV.",
            extension,
        )
        return pd.DataFrame()

    # If reading failed, return an empty DataFrame so the pipeline can continue.
    if df is None or df.empty:
        logger.warning(
            "extractor_excel_csv.extract_dataframe_from_excel_csv: "
            "File '%s' produced an empty DataFrame.",
            path.name,
        )
        return pd.DataFrame()

    # Map the columns to standard fields by their header names BEFORE adding the
    # helper columns (so Account_ID / Bank_Name can never be mistaken for a field).
    inferred_map = _infer_column_map(df.columns)

    # Attach the investigator-provided identifiers to every row.
    # This is essential for cross-account analysis — every transaction must
    # carry its account identity so we can trace money flows between accounts.
    df["Account_ID"] = account_id
    df["Bank_Name"] = bank_name

    # Carry the sheet's own identity block + the deterministic column map alongside
    # the DataFrame so the pipeline can use them (set LAST so no later op drops them).
    df.attrs["statement_metadata"] = metadata or {}
    df.attrs["inferred_column_map"] = inferred_map or {}

    logger.info(
        "extractor_excel_csv.extract_dataframe_from_excel_csv: "
        "Read %d rows × %d cols from '%s'. inferred_map=%s metadata_fields=%s",
        len(df), len(df.columns), path.name,
        inferred_map, sorted(metadata.keys()) if metadata else [],
    )

    return df


def _read_excel_file(file_path: str) -> Tuple[Optional[pd.DataFrame], Dict[str, str]]:
    """
    Reads an Excel file (.xlsx or .xls) using the openpyxl engine.

    Reads with header=None first so every row is visible, then locates the true
    column header (skipping any letterhead / key:value metadata rows above the
    table) and parses that metadata block into account identity.

    Returns:
        (DataFrame or None, metadata dict). The DataFrame has the real headers
        applied; the metadata dict carries any account identity found above the
        table (empty if none).
    """
    try:
        # header=None → see every row (including any metadata rows above the table).
        # dtype=str preserves values like an account number with a leading zero
        # (otherwise pandas would coerce it to a float and corrupt it).
        raw = pd.read_excel(file_path, engine="openpyxl", header=None, dtype=str)
    except Exception as error:
        logger.error(
            "extractor_excel_csv._read_excel_file: "
            "Failed to read Excel file '%s': %s",
            file_path, error,
        )
        return None, {}

    if raw is None or raw.empty:
        logger.warning(
            "extractor_excel_csv._read_excel_file: '%s' has no rows.",
            Path(file_path).name,
        )
        return None, {}

    all_rows = raw.values.tolist()
    header_idx = _detect_header_index(all_rows)

    # Identity printed above the table (Account Number, IFSC, Holder, …), if any.
    metadata = _parse_metadata_block(all_rows[:header_idx])

    header = [
        str(h).strip() if not _is_blank(h) else f"col_{i}"
        for i, h in enumerate(all_rows[header_idx])
    ]
    data = raw.iloc[header_idx + 1:].copy()
    data.columns = header
    data = data.dropna(how="all").reset_index(drop=True)

    if header_idx:
        logger.info(
            "extractor_excel_csv._read_excel_file: "
            "skipped %d leading metadata row(s) before the header in '%s'.",
            header_idx, Path(file_path).name,
        )
    logger.info(
        "extractor_excel_csv._read_excel_file: "
        "Read Excel file with %d rows. Columns: %s",
        len(data), data.columns.tolist(),
    )
    return data, metadata


def _read_csv_file(file_path: str) -> Tuple[Optional[pd.DataFrame], Dict[str, str]]:
    """
    Reads a CSV file, trying multiple text encodings until one succeeds.

    Indian bank software sometimes produces CSV files with Windows-specific
    character encodings (cp1252 or latin-1) rather than the standard UTF-8.
    Leading metadata / comment lines above the table are detected and skipped,
    and any account identity in them is parsed out.

    Returns:
        (DataFrame or None, metadata dict). None DataFrame if all encodings fail.
    """
    # List of encodings to try, in order of preference.
    # utf-8 is the modern standard.
    # latin-1 and cp1252 are common in older Windows banking systems.
    encodings_to_try = ["utf-8-sig", "utf-8", "latin-1", "cp1252"]

    for encoding in encodings_to_try:
        # ── Read the raw text once so we can find the real header row ─────────
        try:
            with open(file_path, encoding=encoding, newline="") as f:
                content = f.read()
        except UnicodeDecodeError:
            logger.debug(
                "extractor_excel_csv._read_csv_file: "
                "Encoding '%s' failed for '%s', trying next encoding.",
                encoding, file_path,
            )
            continue
        except Exception as error:
            logger.error(
                "extractor_excel_csv._read_csv_file: "
                "Could not open CSV '%s' with encoding '%s': %s",
                file_path, encoding, error,
            )
            return None, {}

        # ── Find where the transaction table actually starts ─────────────────
        # csv.reader correctly counts fields even when a value is quoted and
        # contains a comma, so the field-count detection is reliable.
        try:
            rows = list(csv.reader(io.StringIO(content)))
        except Exception as error:
            logger.error(
                "extractor_excel_csv._read_csv_file: "
                "csv parse failed for '%s': %s", file_path, error,
            )
            return None, {}

        skiprows = _detect_header_index(rows)
        metadata = _parse_metadata_block(rows[:skiprows])
        if skiprows:
            logger.info(
                "extractor_excel_csv._read_csv_file: "
                "skipping %d leading metadata line(s) before the header in '%s'.",
                skiprows, Path(file_path).name,
            )

        # ── Parse from the detected header row ───────────────────────────────
        # engine="python" is more tolerant of irregular real-world bank CSVs;
        # on_bad_lines="warn" surfaces (never silently drops) a malformed row.
        try:
            df = pd.read_csv(
                file_path,
                encoding=encoding,
                skiprows=skiprows,
                engine="python",
                skip_blank_lines=True,
                on_bad_lines="warn",
            )
            logger.info(
                "extractor_excel_csv._read_csv_file: "
                "Read CSV with encoding '%s'. %d rows, columns: %s",
                encoding, len(df), df.columns.tolist(),
            )
            return df, metadata
        except Exception as error:
            logger.error(
                "extractor_excel_csv._read_csv_file: "
                "Failed to parse CSV '%s' (encoding '%s', skiprows %d): %s",
                file_path, encoding, skiprows, error,
            )
            return None, {}

    # If all encodings failed, log a clear error message.
    logger.error(
        "extractor_excel_csv._read_csv_file: "
        "All encoding attempts failed for CSV file '%s'. Tried: %s",
        file_path, encodings_to_try,
    )
    return None, {}
