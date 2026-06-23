"""
standardiser.py — Converts raw extracted content into the unified standard DataFrame.

Every bank formats their statements differently. Some call a column "Withdrawal Amt",
others call it "Debit", "W/Drl", or "Dr Amount". Some PDFs have two date columns
(Transaction Date and Value Date). Some banks show Debit and Credit as separate columns,
others show a single Amount column with the direction inferred from context.

The standardiser's job is to take the messy, bank-specific raw data and produce
one clean, consistent DataFrame with exactly these columns every time:

    Date | Narration | Debit | Credit | Balance | Account_ID | Bank_Name

TWO INPUT PATHS:
    1. TEXT-BASED (from digital PDFs, scanned PDFs, DOCX, OCR):
       The raw text is parsed line-by-line. A date-anchored regex approach extracts
       the transaction date, narration, amount, and balance from each line.
       The debit/credit split is determined by narration keywords (UPI/DR, UPI/CR)
       or balance change direction.

    2. DATAFRAME-BASED (from Excel/CSV files):
       The DataFrame already has labelled columns. The column_map from Groq tells us
       which column header corresponds to which standard field. We rename and reformat.

This module contains no API calls — it is pure Python and pandas. All the
"smart" column identification work was already done by column_identifier.py.

Team: Survey Corps | CIDECODE Hackathon 2026 | CID Karnataka
"""

import logging
import re
from io import StringIO
from typing import Dict, List, Optional, Any

import pandas as pd
import numpy as np

from config.settings import STANDARD_COLUMNS, REFERENCE_COLUMNS, SCHEMA_SAMPLE_LINES

# Set up a logger for this module.
logger = logging.getLogger(__name__)

# ── Date format patterns to try ───────────────────────────────────────────────
# Indian bank statements use many different date formats.
# We try these in order until one successfully parses the date string.
DATE_FORMATS = [
    "%d/%m/%Y",   # 01/08/2022 — most common Indian format
    "%d-%m-%Y",   # 01-08-2022
    "%m/%d/%Y",   # 08/01/2022 — US format (some multinationals)
    "%Y-%m-%d",   # 2022-08-01 — ISO format
    "%d %b %Y",   # 01 Aug 2022
    "%d-%b-%y",   # 01-Aug-22 (two-digit year)
    "%d-%b-%Y",   # 01-Aug-2022
    "%d %B %Y",   # 01 August 2022
]

# ── Regex patterns for parsing transaction text lines ─────────────────────────

# Pattern to detect a date at the START of a line (the signal that a line begins a
# new transaction). Kept deliberately broad so it works across banks, not just the
# mentoring format. Matches, e.g.:
#   02/06/18, 02-06-2018, 2018-06-02   (numeric, 2- or 4-digit year)
#   3 May 2018, 16-Oct-2018, 16 Oct 18 (month-name styles)
DATE_AT_LINE_START_PATTERN = re.compile(
    r"^\s*(?:\d{1,4}\s+)?("                       # optional leading serial no. (IDBI: "1 31/03/2021 …")
    r"\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}"          # 02/06/18, 01-04-2019
    r"|\d{4}[/\-]\d{1,2}[/\-]\d{1,2}"            # 2018-06-02
    r"|\d{1,2}[ \-][A-Za-z]{3,9}[ \-]\d{2,4}"    # 3 May 2018, 16-Oct-2018
    r")"
)

# A "money" token: digits with an explicit 2-decimal part (optionally Indian commas),
# and an OPTIONAL trailing Dr/Cr marker which some banks (e.g. Kotak) glue onto the
# amount: "2,500.00(Dr)", "34,592.10(Cr)". Requiring the decimals separates real
# amounts/balances from reference/cheque numbers ("38976288").
_DRCR_SUFFIX = r"(?:\s*\(?(?:Dr|Cr)\)?)?"
# The optional leading [-₹] lets us recognise a NEGATIVE amount/balance such as
# "-34,885.00" or "-0.00" — overdraft / cheque-return rows print these, and without
# the minus they were not seen as money tokens, so the whole row was dropped and the
# balance chain broke (pushing an otherwise-parseable statement onto the LLM path).
_MONEY_TOKEN = re.compile(
    rf"^[-₹]*\d{{1,3}}(?:,\d{{2,3}})*\.\d{{1,2}}{_DRCR_SUFFIX}$|^[-₹]*\d+\.\d{{1,2}}{_DRCR_SUFFIX}$",
    re.IGNORECASE,
)
# Pull the Dr/Cr direction out of an amount token, if present.
_DRCR_FIND = re.compile(r"\(?(Dr|Cr)\)?\s*$", re.IGNORECASE)

# A date appearing ANYWHERE on a line (not only at the start). This is used ONLY
# to estimate how many lines LOOK like transaction rows, so the pipeline can tell
# when the deterministic digital-PDF parser has under-extracted. It is never used
# to parse a value and it encodes no specific bank's layout.
_DATE_ANYWHERE_PATTERN = re.compile(
    r"\d{1,2}[/\-.][0-9A-Za-z]{2,9}[/\-.]\d{2,4}"   # 02/06/18, 16-Oct-2018, 02.06.2018
    r"|\d{4}[/\-.]\d{1,2}[/\-.]\d{1,2}"             # 2018-06-02
)

# A continuation line that is JUST a transaction time. Some banks (e.g. Canara) wrap
# the time onto its own line; we record it as the row's Time instead of letting it
# pollute the narration. e.g. "16:23:06", "08:04:44 PM".
_TIME_ONLY_LINE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AaPp][Mm])?$")

# Page furniture that is never a transaction: standalone page numbers / "Page X of Y".
_PAGE_NOISE = re.compile(
    r"^(?:page\s+\d+\s+of\s+\d+|p\.?\s*\d+\s*/\s*\d+|\d+\s*/\s*\d+|"
    r"(?:statement\s+)?continued.*)$",
    re.IGNORECASE,
)
# An embedded "Page X of Y" fragment to scrub out of a narration if it slips in.
_PAGE_FRAGMENT = re.compile(r"\s*page\s+\d+\s+of\s+\d+\s*", re.IGNORECASE)

# Standard column-header vocabulary (generic banking terms — NOT any bank's name, so
# the anti-overfitting guard is unaffected). A line carrying several of these and NO
# money amount is a repeated column-header row we should drop before parsing.
_HEADER_WORDS = ("date", "narration", "description", "particulars", "remarks",
                 "debit", "withdrawal", "credit", "deposit", "balance",
                 "cheque", "chq", "ref", "branch", "value", "txn", "transaction")


def _is_money_token(token: str) -> bool:
    """True if the token looks like a monetary amount (has a 2-digit decimal)."""
    return bool(_MONEY_TOKEN.match(token.strip()))


def count_transaction_like_lines(raw_text: str) -> int:
    """
    Estimate of how many lines in the raw text LOOK like transaction rows: a line
    that contains a date somewhere AND at least one monetary amount.

    This is a GENERALIZATION CHECK, not a parser. The deterministic digital-PDF
    parser makes layout assumptions (date at line start, balance = last token,
    2-decimal amounts) that silently produce zero/partial output on an unseen
    layout. When the parser yields far fewer rows than this estimate, the pipeline
    falls back to the LLM structurer, which reads any layout. It is deliberately
    bank-agnostic — it never special-cases a specific statement format.
    """
    n = 0
    for line in (raw_text or "").splitlines():
        s = line.strip()
        if _DATE_ANYWHERE_PATTERN.search(s) and any(_is_money_token(t) for t in s.split()):
            n += 1
    return n


def _is_header_line(s: str) -> bool:
    """True if a line is a repeated column-header row (>=3 header words, no money, no date)."""
    if DATE_AT_LINE_START_PATTERN.match(s):
        return False
    if any(_is_money_token(t) for t in s.split()):
        return False
    low = s.lower()
    return sum(1 for w in _HEADER_WORDS if w in low) >= 3


def _strip_noise_lines(lines: List[str]) -> List[str]:
    """
    Removes page numbers and repeated column-header rows BEFORE parsing.

    Generic, bank-agnostic cleanup: it stops a multi-page statement's repeating
    header/footer from being stitched into a real transaction's narration (which
    corrupts rows and can push an otherwise-parseable statement onto the costly LLM
    path). It only drops lines that carry NO money amount and do not begin with a
    date, so a real transaction row can never be removed.
    """
    out = []
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if _PAGE_NOISE.match(s) or _is_header_line(s):
            continue
        out.append(ln)
    return out


def build_schema_sample(raw_text: str, failing_lines: List[str] = None,
                        max_lines: int = SCHEMA_SAMPLE_LINES) -> str:
    """
    Builds a small, REPRESENTATIVE sample of transaction lines to send to the LLM
    for schema discovery (Tier 4). This is content-based, never bank-based — so it
    encodes no overfitting.

    The first rows of a statement are the LEAST representative (opening balance,
    simple entries), so instead of "first N lines" we deliberately gather:
      • a few header lines (for the date format / layout context),
      • transaction-like lines from the START, MIDDLE and END of the document,
      • the LONGEST transaction lines (most likely to show wrapped narrations or
        reference numbers that break a naive parse),
      • any specific lines the cheap parse already FAILED to reconcile.
    The sample is de-duplicated and capped at `max_lines` so the LLM call stays
    cheap and the laptop stays cool.
    """
    lines = [ln.strip() for ln in (raw_text or "").splitlines() if ln.strip()]
    if not lines:
        return ""

    txn_lines = [
        ln for ln in lines
        if _DATE_ANYWHERE_PATTERN.search(ln) and any(_is_money_token(t) for t in ln.split())
    ]

    chosen: List[str] = list(lines[:3])  # header context
    if txn_lines:
        n = len(txn_lines)
        spread_idx = sorted({0, 1, 2, n // 2, n // 2 + 1, n - 2, n - 1})
        chosen += [txn_lines[i] for i in spread_idx if 0 <= i < n]
        chosen += sorted(txn_lines, key=len, reverse=True)[:3]  # longest lines
    if failing_lines:
        chosen += [str(ln).strip() for ln in failing_lines if str(ln).strip()]

    # De-duplicate preserving order, then cap.
    seen, out = set(), []
    for ln in chosen:
        if ln not in seen:
            seen.add(ln)
            out.append(ln)
        if len(out) >= max_lines:
            break
    return "\n".join(out)


# Maps a human-readable date format (as the LLM returns it in the schema) to a
# Python strptime pattern, so the discovered date_format actually drives parsing
# (it is no longer a "decorative" schema field). Unknown tokens → no pattern.
def _format_to_strptime(date_format: str) -> str:
    """Converts e.g. 'DD-MM-YYYY' → '%d-%m-%Y', 'DD Mon YYYY' → '%d %b %Y'."""
    if not date_format or not isinstance(date_format, str):
        return ""
    patt = date_format.strip()
    # Order matters: replace longer tokens first.
    replacements = [
        ("YYYY", "%Y"), ("YY", "%y"),
        ("MMMM", "%B"), ("MMM", "%b"), ("MONTH", "%B"), ("MON", "%b"),
        ("DD", "%d"), ("MM", "%m"),
    ]
    upper = patt.upper()
    for token, code in replacements:
        upper = upper.replace(token, code)
    # If nothing recognisable was converted, treat as no hint.
    return upper if "%" in upper else ""


def _direction_of(token: str) -> str:
    """Returns 'D' for a (Dr) amount, 'C' for a (Cr) amount, '' if unmarked."""
    m = _DRCR_FIND.search(token.strip())
    if not m:
        return ""
    return "D" if m.group(1).lower() == "dr" else "C"

# Pattern to detect Indian-format numbers (with or without commas and rupee symbol).
# Examples: 1500.00, 1,50,000.00, ₹5,000, 90000
AMOUNT_PATTERN = re.compile(
    r"(?:₹\s*)?(?:\d{1,3}(?:,\d{2})*(?:,\d{3})*|\d+)(?:\.\d{1,2})?"
)

# A stricter pattern for the last few tokens of a line, which should be
# valid monetary amounts (decimal numbers potentially with commas).
DECIMAL_AMOUNT_PATTERN = re.compile(r"^\d{1,3}(?:,\d{2,3})*(?:\.\d{1,2})?$")

# Narration keywords that indicate a DEBIT transaction (money leaving the account)
DEBIT_KEYWORDS = ["/DR/", "WDL", "WITHDRAWAL", "ATM/W", "NEFT/DR", "IMPS/DR", "CHQ"]

# Narration keywords that indicate a CREDIT transaction (money entering the account)
CREDIT_KEYWORDS = ["/CR/", "DEP", "DEPOSIT", "CREDIT", "IMPS/P2P", "NEFT/CR", "REFUND"]


def standardise_transactions(
    raw_text: str,
    column_map: Dict[str, Any],
    account_id: str,
    bank_name: str,
) -> pd.DataFrame:
    """
    Converts raw extracted bank statement text into a standardised
    pandas DataFrame using the column structure identified by Groq.

    This function handles all the messy real-world formatting issues:
        - Dates in different formats (DD/MM/YYYY, MM-DD-YYYY, etc.)
        - Numbers with commas (90,000 → 90000.0)
        - Numbers with currency symbols (₹5,000 → 5000.0)
        - Empty debit or credit fields (represented as - or blank)
        - Multiple spaces between columns
        - Rows that are headers or page totals (detected and removed)
        - Narrations that contain spaces (parsed using end-of-line number extraction)

    For text-based input: uses date-anchored line parsing where each valid
    transaction line starts with a date pattern. The balance is always the
    last number, and the narration is extracted from the middle portion.

    For CSV-format text (comma-separated): parsed directly with pandas,
    then columns are renamed using the column_map.

    Parameters:
        raw_text (str): Full extracted text from any extractor.
        column_map (dict): Column structure from identify_column_structure().
                           e.g. {"date": 0, "narration": 1, "debit": 2,
                                 "credit": 3, "balance": 4}
                           Values can be 0-based integer indices or column
                           header name strings.
        account_id (str): Investigator-provided account identifier (e.g. ACC001).
        bank_name (str): Investigator-provided bank name (e.g. SBI).

    Returns:
        pd.DataFrame: Standardised DataFrame with columns:
                      Date, Narration, Debit, Credit, Balance,
                      Account_ID, Bank_Name

                      Date column is pandas datetime dtype.
                      Debit, Credit, Balance columns are float64.
                      Narration, Account_ID, Bank_Name are string.
    """
    if not raw_text or not raw_text.strip():
        logger.warning(
            "standardiser.standardise_transactions: "
            "Empty text received for account '%s'. Returning empty DataFrame.",
            account_id,
        )
        return _create_empty_standard_dataframe()

    logger.info(
        "standardiser.standardise_transactions: "
        "Standardising transactions for account '%s' at '%s'",
        account_id,
        bank_name,
    )

    # Text from PDFs / DOCX / OCR is ALWAYS parsed with the date-anchored parser.
    # (Excel and CSV go through standardise_dataframe_direct instead, never here.)
    # We must NOT treat this text as CSV just because Indian amounts contain commas
    # like "65,731.31" — doing so was discarding entire statements.
    lines = [line for line in raw_text.splitlines() if line.strip()]
    df = _parse_text_lines(lines, column_map, account_id, bank_name)

    if df.empty:
        logger.warning(
            "standardiser.standardise_transactions: "
            "No valid transactions parsed for account '%s'.",
            account_id,
        )
        return _create_empty_standard_dataframe()

    logger.info(
        "standardiser.standardise_transactions: "
        "Standardised %d transaction rows for account '%s'.",
        len(df),
        account_id,
    )
    return df


# Columns produced by the rich digital-PDF parser (date/time + ref/cheque + type).
RICH_COLUMNS = [
    "Date", "Time", "Narration", "Reference_Number", "Cheque_Number",
    "Debit", "Credit", "Balance", "Transaction_Type", "Account_ID", "Bank_Name",
]


# A cheque/instrument number after a CHQ/CHEQUE/INSTRUMENT keyword, tolerating the
# many separators banks use: "CHQNO 12345", "CHQ NO 12345", "CHEQUE NO 12345",
# "CHEQUE NUMBER 12345", "CHQ#12345", "CHQ.NO. 12345", "INSTR NO 12345". The two
# variants the old regex missed — "CHEQUE NUMBER" and "CHQ#" — are now covered.
_CHEQUE_IN_TEXT = re.compile(
    r"(?:CHEQUE|CHQ|INSTRUMENT|INSTR)\s*\.?\s*(?:NO|NUMBER|#)?\s*[:.#/\-]*\s*(\d{4,8})\b",
    re.IGNORECASE,
)
# An EXPLICITLY-marked transaction reference (Ref No / RRN / UTR / Txn Id). A UTR can
# be alphanumeric, so we allow letters+digits (but require a leading digit so we do
# not grab an ordinary word).
_REF_IN_TEXT = re.compile(
    r"(?:REFERENCE|REF|RRN|UTR|TXN\s*ID|TRANSACTION\s*ID|TXN\s*REF)\s*\.?\s*"
    r"(?:NO|NUMBER|ID|#)?\s*[:.#/\-]*\s*([0-9][0-9A-Z]{5,})\b",
    re.IGNORECASE,
)


def _extract_ref_cheque(narration: str, has_ref: bool, has_cheque: bool) -> tuple:
    """
    Deterministically pulls a cheque number and a reference number out of a
    narration, keeping them in SEPARATE fields (team policy — a cheque number and a
    transaction reference are different things).

    Cheque: digits after a CHQ/CHEQUE/INSTRUMENT keyword (all the common spellings).
    Reference: an explicitly-marked Ref/RRN/UTR/Txn-Id token if present, otherwise
    the longest 6+-digit run that is NOT the cheque number — so a cheque value can
    never silently leak into the reference field (the bug this fixes). Both are gated
    by the schema the LLM discovered (has_cheque / has_ref).
    """
    cheque = ""
    if has_cheque:
        m = _CHEQUE_IN_TEXT.search(narration or "")
        if m:
            cheque = m.group(1)
    ref = ""
    if has_ref:
        m = _REF_IN_TEXT.search(narration or "")
        if m and m.group(1) != cheque:
            ref = m.group(1)
        else:
            runs = [r for r in re.findall(r"\d{6,}", narration or "") if r != cheque]
            if runs:
                ref = max(runs, key=len)
    return ref, cheque


def standardise_digital_pdf_transactions(
    raw_text: str,
    account_id: str,
    bank_name: str,
    opening_balance: str = "",
    schema: Dict[str, Any] = None,
) -> pd.DataFrame:
    """
    Deterministically parses EVERY transaction row of a digital PDF using the schema
    the LLM discovered from a small sample. No per-row LLM calls — this scales to
    thousands of transactions at zero extra API cost.

    Steps (all local):
      1. Find each transaction (a line starting with a date), stitching wrapped
         continuation lines into the narration.
      2. Pull the date, optional time, amounts and balance off each row.
      3. Assign debit/credit (Dr/Cr markers → narration keywords → balance change),
         then correct the direction from the running balance (authoritative).
      4. Extract reference / cheque numbers per the discovered schema.

    Returns a DataFrame with the RICH_COLUMNS schema.
    """
    schema = schema or {}
    lines = [ln for ln in (raw_text or "").splitlines() if ln.strip()]
    # Drop page furniture (page numbers, repeated column-header rows) so a multi-page
    # statement's repeating header/footer can't be stitched into a real transaction.
    lines = _strip_noise_lines(lines)

    # ── Read the discovered schema so it actually DRIVES parsing (not decorative) ─
    # date_format    → tried first when parsing dates (disambiguates DD/MM vs MM/DD)
    # balance_position == "none" → statement has no running balance; the single
    #                   trailing money token is the amount and Balance stays blank
    # narration_wraps → whether a transaction can span several printed lines
    date_format_hint = _format_to_strptime(schema.get("date_format", ""))
    balance_position = str(schema.get("balance_position", "last_money_token") or "last_money_token")
    require_balance = balance_position != "none"
    narration_wraps = bool(schema.get("narration_wraps", True))
    logger.info(
        "standardiser.standardise_digital_pdf_transactions: schema in use → "
        "date_format=%r balance_position=%r narration_wraps=%s has_ref=%s has_cheque=%s",
        schema.get("date_format", ""), balance_position, narration_wraps,
        schema.get("has_reference_number", True), schema.get("has_cheque_number", True),
    )

    # Seed the balance check with the opening balance so even the FIRST row's
    # debit/credit direction is correct. If metadata didn't supply one, find it in
    # the text (an "Opening Balance" / "B/F" line). This only seeds parsing — it
    # does not change the account-metadata output.
    if not opening_balance:
        m = re.search(
            r"(?:opening\s*balance|b/?f)\s*[:\-]?\s*(?:Rs\.?|INR|₹)?\s*([\d,]+\.\d{2})",
            raw_text or "", re.IGNORECASE)
        if m:
            opening_balance = m.group(1)

    # ── 1+2. Parse rows (date-anchored, with multi-line stitching) ────────────
    records = []
    current = None
    for line in lines:
        s = line.strip()
        if DATE_AT_LINE_START_PATTERN.match(s):
            if current:
                records.append(current)
            current = _parse_single_transaction_line(s, require_balance=require_balance)
            if current:
                current["Account_ID"] = account_id
                current["Bank_Name"] = bank_name
        elif current is not None and narration_wraps:
            # A continuation line. If it is JUST a timestamp (some banks wrap the
            # transaction time onto its own line), record it as the Time instead of
            # polluting the narration. Otherwise extend the narration.
            if _TIME_ONLY_LINE.match(s):
                if not current.get("Time"):
                    current["Time"] = s
            else:
                current["Narration"] = (current.get("Narration", "") + " " + s).strip()
    if current:
        records.append(current)

    if not records:
        return pd.DataFrame(columns=RICH_COLUMNS)

    # Scrub any embedded "Page X of Y" fragment that slipped into a narration and
    # collapse the whitespace it leaves behind.
    for rec in records:
        narr = rec.get("Narration", "")
        if narr:
            rec["Narration"] = re.sub(r"\s{2,}", " ", _PAGE_FRAGMENT.sub(" ", narr)).strip()

    df = pd.DataFrame(records)

    # Rows whose direction the bank stated explicitly (Dr./Cr.) are "locked" — the
    # balance-change correction below must NOT touch them, because some banks list
    # transactions newest-first, which would invert a balance-based guess.
    if "dr_cr" in df.columns:
        df["_dir_locked"] = df["dr_cr"].astype(str).str.strip().ne("")
    else:
        df["_dir_locked"] = False

    # ── 3. Debit/Credit from the amount, then type column ─────────────────────
    if "amount" in df.columns:
        df = _infer_debit_credit_from_amount(df)
    df = _clean_and_type_columns(df, date_format=date_format_hint)
    if "Transaction_Type" not in df.columns:
        df["Transaction_Type"] = ""

    # ── 4. Reference / cheque numbers (gated by the discovered schema) ────────
    has_ref = bool(schema.get("has_reference_number", True))
    has_chq = bool(schema.get("has_cheque_number", True))
    refs, cheques = [], []
    for narr in df["Narration"].tolist():
        r, c = _extract_ref_cheque(str(narr), has_ref, has_chq)
        refs.append(r)
        cheques.append(c)
    df["Reference_Number"] = refs
    df["Cheque_Number"] = cheques

    # ── Correct debit/credit direction from the running balance ───────────────
    df = _correct_direction_by_balance(df, _clean_amount_to_float(opening_balance))

    # Ensure all rich columns exist and order them.
    for col in RICH_COLUMNS:
        if col not in df.columns:
            df[col] = 0.0 if col in ("Debit", "Credit", "Balance") else ""
    df = df[RICH_COLUMNS]
    df = df.dropna(subset=["Date"]).reset_index(drop=True)
    return df


def standardise_llm_transactions(
    transactions: List[Dict[str, Any]],
    account_id: str,
    bank_name: str,
    opening_balance: str = "",
) -> pd.DataFrame:
    """
    Builds the clean transaction table from the LLM's structured JSON, then
    DETERMINISTICALLY corrects debit/credit from the running balance.

    The LLM is great at understanding the document (separating date/time, narration
    vs reference/cheque number, metadata vs rows) but unreliable at the arithmetic
    of which way money moved. The balance change is unambiguous, so we recompute
    the debit/credit direction from it and override the LLM where they disagree.

    Parameters:
        transactions (list[dict]): rows from llm_structurer (date/time/description/
            reference_number/cheque_number/debit/credit/balance/transaction_type).
        account_id (str): stamped on every row.
        bank_name (str): stamped on every row.
        opening_balance (str): seeds the balance check for the FIRST row, so even
            row 1's direction is correct.

    Returns:
        pd.DataFrame: Date, Time, Narration, Reference_Number, Cheque_Number,
            Debit, Credit, Balance, Transaction_Type, Account_ID, Bank_Name.
            Rows with an unparseable date are dropped.
    """
    if not transactions:
        return _create_empty_standard_dataframe()

    rows = []
    for t in transactions:
        rows.append({
            "Date": t.get("date", ""),
            "Time": t.get("time", ""),
            "Narration": t.get("description", ""),
            "Reference_Number": t.get("reference_number", ""),
            "Cheque_Number": t.get("cheque_number", ""),
            "Debit": t.get("debit", ""),
            "Credit": t.get("credit", ""),
            "Balance": t.get("balance", ""),
            "Transaction_Type": t.get("transaction_type", ""),
            "Account_ID": account_id,
            "Bank_Name": bank_name,
        })
    df = pd.DataFrame(rows)

    # Type the columns (dates, amounts, strings).
    df["Date"] = df["Date"].apply(_parse_date)
    for col in ["Debit", "Credit", "Balance"]:
        df[col] = df[col].apply(_clean_amount_to_float)
    for col in ["Time", "Narration", "Reference_Number", "Cheque_Number",
                "Transaction_Type", "Account_ID", "Bank_Name"]:
        df[col] = df[col].astype(str).str.strip().replace({"nan": "", "None": "", "NaT": ""})

    # ── Correct debit/credit from the running balance (authoritative) ─────────
    df = _correct_direction_by_balance(df, _clean_amount_to_float(opening_balance))

    # Drop rows with an unparseable date (kept the table trustworthy).
    df = df.dropna(subset=["Date"]).reset_index(drop=True)
    return df


def _correct_direction_by_balance(df: pd.DataFrame, opening_balance: float = 0.0) -> pd.DataFrame:
    """
    Reassigns each row's amount to Debit or Credit based on the balance change.

    balance went DOWN  → money left → Debit.
    balance went UP    → money came in → Credit.
    The amount used is whichever of Debit/Credit the LLM filled in (the magnitude
    is reliable; only the direction is corrected).
    """
    if df.empty:
        return df
    prev = opening_balance if opening_balance else None
    debits, credits, types = [], [], []
    has_type = "Transaction_Type" in df.columns
    for _, row in df.iterrows():
        bal = row["Balance"]
        amount = row["Debit"] if row["Debit"] and row["Debit"] > 0 else row["Credit"]
        amount = amount or 0.0
        d, c = row["Debit"], row["Credit"]
        locked = bool(row.get("_dir_locked", False))  # explicit Dr./Cr. — trust it
        if not locked and prev is not None and pd.notna(bal) and amount > 0:
            delta = bal - prev
            if delta < -0.01:
                d, c = amount, 0.0
            elif delta > 0.01:
                d, c = 0.0, amount
        debits.append(d)
        credits.append(c)
        # Keep Transaction_Type consistent with the corrected direction.
        types.append("debit" if d and d > 0 else ("credit" if c and c > 0 else
                     (str(row["Transaction_Type"]) if has_type else "")))
        if pd.notna(bal):
            prev = bal
    df["Debit"] = debits
    df["Credit"] = credits
    if has_type:
        df["Transaction_Type"] = types
    return df


def standardise_transaction_records(
    records: List[Dict[str, Any]],
    account_id: str,
    bank_name: str,
) -> pd.DataFrame:
    """
    Standardises a list of already-structured transaction dicts (from the vision
    reader) into the unified schema.

    Each record looks like:
        {"date": "01/08/2022", "narration": "...", "debit": "120",
         "credit": "", "balance": "253159.45"}

    The vision model has already split debit vs credit, so unlike the text parser
    there is no guessing here — we just normalise types and shape.

    Parameters:
        records (list[dict]): transaction dicts with date/narration/debit/credit/balance.
        account_id (str): identifier to stamp on every row.
        bank_name (str): bank name to stamp on every row.

    Returns:
        pd.DataFrame: standardised, with the STANDARD_COLUMNS. Rows whose date
                      cannot be parsed (e.g. a fully UNREADABLE date) are dropped
                      to keep the clean table trustworthy.
    """
    if not records:
        return _create_empty_standard_dataframe()

    rows = []
    for rec in records:
        rows.append({
            "Date": rec.get("date", ""),
            "Time": rec.get("time", ""),
            "Narration": rec.get("narration", ""),
            "Reference_Number": _clean_identifier(rec.get("reference_number", "")),
            "Cheque_Number": _clean_identifier(rec.get("cheque_number", "")),
            "Debit": rec.get("debit", ""),
            "Credit": rec.get("credit", ""),
            "Balance": rec.get("balance", ""),
            "Account_ID": account_id,
            "Bank_Name": bank_name,
        })

    df = pd.DataFrame(rows)
    df = _clean_and_type_columns(df)
    df = df[STANDARD_COLUMNS + REFERENCE_COLUMNS].copy()

    # Drop rows where the date could not be parsed at all (e.g. UNREADABLE date).
    before = len(df)
    df = df.dropna(subset=["Date"])
    if before - len(df) > 0:
        logger.info(
            "standardiser.standardise_transaction_records: "
            "dropped %d row(s) with an unreadable/invalid date.",
            before - len(df),
        )
    return df.reset_index(drop=True)


def _clean_identifier(value: Any) -> str:
    """
    Normalises a cheque / reference identifier to a clean string.

    Pandas often reads a numeric cheque number as a float (123456.0); an
    investigator needs "123456", not "123456.0". Missing values become "" (never
    the literal string "nan").
    """
    if pd.isna(value) or value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    s = str(value).strip()
    if s.lower() in ("nan", "none", "nat", "-", ""):
        return ""
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s


# Header keywords that semantically denote a CHEQUE/instrument number vs a
# transaction REFERENCE. These are generic banking vocabulary (NOT bank names), so
# they encode no per-bank overfitting and the anti-overfitting guard is unaffected.
_CHEQUE_HINTS = ("cheque", "chq", "instrument", "instr")
_REFERENCE_HINTS = ("reference", "ref", "rrn", "utr", "txnid", "transactionid",
                    "transactionref", "refid", "referenceid", "neftref", "impsref")


def _match_reference_cheque_columns(columns, exclude=None):
    """
    Finds, by MEANING, which raw columns hold the cheque number and the reference
    number — independent of the exact label the bank used (CHQNO, Chq No, Cheque
    Number, Ref No, Reference No, RRN, UTR, ...). This is what makes the mapping
    SEMANTIC rather than tied to one bank's spelling.

    Returns (reference_column_name_or_None, cheque_column_name_or_None).

    Policy (team decision): a column that means CHEQUE → cheque; a column that means
    REFERENCE → reference; a COMBINED/ambiguous "Chq/Ref" column → reference (the
    more general bucket). Columns already used for a core field are excluded so we
    never steal the date / narration / amount columns.
    """
    exclude = set(exclude or [])

    def norm(c):
        return re.sub(r"[^a-z0-9]", "", str(c).lower())

    ref_col = chq_col = None
    for col in columns:
        if col in exclude:
            continue
        n = norm(col)
        if not n:
            continue
        has_chq = any(h in n for h in _CHEQUE_HINTS)
        has_ref = any(h in n for h in _REFERENCE_HINTS)
        if has_chq and not has_ref and chq_col is None:
            chq_col = col
        elif has_ref and not has_chq and ref_col is None:
            ref_col = col
        elif has_chq and has_ref and ref_col is None:
            # Combined "Cheque/Ref" column → reference (the general bucket), per policy.
            ref_col = col
    return ref_col, chq_col


def standardise_dataframe_direct(
    raw_df: pd.DataFrame,
    column_map: Dict[str, Any],
    account_id: str,
    bank_name: str,
) -> pd.DataFrame:
    """
    Standardises a raw DataFrame from Excel/CSV directly by renaming and
    reformatting columns, without going through text parsing.

    Called by the pipeline when the input is an Excel or CSV file that
    was already read into a DataFrame by extract_dataframe_from_excel_csv().

    The column_map maps standard field names to the actual column headers
    in this specific bank's statement format:
        {"date": "Date", "narration": "Particulars", "debit": "Debit",
         "credit": "Credit", "balance": "Balance"}

    Parameters:
        raw_df (pd.DataFrame): Raw DataFrame from the Excel/CSV extractor,
                               with original bank-specific column names.
        column_map (dict): Column mapping from identify_column_structure().
                           Values are column header name strings.
        account_id (str): Investigator-provided account identifier.
        bank_name (str): Investigator-provided bank name.

    Returns:
        pd.DataFrame: Standardised DataFrame with the exact STANDARD_COLUMNS.
    """
    if raw_df is None or raw_df.empty:
        logger.warning(
            "standardiser.standardise_dataframe_direct: "
            "Empty DataFrame received for account '%s'.",
            account_id,
        )
        return _create_empty_standard_dataframe()

    logger.info(
        "standardiser.standardise_dataframe_direct: "
        "Standardising %d rows from DataFrame for account '%s'. "
        "Available columns: %s",
        len(raw_df),
        account_id,
        raw_df.columns.tolist(),
    )

    # Build a reverse mapping: original_column_name → standard_field_name
    # We try both string column names and integer indices.
    column_rename = {}
    for standard_field, source_column in column_map.items():
        if source_column is None:
            continue

        if isinstance(source_column, int):
            # Integer index — convert to the actual column name at that position
            if source_column < len(raw_df.columns):
                actual_col_name = raw_df.columns[source_column]
                column_rename[actual_col_name] = standard_field.capitalize()
        elif isinstance(source_column, str):
            # String column name — find the matching column (case-insensitive)
            matching_col = _find_column_case_insensitive(raw_df.columns, source_column)
            if matching_col:
                column_rename[matching_col] = standard_field.capitalize()
            else:
                # Try partial matching — e.g., "Withdrawal" matches "Withdrawal Amt"
                matching_col = _find_column_partial_match(raw_df.columns, source_column)
                if matching_col:
                    column_rename[matching_col] = standard_field.capitalize()

    logger.info(
        "standardiser.standardise_dataframe_direct: "
        "Column rename map: %s",
        column_rename,
    )

    # If we couldn't map the column_map well, try a heuristic column guessing approach
    if "Date" not in column_rename.values():
        column_rename = _guess_column_mapping(raw_df.columns)

    # Create a copy with renamed columns
    df = raw_df.rename(columns=column_rename).copy()

    # Ensure all required standard columns are present
    # If a column is missing, add it as NaN/0
    for col in ["Date", "Narration", "Debit", "Credit", "Balance"]:
        if col not in df.columns:
            logger.warning(
                "standardiser.standardise_dataframe_direct: "
                "Column '%s' not found in DataFrame. Adding as empty.",
                col,
            )
            df[col] = np.nan
    # Excel/CSV statements rarely carry a per-row time → blank Time column.
    if "Time" not in df.columns:
        df["Time"] = ""

    # Attach identifiers
    df["Account_ID"] = account_id
    df["Bank_Name"] = bank_name

    # ── Capture cheque / reference identifiers SEMANTICALLY (not by exact name) ──
    # A statement may label these "CHQNO", "Chq No", "Cheque Number", "Ref No",
    # "RRN", "UTR", ... We match on MEANING, so the value is never lost just because
    # of the bank's column label. Columns already used for a core field are excluded.
    ref_src, chq_src = _match_reference_cheque_columns(
        raw_df.columns, exclude=set(column_rename.keys()))
    df["Reference_Number"] = (df[ref_src].map(_clean_identifier)
                              if ref_src is not None and ref_src in df.columns else "")
    df["Cheque_Number"] = (df[chq_src].map(_clean_identifier)
                           if chq_src is not None and chq_src in df.columns else "")
    if ref_src or chq_src:
        logger.info(
            "standardiser.standardise_dataframe_direct: matched reference column %r, "
            "cheque column %r (semantic, label-independent).", ref_src, chq_src)

    # Clean and type each column
    df = _clean_and_type_columns(df)

    # Select the standard columns PLUS the preserved cheque/reference identifiers.
    df = df[STANDARD_COLUMNS + REFERENCE_COLUMNS].copy()

    # Drop rows where the date could not be parsed (headers, totals, blank rows)
    initial_count = len(df)
    df = df.dropna(subset=["Date"])
    dropped_count = initial_count - len(df)
    if dropped_count > 0:
        logger.info(
            "standardiser.standardise_dataframe_direct: "
            "Dropped %d rows with unparseable dates.",
            dropped_count,
        )

    logger.info(
        "standardiser.standardise_dataframe_direct: "
        "Result: %d valid transaction rows.",
        len(df),
    )
    return df.reset_index(drop=True)


# ─────────────────────────────────────────────────────────────────────────────
# PRIVATE HELPER FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────


def _detect_csv_format(lines: List[str]) -> bool:
    """
    Checks whether the text looks like comma-separated CSV format.

    Returns True if most non-empty lines contain commas as delimiters,
    indicating this text was produced by DataFrame.to_csv() rather than
    extracted from a PDF or DOCX document.

    Parameters:
        lines (list[str]): Non-empty lines from the raw text.

    Returns:
        bool: True if CSV format detected, False if space/tab-separated.
    """
    if not lines:
        return False
    # Check the first 5 lines — if most have commas, it's CSV
    sample = lines[:5]
    comma_lines = sum(1 for line in sample if "," in line)
    return comma_lines >= len(sample) // 2


def _parse_csv_text(
    raw_text: str,
    column_map: Dict[str, Any],
    account_id: str,
    bank_name: str,
) -> pd.DataFrame:
    """
    Parses CSV-formatted text using pandas, then maps columns to standard names.

    The CSV format is produced when the pipeline converts an Excel/CSV
    DataFrame to a text string via DataFrame.to_csv().

    Parameters:
        raw_text (str): Comma-separated text with header row.
        column_map (dict): Maps standard fields to column indices/names.
        account_id (str): Account identifier.
        bank_name (str): Bank name.

    Returns:
        pd.DataFrame: Standardised DataFrame.
    """
    try:
        # Use pandas to parse the CSV text from a string buffer.
        df = pd.read_csv(StringIO(raw_text))

        logger.info(
            "standardiser._parse_csv_text: "
            "Parsed CSV with %d rows and columns: %s",
            len(df),
            df.columns.tolist(),
        )

        # Pass through the direct DataFrame standardiser
        return standardise_dataframe_direct(df, column_map, account_id, bank_name)

    except Exception as error:
        logger.error(
            "standardiser._parse_csv_text: "
            "Failed to parse CSV text: %s",
            error,
        )
        return _create_empty_standard_dataframe()


def _parse_text_lines(
    lines: List[str],
    column_map: Dict[str, Any],
    account_id: str,
    bank_name: str,
) -> pd.DataFrame:
    """
    Parses space/tab-separated bank statement text lines into transaction records.

    Uses a date-anchored approach:
      1. A valid transaction line starts with a date pattern (DD/MM/YYYY)
      2. The balance is always the last decimal number on the line
      3. The next-to-last decimal number is the transaction amount
      4. The narration is everything between the initial date(s) and the amounts
      5. Debit/credit is determined from narration keywords or balance change

    Parameters:
        lines (list[str]): Non-empty text lines from the extracted document.
        column_map (dict): Column structure hint from Groq.
        account_id (str): Account identifier.
        bank_name (str): Bank name.

    Returns:
        pd.DataFrame: Standardised DataFrame with parsed transactions.
    """
    # Bank statements wrap a single transaction across several printed lines: the
    # first line starts with a date and holds the amounts; the following lines (no
    # leading date) are continuation of the narration. We therefore parse each
    # date-starting line on its own, and APPEND any non-date lines to the current
    # transaction's narration — so multi-line rows are stitched, not lost.
    records = []
    current = None  # the record being built

    for line in lines:
        stripped = line.strip()
        if DATE_AT_LINE_START_PATTERN.match(stripped):
            if current:
                records.append(current)
            current = _parse_single_transaction_line(stripped)
            if current:
                current["Account_ID"] = account_id
                current["Bank_Name"] = bank_name
        elif current is not None:
            # Continuation line — extend the narration only (amounts already read).
            current["Narration"] = (current.get("Narration", "") + " " + stripped).strip()

    if current:
        records.append(current)

    if not records:
        return _create_empty_standard_dataframe()

    df = pd.DataFrame(records)

    # Infer debit/credit for rows where we only have an "amount" field
    # (many Indian bank PDFs show only one amount column, not separate debit/credit)
    if "amount" in df.columns:
        df = _infer_debit_credit_from_amount(df)

    # Clean and type columns
    df = _clean_and_type_columns(df)

    # Select only standard columns
    available_cols = [col for col in STANDARD_COLUMNS if col in df.columns]
    for col in STANDARD_COLUMNS:
        if col not in df.columns:
            df[col] = np.nan

    return df[STANDARD_COLUMNS].copy().reset_index(drop=True)


def _parse_single_transaction_line(line: str, require_balance: bool = True) -> Optional[Dict[str, Any]]:
    """
    Parses a single transaction line from a space-separated bank statement.

    Strategy:
      1. The last token should be the balance (a decimal number)
      2. The second-to-last token should be the transaction amount
      3. The first token(s) are the date(s)
      4. Everything in between is the narration

    Parameters:
        line (str): A single text line that starts with a date.
        require_balance (bool): when True (default), a usable row needs at least an
            amount AND a running balance. When the discovered schema says the
            statement has NO balance column (balance_position == "none"), set this
            False: the single trailing money token is taken as the amount and the
            Balance is left blank rather than rejecting the row.

    Returns:
        dict: Record with keys: "Date", "Narration", "amount", "Balance"
              Returns None if the line cannot be parsed as a transaction.
    """
    # ── Step 1: peel the date(s) off the front ─────────────────────────────
    # Strip the leading transaction date, and a value-date if one follows it
    # immediately (many banks print Txn Date + Value Date side by side).
    m = DATE_AT_LINE_START_PATTERN.match(line)
    if not m:
        return None
    date_str = m.group(1).strip()
    rest = line[m.end():].strip()

    m2 = DATE_AT_LINE_START_PATTERN.match(rest)
    if m2:
        rest = rest[m2.end():].strip()

    # ── Step 1b: capture a transaction time if the bank prints one ─────────
    # e.g. "13:52:58" or "08:04:44 PM". If there is no time, Time stays "" — we
    # never invent a 00:00:00.
    time_str = ""
    mt = re.match(r"(\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AaPp][Mm])?)\b", rest)
    if mt:
        time_str = mt.group(1).strip()
        rest = rest[mt.end():].strip()

    # ── Step 1c: strip a VALUE date that appears after the time ────────────
    # Some banks print: TxnDate  Time  ValueDate  Description … (e.g. IDBI).
    m3 = DATE_AT_LINE_START_PATTERN.match(rest)
    if m3:
        rest = rest[m3.end():].strip()

    # ── Step 1d: explicit Dr./Cr. marker ──────────────────────────────────
    # The most reliable direction signal — essential for statements listed
    # newest-first (e.g. IDBI), where the running-balance trick would be inverted.
    direction = ""
    md = re.search(r"\b(Dr|Cr)\.?\s+(?:INR|RS\.?|₹|\d)", rest, re.IGNORECASE)
    if md:
        direction = "D" if md.group(1).lower() == "dr" else "C"

    # ── Step 2: pull the trailing amounts off the end ──────────────────────
    # The last money token is the running balance; the one before it is the
    # transaction amount. Reference/cheque numbers (no decimals) stay in the
    # narration. Layout-independent across bank column orders.
    tokens = rest.split()
    money = []
    while tokens and _is_money_token(tokens[-1]) and len(money) < 3:
        money.insert(0, tokens.pop())

    if require_balance:
        if len(money) < 2:
            # Need at least an amount and a balance to be a usable transaction row.
            return None
        balance_str = money[-1]
        amount_str = money[-2]
    else:
        # Schema says there is no running balance — the trailing money token is the
        # amount and there is no balance to read.
        if len(money) < 1:
            return None
        balance_str = ""
        amount_str = money[-1]
    narration = " ".join(tokens).strip()
    # Drop trailing currency / Dr-Cr noise the marker left behind ("… Dr. INR").
    narration = re.sub(r"\s+(?:Dr|Cr)\.?(?:\s+(?:INR|RS|Rs|₹))?\s*$", "", narration, flags=re.IGNORECASE).strip()
    narration = re.sub(r"\s+(?:INR|RS|Rs|₹)\s*$", "", narration, flags=re.IGNORECASE).strip()

    if not direction:
        direction = _direction_of(amount_str)  # fall back to a (Dr)/(Cr) suffix on the amount

    return {
        "Date": date_str,
        "Time": time_str,
        "Narration": narration,
        "amount": _clean_amount(amount_str),
        "dr_cr": direction,  # 'D'/'C' when the bank marks direction explicitly
        "Balance": balance_str,  # cleaned later
    }


def _infer_debit_credit_from_amount(df: pd.DataFrame) -> pd.DataFrame:
    """
    Converts a single "amount" column into separate "Debit" and "Credit" columns.

    Indian bank PDFs often show only one amount column because a transaction
    is either a debit OR a credit — never both at the same time. The direction
    (debit/credit) is determined from the narration text keywords.

    Logic:
      1. Check narration for debit keywords (UPI/DR, WDL, ATM/W, etc.)
         → If found, it is a Debit
      2. Check narration for credit keywords (UPI/CR, DEPOSIT, CREDIT, etc.)
         → If found, it is a Credit
      3. If narration has no clear keyword, calculate from balance change:
         - If current_balance > previous_balance → it was a credit
         - If current_balance < previous_balance → it was a debit

    Parameters:
        df (pd.DataFrame): DataFrame with "amount" and "Narration" columns.

    Returns:
        pd.DataFrame: DataFrame with "Debit" and "Credit" columns added.
    """
    debits = []
    credits = []

    # Pre-parse the Balance column as floats so we can compute balance changes
    balances = df["Balance"].apply(_clean_amount_to_float).tolist()

    for row_index, (_, row) in enumerate(df.iterrows()):
        narration = str(row.get("Narration", "")).upper()
        amount = row.get("amount", 0.0)
        if amount is None:
            amount = 0.0

        # ── Step 0: if the bank marked the amount (Dr)/(Cr), trust that ────
        # This is the most reliable signal — no guessing needed (e.g. Kotak).
        marked = str(row.get("dr_cr", "") or "")
        if marked == "D":
            debits.append(amount); credits.append(0.0); continue
        if marked == "C":
            debits.append(0.0); credits.append(amount); continue

        # ── Step 1: Check narration keywords for direction ─────────────────
        is_debit = any(keyword.upper() in narration for keyword in DEBIT_KEYWORDS)
        is_credit = any(keyword.upper() in narration for keyword in CREDIT_KEYWORDS)

        if is_debit and not is_credit:
            debits.append(amount)
            credits.append(0.0)
        elif is_credit and not is_debit:
            debits.append(0.0)
            credits.append(amount)
        else:
            # ── Step 2: Fallback — infer direction from balance change ──────
            # If narration keywords are ambiguous, check whether the running
            # balance went up (credit) or down (debit) compared to the
            # previous row. This correctly handles NEFT, RTGS, and other
            # transfer narrations that don't include /DR/ or /CR/.
            if row_index > 0:
                prev_balance = balances[row_index - 1]
                curr_balance = balances[row_index]
                # Allow a small tolerance for rounding errors
                balance_change = curr_balance - prev_balance
                if balance_change < -0.5:
                    # Balance went down → this was a debit
                    debits.append(amount)
                    credits.append(0.0)
                elif balance_change > 0.5:
                    # Balance went up → this was a credit
                    debits.append(0.0)
                    credits.append(amount)
                else:
                    # Balance barely changed — ambiguous (e.g., zero-value transactions)
                    # Store in credit as a safe fallback
                    debits.append(0.0)
                    credits.append(amount)
            else:
                # First row — no previous balance available, default to credit
                debits.append(0.0)
                credits.append(amount)

    df["Debit"] = debits
    df["Credit"] = credits

    # Remove the temporary "amount" column now that we have separate debit/credit
    if "amount" in df.columns:
        df = df.drop(columns=["amount"])

    return df


def _clean_amount(value_str: str) -> Optional[float]:
    """
    Converts a string representation of a currency amount to a float.

    Handles:
        - Comma-formatted numbers: "1,50,000.00" → 150000.0
        - Rupee symbol: "₹5,000" → 5000.0
        - Dash or nil (empty amount): "-" or "nil" → returns None (not 0.0)
        - Plain numbers: "5000.00" → 5000.0

    Parameters:
        value_str (str): The amount string to clean.

    Returns:
        float: The cleaned amount as a float.
               Returns None if the string is not a valid amount
               (which tells the caller this token is not a number).
    """
    if not value_str:
        return None

    # Strip whitespace, currency, commas, and any trailing (Dr)/(Cr) marker.
    cleaned = value_str.strip().replace("₹", "").replace(",", "").replace(" ", "")
    cleaned = re.sub(r"\(?(?:Dr|Cr)\)?$", "", cleaned, flags=re.IGNORECASE)

    # Empty amounts are represented as "-", "nil", "N/A", or just blank
    if cleaned in ("-", "nil", "NIL", "N/A", "n/a", ""):
        return None  # Not a number — return None to indicate "no value"

    # Try to convert to float
    try:
        return float(cleaned)
    except ValueError:
        return None


def _clean_amount_to_float(value: Any) -> float:
    """
    Converts any amount value (string, float, int, None) to a float.

    Used when cleaning DataFrame columns where values may be:
        - Already floats: 5000.0
        - Strings with commas: "5,000.00"
        - NaN (pandas missing value)
        - None
        - Dashes: "-"

    Returns 0.0 for missing/empty values (unlike _clean_amount which returns None).

    Parameters:
        value: Any value that should represent a monetary amount.

    Returns:
        float: The amount as float, or 0.0 if the value is missing/invalid.
    """
    if pd.isna(value) or value is None:
        return 0.0

    if isinstance(value, (int, float)):
        return float(value)

    cleaned = str(value).strip().replace("₹", "").replace(",", "").replace(" ", "")
    cleaned = re.sub(r"\(?(?:Dr|Cr)\)?$", "", cleaned, flags=re.IGNORECASE)

    if cleaned in ("-", "nil", "NIL", "N/A", "n/a", ""):
        return 0.0

    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _parse_date(date_value: Any, preferred_format: str = None) -> Optional[pd.Timestamp]:
    """
    Parses a date value from any of the common Indian bank statement date formats.

    Tries multiple formats in order:
        DD/MM/YYYY, DD-MM-YYYY, MM/DD/YYYY, YYYY-MM-DD,
        DD MMM YYYY, DD-MMM-YY, DD-MMM-YYYY, DD MMMM YYYY

    Uses dayfirst=True which is correct for most Indian formats where
    the day comes before the month (e.g., 01/08/2022 means 1st August, not
    January 8th).

    If `preferred_format` (a strptime pattern, e.g. "%m/%d/%Y") is supplied — the
    LLM-discovered schema's date_format converted by _format_to_strptime — it is
    tried FIRST. This is what disambiguates a statement that uses US-style
    MM/DD/YYYY from the Indian DD/MM/YYYY default, instead of silently guessing.

    Parameters:
        date_value: A date string, datetime, Timestamp, or pandas NaT.
        preferred_format: optional strptime pattern to try before the defaults.

    Returns:
        pd.Timestamp or None: The parsed date, or None if parsing failed.
    """
    if pd.isna(date_value) or date_value is None:
        return None

    # Already a valid timestamp
    if isinstance(date_value, pd.Timestamp):
        return date_value

    date_str = str(date_value).strip()

    if not date_str or date_str in ("nan", "NaT", "None"):
        return None

    # The schema's discovered date format takes priority when provided.
    if preferred_format:
        try:
            return pd.to_datetime(date_str, format=preferred_format)
        except (ValueError, TypeError):
            pass

    # Try pandas auto-parse first with dayfirst=True (for Indian DD/MM/YYYY format)
    try:
        return pd.to_datetime(date_str, dayfirst=True, errors="raise")
    except (ValueError, TypeError):
        pass

    # Try each format explicitly
    for fmt in DATE_FORMATS:
        try:
            return pd.to_datetime(date_str, format=fmt)
        except (ValueError, TypeError):
            continue

    # Could not parse this date string
    logger.debug(
        "standardiser._parse_date: Could not parse date value: '%s'",
        date_str,
    )
    return None


def _clean_and_type_columns(df: pd.DataFrame, date_format: str = None) -> pd.DataFrame:
    """
    Applies type conversion and cleaning to all standard columns in the DataFrame.

    Converts:
        - Date → pandas datetime (using _parse_date)
        - Debit → float (using _clean_amount_to_float)
        - Credit → float (using _clean_amount_to_float)
        - Balance → float (using _clean_amount_to_float)
        - Narration → string

    Parameters:
        df (pd.DataFrame): DataFrame with columns that may need type conversion.
        date_format (str): optional strptime pattern (from the discovered schema)
                           tried first when parsing the Date column.

    Returns:
        pd.DataFrame: Same DataFrame with properly typed columns.
    """
    # ── Date column ─────────────────────────────────────────────────────────
    if "Date" in df.columns:
        df["Date"] = df["Date"].apply(lambda v: _parse_date(v, preferred_format=date_format))

    # ── Numeric columns (Debit, Credit, Balance) ───────────────────────────
    for col in ["Debit", "Credit", "Balance"]:
        if col in df.columns:
            df[col] = df[col].apply(_clean_amount_to_float)

    # ── Narration column ────────────────────────────────────────────────────
    if "Narration" in df.columns:
        # Convert to string and strip extra whitespace
        df["Narration"] = df["Narration"].astype(str).str.strip()
        # Replace pandas NaN string representations with empty string
        df["Narration"] = df["Narration"].replace({"nan": "", "None": "", "NaT": ""})

    # ── Time column (string; blank when the statement has no per-row time) ────
    if "Time" in df.columns:
        df["Time"] = df["Time"].astype(str).str.strip().replace(
            {"nan": "", "None": "", "NaT": ""})

    # ── Account_ID and Bank_Name ────────────────────────────────────────────
    for col in ["Account_ID", "Bank_Name"]:
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip()

    return df


def _find_column_case_insensitive(
    columns: pd.Index,
    target: str,
) -> Optional[str]:
    """
    Finds a column name in a pandas Index using case-insensitive exact matching.

    Parameters:
        columns (pd.Index): The DataFrame's column index.
        target (str): The column name to search for.

    Returns:
        str or None: The matching column name, or None if not found.
    """
    target_lower = target.lower()
    for col in columns:
        if str(col).lower() == target_lower:
            return col
    return None


def _find_column_partial_match(
    columns: pd.Index,
    target: str,
) -> Optional[str]:
    """
    Finds a column name in a pandas Index using case-insensitive partial matching.

    For example, "Withdrawal" would match a column named "Withdrawal Amt".
    This handles the variety of column naming conventions used by different banks.

    Parameters:
        columns (pd.Index): The DataFrame's column index.
        target (str): The partial column name to search for.

    Returns:
        str or None: The first matching column name, or None if not found.
    """
    target_lower = target.lower()
    for col in columns:
        if target_lower in str(col).lower():
            return col
    return None


def _guess_column_mapping(columns: pd.Index) -> Dict[str, str]:
    """
    Heuristically guesses the standard column names from a DataFrame's actual columns.

    This is a fallback used when Groq's column_map cannot be matched to the
    DataFrame's actual column headers. It uses keyword matching to find
    the most likely column for each standard field.

    Parameters:
        columns (pd.Index): The DataFrame's column index.

    Returns:
        dict: Mapping of original_column_name → standard_field_name
    """
    # Keywords associated with each standard field
    field_keywords = {
        "Date": ["date", "txn date", "transaction date", "value date"],
        "Narration": ["narration", "description", "particulars", "remarks", "details"],
        "Debit": ["debit", "withdrawal", "withdraw", "dr", "w/drl", "dr amount"],
        "Credit": ["credit", "deposit", "cr", "cr amount"],
        "Balance": ["balance", "running balance", "closing balance"],
    }

    column_rename = {}
    used_cols = set()

    for standard_field, keywords in field_keywords.items():
        for keyword in keywords:
            for col in columns:
                if col in used_cols:
                    continue
                if keyword.lower() in str(col).lower():
                    column_rename[col] = standard_field
                    used_cols.add(col)
                    break
            if standard_field in column_rename.values():
                break

    logger.info(
        "standardiser._guess_column_mapping: "
        "Guessed column mapping: %s",
        column_rename,
    )
    return column_rename


def _create_empty_standard_dataframe() -> pd.DataFrame:
    """
    Creates an empty DataFrame with the standard column schema.

    Returns an empty DataFrame so that callers always get a valid DataFrame
    object even when no transactions could be extracted.

    Returns:
        pd.DataFrame: Empty DataFrame with standard columns and correct dtypes.
    """
    return pd.DataFrame(columns=STANDARD_COLUMNS)
