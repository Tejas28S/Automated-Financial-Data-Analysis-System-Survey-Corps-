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
                       "acct number", "acct no", "account #",
                       "cod acct no", "cod account no", "acct number"],
    "account_holder": ["account holder", "account name", "account title",
                       "customer name", "holder name", "a/c holder",
                       "name of account holder", "primary holder",
                       # Core-banking KYC mnemonics for the customer's full name.
                       "nam cust full", "nam acct holder", "nam cust", "cust name",
                       "name cust", "name of customer", "name"],
    "ifsc_code": ["ifsc code", "ifsc", "ifs code", "rtgs/neft ifsc", "cod ifsc"],
    "branch": ["branch name", "branch address", "branch"],
    "branch_code": ["branch code", "branch sol id", "sol id", "branch id"],
    "account_type": ["account type", "a/c type", "type of account", "scheme",
                     "product", "product type"],
    "bank_name": ["bank name", "bank"],
    "currency": ["currency"],
    "customer_id": ["customer id", "cif no", "cif number", "cif", "customer number"],
    "micr_code": ["micr code", "micr"],
    "nominee_name": ["nominee name", "nominee", "name of nominee"],
    "joint_holder": ["joint holder", "joint holders", "second holder"],
    "branch_email": ["branch email", "branch e-mail", "email id", "e-mail"],
    "branch_phone": ["branch phone", "branch contact", "mobile number",
                     "regd. mobile number", "phone"],
    "opening_balance": ["opening balance", "brought forward"],
    "closing_balance": ["closing balance", "carried forward", "available balance"],
    "statement_period": ["statement period", "period"],
}

# Cells that are pure separators between a label and its value (some exports put the
# ":" in its own column, so the value is two cells to the right of the label).
_SEPARATOR_CELLS = {":", "-", "=", "::", ":-"}

# ── Column-header keyword map (header label → standard field), priority order ───
# Used to map the transaction table's columns DETERMINISTICALLY from their header
# names — no LLM needed for a normal Excel/CSV with a header row. Each tuple is
# (standard_field, [keyword spellings, most specific first]).
_COL_KEYWORDS = [
    ("date", ["transaction date", "txn date", "value date", "posting date",
              "tran date", "tran_date", "post date",
              # Core-banking mnemonics (Finacle/FLEXCUBE): DAT_TXN_PROCESSING,
              # DAT_TXN_VALUE, DAT_TXN_POSTING, DAT_VALUE. Generic vocabulary, not a
              # bank name — matched as substrings so all the dated variants resolve.
              "dat_txn", "dat txn", "dat_value", "dat value", "dat_post", "dat post",
              "dat_tran", "date"]),
    ("narration", ["narration", "description", "particulars", "particular",
                   "remarks", "transaction details", "details",
                   "txt_tran_particular", "txt_txn_desc", "txt_txn_narrative"]),
    ("balance", ["closing balance", "running balance", "available balance",
                 "ledger balance", "bal_running", "amt_running_bal", "balance", "bal"]),
    # "dr_amt"/"cr_amt" (and the spaced forms) are common debit/credit column names
    # in core-banking exports; without them the columns went unmapped and every
    # amount came out zero. Listed before the bare "dr"/"cr" which are skipped on
    # substring matching (too short) but still used for an EXACT header match.
    ("debit", ["withdrawal amt", "withdrawals", "withdrawal", "debit amount",
               "dr_amt", "dr amt", "debit", "w/drl", "dr amount", "dr"]),
    ("credit", ["deposit amt", "deposits", "deposit", "credit amount",
                "cr_amt", "cr amt", "credit", "cr amount", "cr"]),
    # A SINGLE amount column paired with a separate Dr/Cr direction flag — the common
    # core-banking layout (AMT_TXN_LCY + COD_DRCR). The standardiser splits the amount
    # into Debit/Credit using the flag. Listed AFTER debit/credit so a statement with
    # real separate debit/credit columns is unaffected.
    ("amount", ["amt_txn_lcy", "amt_txn", "txn amount", "transaction amount",
                "amt_lcy", "amount (lcy)", "amount"]),
    ("drcr_flag", ["cod_drcr", "drcr_flag", "dr_cr_flag", "drcr", "dr_cr", "dr cr",
                   "cr_dr", "crdr"]),
    ("cheque_number", ["cheque number", "cheque no", "chq no", "chqno",
                       "instrument no", "chq"]),
    ("reference_number", ["chq/ref no", "reference no", "reference number",
                          "reference", "ref no", "utr", "rrn", "ref"]),
]


# ── Identity COLUMN map (header name → identity field) ──────────────────────────
# Some core-banking exports carry the account identity as COLUMNS repeated on every
# transaction row (e.g. "Ac_No | AC_Name | Tran_Date | …" where every row repeats
# the same account number and holder), instead of a key:value block above the table.
# Each entry is (identity_field, [regexes matched against the normalised header]).
# Generic banking vocabulary only — never a bank name — so the anti-overfitting guard
# is unaffected and this generalises to any export that labels its identity columns.
_IDENTITY_COL_MAP = [
    ("account_number", [r"^a/?c\s*no$", r"^ac_?no$", r"^acct\s*(?:no|number)$",
                        r"^account\s*(?:no|num|number)$", r"\baccount\s*number\b",
                        r"^account$"]),
    ("account_holder", [r"^ac_?name$", r"^a/?c\s*name$", r"^acct\s*name$",
                        r"^account\s*(?:name|holder|title)$", r"\baccount\s*holder\b",
                        r"^customer\s*name$", r"^holder\s*name$", r"^name$"]),
    ("ifsc_code", [r"^ifsc(?:\s*code)?$", r"^ifs\s*code$"]),
    ("customer_id", [r"^cif(?:\s*(?:no|number|id))?$", r"^customer\s*(?:id|no|number)$",
                     r"^cust\s*id$"]),
    ("bank_name", [r"^bank(?:\s*name)?$"]),
    ("branch", [r"^branch(?:\s*name)?$"]),
    ("micr_code", [r"^micr(?:\s*code)?$"]),
    ("account_type", [r"^ac(?:count)?\s*type$", r"^scheme$", r"^product(?:\s*type)?$"]),
]

# Identity fields whose value has a fixed shape we can sanity-check before trusting a
# column's dominant value (so a mis-named column cannot inject a bad identity).
_ACCNO_SHAPE = re.compile(r"^[0-9Xx]{6,20}$")
_IFSC_SHAPE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")


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

    # ── Primary: locate the header by COLUMN-KEYWORD content ──────────────────
    # The real transaction header names recognisable columns (a date column plus at
    # least one of debit/credit/balance/narration). A key:value metadata row such as
    # "Intrest Rate | 2.50 % p.a. | Email | Not Available" maps to NONE of these, so
    # it can no longer be mistaken for the header (the bug that produced 0 rows on
    # statements with a wide 2-panel metadata block above the table). We pick the
    # all-text row with the most mapped fields that is immediately followed (after
    # any blank rows) by a data row.
    best_idx, best_score = None, 0
    for i, r in enumerate(rows):
        if not _row_is_all_text(r):
            continue
        cmap = _infer_column_map([str(c) for c in r if not _is_blank(c)])
        core = {f for f in cmap if f in ("date", "narration", "debit", "credit", "balance")}
        # A genuine header has a date column plus at least one money/narration column.
        if "date" not in core or len(core) < 2:
            continue
        j = i + 1
        while j < len(rows) and all(_is_blank(c) for c in rows[j]):
            j += 1
        if j < len(rows) and _row_has_data(rows[j]) and len(core) > best_score:
            best_idx, best_score = i, len(core)
    if best_idx is not None:
        return best_idx

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


def _sniff_delimiter(content: str) -> str:
    """
    Detects the field delimiter of a CSV-like file (comma, tab, semicolon, pipe).

    Bank "CSV" exports are not always comma-separated — some are TAB-delimited. We
    let csv.Sniffer inspect a sample; if it is inconclusive we pick, among the
    common delimiters, the one that yields the most consistent column count across
    the first several non-empty lines. Falls back to comma.
    """
    sample = content[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
        if dialect.delimiter in ",\t;|":
            return dialect.delimiter
    except (csv.Error, Exception):
        pass

    # Manual fallback: score each candidate by how many lines share the modal width.
    lines = [ln for ln in sample.splitlines() if ln.strip()][:15]
    best, best_score = ",", -1
    for cand in (",", "\t", ";", "|"):
        counts = [ln.count(cand) for ln in lines if ln.count(cand) > 0]
        if not counts:
            continue
        modal = Counter(counts).most_common(1)[0]
        score = modal[1] * modal[0]  # consistency × number of fields
        if score > best_score:
            best, best_score = cand, score
    return best


def _norm_label(s) -> str:
    """
    Normalises a metadata label for matching: lower-case, underscores → spaces,
    collapse whitespace, drop a trailing colon. The underscore step lets a label
    printed as "ACCOUNT_NAME" match the "account name" vocabulary.
    """
    t = str(s).strip().lower().replace("_", " ")
    return re.sub(r"\s+", " ", t).rstrip(":").strip()


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

    def _match_field(label: str):
        """Returns the standard field a normalised label maps to, or None.
        Prefers an EXACT variant match over a startswith match, and the LONGEST
        variant, so "account number" is not shadowed by the generic "name"."""
        best_field = None
        best_len = -1
        for field, variants in _META_LABELS.items():
            for v in variants:
                if label == v or label.startswith(v + " ") or label.startswith(v):
                    if len(v) > best_len:
                        best_field, best_len = field, len(v)
        return best_field

    def _record(label_raw: str, value: str):
        nonlocal period_from, period_to
        label = _norm_label(label_raw)
        value = str(value).strip().lstrip(":").strip()
        if not label or not value or value in _SEPARATOR_CELLS:
            return
        if label.startswith("period from") or label == "from":
            period_from = value
            return
        if label.startswith("period to") or label == "to":
            period_to = value
            return
        field = _match_field(label)
        if field:
            # For the holder, prefer the MOST COMPLETE value: a core-banking kyc dump
            # has both a first-name column ("Pushpendra") and a full-name column
            # ("Pushpendra Pathak"); keep the longer. Other fields keep first-wins.
            if field == "account_holder":
                if field not in details or len(value) > len(details[field]):
                    details[field] = value
            else:
                details.setdefault(field, value)

    for r in rows:
        cells = [str(c).strip() for c in r if not _is_blank(c)]
        if not cells:
            continue

        # Single cell carrying "Label: value" (or "# Label: value" comment line).
        if len(cells) == 1:
            text = cells[0].lstrip("#").strip()
            if ":" in text:
                lbl, _, val = text.partition(":")
                _record(lbl, val)
            continue

        # Multi-cell row. Banks lay metadata out in several ways on ONE row:
        #   "Label", "value"                     (2 cells)
        #   "Label", ":", "value"                (separator in its own cell)
        #   "Label", ":", "value", "Label2", ":", "value2"   (two side-by-side panels)
        # We WALK the cells: at each cell that is a known label, the value is the next
        # non-separator cell. This recovers every label:value pair on the row and
        # never mistakes a stray ":" cell for the value.
        i = 0
        n = len(cells)
        matched_any = False
        while i < n:
            label = _norm_label(cells[i])
            if _match_field(label) or label in ("period from", "period to", "from", "to"):
                j = i + 1
                while j < n and cells[j].strip() in _SEPARATOR_CELLS:
                    j += 1
                if j < n and _norm_label(cells[j]) and not _match_field(_norm_label(cells[j])):
                    _record(cells[i], cells[j])
                    matched_any = True
                    i = j + 1
                    continue
            i += 1
        # Fallback for a simple 2-cell row where the first cell wasn't a known label
        # but the layout is clearly "label, value" (keeps prior behaviour).
        if not matched_any and n >= 2 and cells[1].strip() not in _SEPARATOR_CELLS:
            _record(cells[0], cells[1])

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


def _dominant_value(values) -> str:
    """
    Returns the single value that DOMINATES a column (the account-identity value
    repeated on most rows), or "" if no value is clearly dominant. An identity column
    (account number, holder name) repeats ONE value on every transaction row; a
    counterparty column has many distinct values and is correctly rejected here.
    """
    vals = [str(v).strip() for v in values if not _is_blank(v)]
    if not vals:
        return ""
    top, count = Counter(vals).most_common(1)[0]
    return top if count >= max(1, 0.5 * len(vals)) else ""


def _identity_from_columns(df: pd.DataFrame, used_cols: set) -> Dict[str, str]:
    """
    Recovers account identity carried as COLUMNS repeated on every transaction row
    (e.g. "Ac_No", "AC_Name", "IFSC", "CIF"). For each identity field we find a
    matching column (by its header name, never used as a transaction column), take
    its DOMINANT value, and shape-check the value where the field has a fixed format.
    Returns only the fields actually found. Bank-agnostic and value-validated, so a
    mislabelled or noisy column never injects a wrong identity.
    """
    found: Dict[str, str] = {}
    cols = [c for c in df.columns if str(c) not in {"Account_ID", "Bank_Name"}]
    used = {str(c) for c in (used_cols or set())}
    # Normalise headers for matching: lowercase, underscores→spaces, collapse runs,
    # AND strip trailing label punctuation ("ACCOUNT NO." → "account no", "IFSC:" →
    # "ifsc") so a header that merely ends in a period/colon still matches the
    # $-anchored vocabulary patterns. Generic — no bank- or file-specific text.
    norm = {c: re.sub(r"[\s.:;,#*]+$", "",
                      re.sub(r"\s+", " ", str(c).strip().lower().replace("_", " ")))
            for c in cols}
    for field, patterns in _IDENTITY_COL_MAP:
        for c in cols:
            if str(c) in used:
                continue
            if not any(re.search(p, norm[c]) for p in patterns):
                continue
            value = _dominant_value(df[c].tolist())
            if not value:
                continue
            if field == "account_number" and not _ACCNO_SHAPE.match(value.replace(" ", "")):
                continue
            if field == "ifsc_code" and not _IFSC_SHAPE.match(value.upper()):
                continue
            if field == "account_holder" and (_is_numeric(value) or _DATE_LIKE.match(value)):
                continue
            found[field] = value.upper() if field == "ifsc_code" else value
            break
    return found


def _merge_metadata(*sources: Dict[str, str]) -> Dict[str, str]:
    """Merges metadata dicts left-to-right: the first non-blank value for a field wins."""
    out: Dict[str, str] = {}
    for src in sources:
        for k, v in (src or {}).items():
            if k not in out and not _is_blank(v):
                out[k] = str(v).strip()
    return out


def _clean_identity_fields(meta: Dict[str, str]) -> Dict[str, str]:
    """
    Normalises fixed-shape identity values so trailing junk a single-cell label:value
    row may have swept up (e.g. "IFSC: HDFC0282243 Branch: INDIRANAGAR") never reaches
    the output. Extracts the canonical IFSC token and the leading account-number run.
    """
    if meta.get("ifsc_code"):
        m = re.search(r"[A-Z]{4}0[A-Z0-9]{6}", str(meta["ifsc_code"]).upper())
        if m:
            meta["ifsc_code"] = m.group(0)
    if meta.get("account_number"):
        m = re.match(r"^([0-9Xx]{6,20})", str(meta["account_number"]).strip())
        if m:
            meta["account_number"] = m.group(1)
    # Strip leading separator punctuation a "Name :- VALUE" (colon-dash) label leaves on
    # the holder, and collapse inner whitespace. A real name starts with a letter (or a
    # company "M/S"), so this only removes junk.
    if meta.get("account_holder"):
        meta["account_holder"] = re.sub(
            r"^[\s\-:=.,/]+", "", re.sub(r"\s{2,}", " ", str(meta["account_holder"]))).strip()
    return meta


def _enrich_metadata(metadata: Dict[str, str], df: pd.DataFrame,
                     meta_text: str, used_cols: set) -> Dict[str, str]:
    """
    Multi-SOURCE metadata recovery for a structured sheet, in reliability order:
      1. the key:value block / extra sheets already parsed (`metadata`),
      2. identity carried as repeated COLUMNS in the table,
      3. the TEXT heuristics shared with the PDF path (unlabelled holder lines,
         IFSC-by-shape, bank-by-IFSC-prefix or keyword) run over the sheet's
         metadata region + sheet titles.
    Each source only FILLS fields the earlier ones left blank, so a confident
    deterministic value is never overwritten by a weaker guess. This is what gives
    Excel/CSV the same identity coverage the PDF path already has, without
    duplicating any of that logic.
    """
    col_ident = _identity_from_columns(df, used_cols)
    text_ident: Dict[str, str] = {}
    if meta_text and meta_text.strip():
        try:
            # Reuse the PDF path's hardened identity reader (holder/IFSC/bank/branch…).
            from extraction.account_extractor import extract_account_details_from_text
            text_ident = {k: v for k, v in extract_account_details_from_text(meta_text).items()
                          if not _is_blank(v)}
        except Exception as e:  # never let metadata enrichment break extraction
            logger.debug("extractor_excel_csv._enrich_metadata: text reader failed: %s", e)
    return _clean_identity_fields(_merge_metadata(metadata, col_ident, text_ident))


def _median_text_len(series) -> float:
    """Median length of the non-blank string values in a column (0 if none)."""
    vals = [len(str(v).strip()) for v in series if not _is_blank(v)]
    if not vals:
        return 0.0
    vals.sort()
    return float(vals[len(vals) // 2])


def _refine_narration_column(df: pd.DataFrame, inferred_map: Dict[str, str]) -> None:
    """
    Value-aware narration upgrade (in place). A header NAME can be misleading: a
    core-banking export may have a "TXT_TRAN_PARTICULAR" column (matched as narration
    by the word "particular") that actually holds a one-character code ("P"), while the
    real description lives in "TXT_TXN_DESC". When the name-matched narration column is
    DEGENERATE (very short values), we replace it with the most descriptive unused text
    column. Gated on degeneracy, so a normal statement whose narration is already
    descriptive is never disturbed. Generalised by value shape, never by a bank name.
    """
    current = inferred_map.get("narration")
    cur_score = _median_text_len(df[current]) if current in df.columns else 0.0
    if cur_score >= 5:           # already a real description → leave it
        return
    used = {inferred_map.get(k) for k in
            ("date", "amount", "balance", "debit", "credit", "drcr_flag",
             "reference_number", "cheque_number")}
    used.discard(None)
    best, best_score = current, cur_score
    for col in df.columns:
        if col in used or col == current or str(col) in ("Account_ID", "Bank_Name"):
            continue
        vals = [str(v).strip() for v in df[col] if not _is_blank(v)]
        if not vals:
            continue
        numeric_frac = sum(1 for v in vals if _is_numeric(v) or _DATE_LIKE.match(v)) / len(vals)
        if numeric_frac > 0.5:    # a numeric/date column is not a narration
            continue
        score = _median_text_len(df[col])
        if score > best_score and score >= 8:
            best, best_score = col, score
    if best != current and best is not None:
        inferred_map["narration"] = best


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
    meta_text: str = ""

    # ── Excel files (.xlsx or .xls) ───────────────────────────────────────────
    if extension in (".xlsx", ".xls"):
        df, metadata, meta_text = _read_excel_file(file_path)

    # ── CSV files (.csv) ──────────────────────────────────────────────────────
    elif extension == ".csv":
        df, metadata, meta_text = _read_csv_file(file_path)

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
    # Upgrade a degenerate narration column (a misleading "particular" header that
    # holds a one-char code) to the most descriptive text column — value-aware.
    _refine_narration_column(df, inferred_map)

    # ── Multi-source metadata recovery ────────────────────────────────────────
    # Beyond the key:value block, recover identity carried as repeated COLUMNS and
    # via the shared text heuristics (unlabelled holder, IFSC-by-shape, bank-by-IFSC).
    # `used_cols` are the transaction columns, never mistaken for identity columns.
    used_cols = set(inferred_map.values())
    metadata = _enrich_metadata(metadata, df, meta_text, used_cols)

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


def _rows_to_text(rows: List[list]) -> str:
    """Joins sheet rows into newline-delimited text for the shared text identity reader."""
    lines = []
    for r in rows:
        cells = [str(c).strip() for c in r if not _is_blank(c)]
        if cells:
            lines.append(" ".join(cells))
    return "\n".join(lines)


def _read_excel_file(file_path: str) -> Tuple[Optional[pd.DataFrame], Dict[str, str], str]:
    """
    Reads an Excel file (.xlsx or .xls), inspecting EVERY worksheet.

    Many bank exports split the workbook: one sheet holds the transaction table and
    a separate sheet ("Account Info", "Summary", "Details") holds the account
    identity and the opening/closing balances as key:value rows. Reading only the
    first sheet (the old behaviour) silently dropped all of that identity. We now:
      • read every sheet,
      • pick the TRANSACTION sheet as the one whose detected table has the most data
        rows (robust to a metadata sheet appearing first),
      • parse identity from the transaction sheet's own above-table block AND from
        every OTHER sheet (which are pure key:value metadata),
      • return the metadata region as text too, so the shared text reader can recover
        an unlabelled holder / bank name that no key:value rule catches.

    Returns (DataFrame or None, metadata dict, metadata_text).
    """
    # Choose the engine by FORMAT: openpyxl reads only the modern .xlsx zip format;
    # a genuine binary .xls (OLE2, magic bytes D0 CF 11 E0) needs xlrd. Reading every
    # file with openpyxl silently produced 0 rows for every .xls in the dataset. We try
    # the format-appropriate engine first, then the other as a fallback (some files are
    # mis-extensioned), and on total failure return empty with a CLEAR error so the file
    # shows up as a visible FAILED rather than a silent empty result.
    ext = Path(file_path).suffix.lower()
    engines = ["xlrd", "openpyxl"] if ext == ".xls" else ["openpyxl", "xlrd"]
    sheets = None
    last_err = None
    for eng in engines:
        try:
            sheets = pd.read_excel(file_path, engine=eng, sheet_name=None,
                                   header=None, dtype=str)
            break
        except Exception as error:
            last_err = error
    if sheets is None:
        logger.error(
            "extractor_excel_csv._read_excel_file: Failed to read Excel file '%s' with "
            "engines %s: %s. (For .xls support ensure 'xlrd>=2.0.1' is installed.)",
            file_path, engines, last_err,
        )
        return None, {}, ""

    sheets = {name: raw for name, raw in (sheets or {}).items()
              if raw is not None and not raw.empty}
    if not sheets:
        logger.warning(
            "extractor_excel_csv._read_excel_file: '%s' has no rows.",
            Path(file_path).name,
        )
        return None, {}, ""

    # Pick the transaction sheet = the one whose detected table has the most data rows.
    best_name = None
    best_rows = None
    best_hidx = 0
    best_count = -1
    sheet_rows = {name: raw.values.tolist() for name, raw in sheets.items()}
    for name, all_rows in sheet_rows.items():
        hidx = _detect_header_index(all_rows)
        ndata = sum(1 for r in all_rows[hidx + 1:] if _row_has_data(r))
        if ndata > best_count:
            best_count, best_name, best_rows, best_hidx = ndata, name, all_rows, hidx

    # Identity: the transaction sheet's above-table block + EVERY other sheet (an
    # "Account Info" / "Summary" / "kyc" sheet). A sheet can carry metadata two ways:
    #   • key:value ROWS  ("Account Holder | Smitha Pillai")          → use as-is.
    #   • a wide HEADER + a few DATA rows (a column-oriented kyc dump) → TRANSPOSE it
    #     into (header, value) pairs, otherwise the block parser mis-pairs two adjacent
    #     HEADER cells ("IFSC" → "COD_PROD") instead of header → value.
    meta_rows = list(best_rows[:best_hidx])
    for name, all_rows in sheet_rows.items():
        if name == best_name:
            continue
        nonblank = [r for r in all_rows if any(not _is_blank(c) for c in r)]
        is_wide_short = (2 <= len(nonblank) <= 4 and len(nonblank[0]) > 4
                         and _row_is_all_text(nonblank[0]))
        if is_wide_short:
            hdr = nonblank[0]
            for data_row in nonblank[1:]:
                for h, v in zip(hdr, data_row):
                    if not _is_blank(h) and not _is_blank(v):
                        meta_rows.append([h, v])  # header : its own value (transposed)
        else:
            meta_rows += all_rows
    metadata = _parse_metadata_block(meta_rows)
    # Text region for the shared reader: sheet titles + all metadata rows.
    meta_text = "\n".join([" ".join(n for n in sheets if n != best_name and not n.startswith("Sheet")),
                           _rows_to_text(meta_rows)]).strip()

    header = [
        str(h).strip() if not _is_blank(h) else f"col_{i}"
        for i, h in enumerate(best_rows[best_hidx])
    ]
    data = sheets[best_name].iloc[best_hidx + 1:].copy()
    data.columns = header
    data = data.dropna(how="all").reset_index(drop=True)

    logger.info(
        "extractor_excel_csv._read_excel_file: '%s' → transaction sheet '%s' "
        "(%d rows, header at %d); metadata fields=%s",
        Path(file_path).name, best_name, len(data), best_hidx, sorted(metadata.keys()),
    )
    return data, metadata, meta_text


def _read_csv_file(file_path: str) -> Tuple[Optional[pd.DataFrame], Dict[str, str], str]:
    """
    Reads a CSV file, trying multiple text encodings until one succeeds.

    Indian bank software sometimes produces CSV files with Windows-specific
    character encodings (cp1252 or latin-1) rather than the standard UTF-8.
    Leading metadata / comment lines above the table are detected and skipped,
    and any account identity in them is parsed out.

    Returns:
        (DataFrame or None, metadata dict, metadata_text). None DataFrame if all
        encodings fail.
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
            return None, {}, ""

        # ── Detect the field delimiter ───────────────────────────────────────
        # Not every "CSV" is comma-separated — Indian bank exports also use TAB or
        # semicolon/pipe. csv.Sniffer inspects a sample; we fall back to comma. The
        # detected delimiter is used for BOTH the header scan and the final parse,
        # otherwise a tab-delimited file collapses into one garbled column.
        delimiter = _sniff_delimiter(content)

        # ── Find where the transaction table actually starts ─────────────────
        # csv.reader correctly counts fields even when a value is quoted and
        # contains the delimiter, so the field-count detection is reliable.
        try:
            rows = list(csv.reader(io.StringIO(content), delimiter=delimiter))
        except Exception as error:
            logger.error(
                "extractor_excel_csv._read_csv_file: "
                "csv parse failed for '%s': %s", file_path, error,
            )
            return None, {}, ""

        skiprows = _detect_header_index(rows)
        metadata = _parse_metadata_block(rows[:skiprows])
        meta_text = _rows_to_text(rows[:skiprows])
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
                sep=delimiter,
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
            return df, metadata, meta_text
        except Exception as error:
            logger.error(
                "extractor_excel_csv._read_csv_file: "
                "Failed to parse CSV '%s' (encoding '%s', skiprows %d): %s",
                file_path, encoding, skiprows, error,
            )
            return None, {}, ""

    # If all encodings failed, log a clear error message.
    logger.error(
        "extractor_excel_csv._read_csv_file: "
        "All encoding attempts failed for CSV file '%s'. Tried: %s",
        file_path, encodings_to_try,
    )
    return None, {}, ""
