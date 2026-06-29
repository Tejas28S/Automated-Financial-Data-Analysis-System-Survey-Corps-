"""
extractor_digital_pdf.py — Text extraction from computer-generated (digital) PDFs.

When a bank generates a statement on their computer system and saves it as a PDF,
the text is stored digitally inside the file as actual characters. This is very
different from a scanned PDF where the content is just a photograph stored as pixels.

This module handles the "digital PDF" case. It uses pdfplumber — a Python library
specifically designed for reading PDF files — to extract all the text from every
page of the statement and combine it into one long string of text.

EXTRACTION STRATEGY — TABLE-AWARE:
    Bank statement PDFs use bordered tables for the transaction section. pdfplumber's
    default extract_text() reads characters in y-coordinate order, which causes a
    cross-row mixing problem: the last narration line of transaction N sits at a y-
    coordinate just above the date row of transaction N+1, so pdfplumber emits the
    two mixed on the same output line. The result: narration from one transaction bleeds
    into the next, and multi-line narrations are fragmented.

    We fix this with a two-layer approach on each page:
        Layer 1 (full-text): extract_text() — captures the account header/metadata
            block that lives above the table ON PAGE 1 ONLY.
        Layer 2 (table-aware): extract_tables() — uses the PDF's actual cell boundaries
            to associate ALL lines of a multi-line narration with the correct table row.
            Multi-line cells are joined with a single space, producing one clean line
            per transaction with no cross-row contamination.
    If the page has no detectable table (e.g. a cover page), we fall back to Layer 1
    only.

    INLINE NARRATION SUPPLEMENT:
    Some PDF layouts (e.g. IDFC First Bank NEFT transactions) print a portion of the
    narration at the same y-coordinate as the date and amounts. pdfplumber's table
    extractor assigns characters to columns by horizontal position — the narration
    fragment lands outside every column boundary and is dropped. However, extract_text()
    DOES emit it on the same line as the date (between the date and the amounts).
    We harvest that "between-date-and-amounts" text and append it to any single-line
    table narration that is missing it.

Team: Survey Corps | CIDECODE Hackathon 2026 | CID Karnataka
"""

import logging
import re
from pathlib import Path

import pandas as pd
import pdfplumber

logger = logging.getLogger(__name__)

# Header-cell vocabulary for STRUCTURED table detection (generic banking terms, never a
# bank name). A transaction-table header row carries at least one date-column word AND
# at least one money-column word. This is how the structured-table fallback tells the
# ledger table apart from a metadata/key-value table on the same page.
_TBL_DATE_WORDS = ("date", "tran date", "txn date", "value date", "posting date",
                   "transaction date", "trans date", "gl date")
_TBL_MONEY_WORDS = ("debit", "credit", "withdrawal", "deposit", "balance", "amount",
                    "dr", "cr", " dr ", " cr ", "withdrawals", "deposits")

# pdfplumber emits "(cid:NN)" when a PDF uses a font glyph it cannot map to a real
# character (very common for tab stops and fancy fonts in real bank statements,
# e.g. SBI prints "Account Number :(cid:9)0000..."). Left in, these artifacts break
# both the account-detail reader and the row parser. We replace them with a space.
_CID_ARTIFACT = re.compile(r"\(cid:\d+\)")

# Minimum columns a table must have to be treated as a transaction table. A table
# with fewer columns is more likely to be a key-value block (account metadata) than
# a multi-column ledger.
_MIN_TABLE_COLS = 4

# Minimum data rows (excluding header) for a table to be worth using.
_MIN_TABLE_ROWS = 2

# Pattern for a date at the start of a string (same semantics as
# DATE_AT_LINE_START_PATTERN in standardiser.py but local to this module).
_DATE_START_RE = re.compile(
    r"^(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}"
    r"|\d{4}[/\-]\d{1,2}[/\-]\d{1,2}"
    r"|\d{1,2}[ \-][A-Za-z]{3,9}[ \-]\d{2,4})"
)

# Date + optional time at the very start of a line, used to build the inline
# narration supplement key.
_DATE_TIME_START_RE = re.compile(
    r"^(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})"
    r"(?:\s+(\d{1,2}:\d{2}(?::\d{2})?))?",
)

# A token that looks like a currency amount (decimal with optional Indian commas and
# optional Dr/Cr suffix).  Used locally without importing standardiser.
_MONEY_LIKE_RE = re.compile(
    r"^[-₹]*[\d]{1,3}(?:,\d{2,3})*(?:\.\d{1,2})?(?:\(?(Dr|Cr)\)?)?"
    r"$|^[-₹]*\d+\.\d{1,2}(?:\(?(Dr|Cr)\)?)?$",
    re.IGNORECASE,
)


def _is_money_like(s: str) -> bool:
    """True if s looks like a currency amount (has decimals, commas, optional Dr/Cr)."""
    return bool(_MONEY_LIKE_RE.match(s.strip()))


def _clean_pdf_text(text: str) -> str:
    """Removes (cid:NN) glyph artifacts so downstream parsing sees clean text."""
    if not text:
        return text
    return _CID_ARTIFACT.sub(" ", text)


def _join_cell(cell) -> str:
    """
    Normalises one table cell to a clean single-line string.

    pdfplumber returns None for empty cells, and may embed \\n when a cell spans
    multiple printed lines (multi-line narrations, or date cells like "09-APR-\\n2025"
    where the year wraps to the next printed line). Parts are joined with a space
    UNLESS the preceding part ends with a word-joining separator (- or /), in which
    case they are joined without a space to reconstruct the original token.

    Examples:
        "09-APR-\\n2025"                  → "09-APR-2025"
        "NEFT/.../AANYA VERMA\\nMEHTA/…" → "NEFT/.../AANYA VERMA MEHTA/…"
        "IMPS-512119381081-\\nDMENT-…"   → "IMPS-512119381081-DMENT-…"
    """
    if cell is None:
        return ""
    parts = [part.strip() for part in str(cell).split("\n") if part.strip()]
    if not parts:
        return ""
    result = parts[0]
    for part in parts[1:]:
        # Join without a space when the previous part ends with a separator that
        # would normally continue into the next token (dates, reference codes).
        if result.endswith(("-", "/")):
            result += part
        else:
            result += " " + part
    return result


def _build_inline_narration_map(full_text: str) -> dict:
    """
    Scans a page's extract_text() output for text that appears BETWEEN the date/time
    and the trailing amounts on a transaction line. This text is "dropped" by the
    table extractor because it occupies the same y-coordinate as the date row and
    falls outside the table's column boundaries (reproducible with IDFC First Bank
    NEFT transactions where the second narration line appears at date y-level).

    Key:   "DD/MM/YY HH:MM" or "DD/MM/YY" (the date + time prefix of the line).
    Value: the text between the date/time/value-date and the trailing amounts.

    The map is built per page and passed to _table_to_lines so that single-line
    table narrations can be supplemented before the line is handed to the standardiser.
    """
    result = {}
    for line in full_text.splitlines():
        s = line.strip()
        m = _DATE_TIME_START_RE.match(s)
        if not m:
            continue
        date_part = m.group(1)
        time_part = m.group(2) or ""
        key = f"{date_part} {time_part}".strip()

        remainder = s[m.end():].strip()

        # Strip optional value date (second date immediately after the time).
        m_vd = re.match(r"^\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}\s*", remainder)
        if m_vd:
            remainder = remainder[m_vd.end():].strip()

        if not remainder:
            continue

        # Strip trailing money tokens — what remains between is the supplement text.
        tokens = remainder.split()
        money = []
        while tokens and _is_money_like(tokens[-1]) and len(money) < 4:
            money.insert(0, tokens.pop())

        if not money:
            continue  # no amounts → not a transaction line

        inline = " ".join(tokens).strip()
        if inline and len(inline) >= 3:
            result[key] = inline

    return result


def _metadata_lines(full_text: str) -> list:
    """
    Returns the account-metadata section from page 1's extract_text() output:
    every line BEFORE the first transaction row (the first date-led line), PLUS any
    later non-transaction line that carries an account-identity LABEL.

    The table cells give us the transactions, and the metadata header is never inside
    a table cell — but pdfplumber emits page-1 text in y-coordinate order, and some
    layouts (e.g. an SBI statement whose IFSC/customer block sits to the side of or
    below the opening rows) place the identity block AFTER the first transaction line.
    Cutting at the first date would silently drop the IFSC / holder / account, leaving
    metadata extraction to scrape a counterparty's name out of a narration. So once we
    pass the first date we still keep label-bearing lines (IFSC, A/C No, Customer/CIF,
    MICR, Branch, …). These are never transactions (the parser needs a leading date),
    so this enriches identity without affecting transaction parsing.
    """
    result = []
    seen_date = False
    for line in full_text.splitlines():
        if _DATE_START_RE.match(line.strip()):
            seen_date = True
            continue  # transaction rows are reconstructed from the table cells
        if not seen_date or _META_LABEL_RE.search(line):
            result.append(line)
    return result[:80]  # safety cap against pathological layouts


# Account-identity labels that may appear in a page-1 metadata block emitted (in
# y-order) below the first transaction row. Generic banking vocabulary only. We
# require the label to be in "Label :" form (a known identity word followed shortly
# by a colon/dash separator) so prose NARRATION lines that merely contain a word like
# "branch" are NOT mistaken for metadata — that guards against a counterparty IFSC in
# a narration ever being scraped as the account's own identity.
_META_LABEL_RE = re.compile(
    r"(?:IFSC|IFS\s*Code|MICR|A/?C|Account|Cust(?:omer)?|CIF|Branch|Nomination|Scheme)"
    r"\b[^:\n]{0,18}[:\-]",
    re.IGNORECASE,
)


def _filter_inline_map(inline_map: dict, table) -> dict:
    """
    Removes supplement entries from inline_map whose text is already captured in
    at least one table narration cell (column 2). This prevents a supplement that
    belongs to one transaction from being incorrectly applied to a DIFFERENT
    transaction that shares the same date+time key (a timestamp collision).

    Algorithm: build the set of all words seen in any table narration column. For
    each supplement entry, compute the fraction of its words that are already in
    the set. If ≥40% of the supplement's words already appear in the table, the
    table already has that text and the supplement would be a duplicate — drop it.
    """
    all_narration_words: set = set()
    for row in (table or []):
        if row and len(row) > 2:
            narr = _join_cell(row[2])
            for word in narr.lower().split():
                all_narration_words.add(word)

    filtered = {}
    for key, supplement in inline_map.items():
        supp_words = supplement.lower().split()
        if not supp_words:
            continue
        overlap = sum(1 for w in supp_words if w in all_narration_words)
        # Keep the supplement only when the majority of its words are NEW.
        if overlap < len(supp_words) * 0.4:
            filtered[key] = supplement

    return filtered


def _table_to_lines(table, inline_map: dict = None) -> list:
    """
    Converts a pdfplumber table (list of row-lists) to a list of text lines.

    Each row becomes one line; cells are separated by two spaces (matching the
    format pdfplumber's extract_text() uses for column boundaries). Empty rows
    and fully-empty rows (only None/blank cells) are dropped.

    inline_map (optional): date-keyed map from _build_inline_narration_map(),
    pre-filtered by _filter_inline_map(). When provided, any single-line narration
    in column 2 that is missing the supplement text from the map is extended with
    it.  This recovers narration fragments that pdfplumber's table extractor drops
    because they fall outside all column boundaries at the y-level of the date row.
    """
    lines = []
    for row in (table or []):
        if not row:
            continue
        cells = [_join_cell(c) for c in row]

        # Supplement single-line narrations from the inline map.
        # Column 0 is the date/time cell; column 2 is the narration cell (standard
        # for most Indian bank tables with Trans Date | Value Date | Narration | …).
        if inline_map and len(cells) >= 3:
            date_key = cells[0].strip()
            narr = cells[2]
            if date_key and narr:
                supplement = inline_map.get(date_key, "")
                # Only append if supplement is not already a substring of the narration.
                if supplement and supplement not in narr:
                    cells[2] = narr + " " + supplement

        row_text = "  ".join(cells).strip()
        if row_text:
            lines.append(row_text)
    return lines


# A header CELL that is genuinely a DATE column (not "Date of Birth"/"Account Open
# Date"/"Statement Date" etc. which appear in metadata tables and would otherwise make
# a metadata table look like a transaction header).
_DATE_COL_CELL = re.compile(
    r"^\s*(?:tran(?:saction)?\s*|txn\s*|value\s*|posting\s*|gl\.?\s*|trans\s*)?date\s*$",
    re.IGNORECASE)
_NON_TXN_DATE_CELL = re.compile(r"birth|open|expiry|kyc|statement|print|generat|report",
                                re.IGNORECASE)
_MONEY_COL_CELL = re.compile(
    r"^\s*(debit|credit|withdrawals?|deposits?|balance|amount|dr|cr|"
    r"withdrawal\s*amt|deposit\s*amt|dr\s*amt|cr\s*amt|"
    r"debit\s*amount|credit\s*amount|transaction\s+(?:debit|credit)\s+amount)\s*$",
    re.IGNORECASE)


# Column-role vocabulary for COORDINATE-based header detection. Each role maps to the
# header words that name it. "ignore" columns (forex rate / local-currency / charges)
# must never be read as the transaction amount — that is the exact defect on FLEXCUBE
# forex layouts where Trans.Rate (=1.00 for INR) was being read as Debit/Credit.
_COORD_HEADER_ROLES = {
    "date":   ("txn dt", "tran dt", "txn date", "tran date", "transaction date", "date", "gl date"),
    "narration": ("narration", "particulars", "description", "remarks", "details"),
    "debit":  ("dr amount", "debit amount", "withdrawal amount", "withdrawal", "debit", "dr"),
    "credit": ("cr amount", "credit amount", "deposit amount", "deposit", "credit", "cr"),
    "balance": ("running balance", "closing balance", "balance"),
}
_COORD_IGNORE = ("trans.rate", "trans rate", "rate", "trans.lcy", "lcy", "conversion",
                 "exchange", "value", "dt value", "chq", "cheque", "ref")


def extract_coordinate_table_df(file_path: str):
    """
    COORDINATE-based column reader (generalised, bank-agnostic). For PDFs whose amounts
    sit in fixed x-columns but the linear-text flattening misreads them — most acutely
    FLEXCUBE-style layouts that print extra Trans.Rate / Trans.LCY columns after the
    real Dr/Cr Amount columns and put the Running Balance on its OWN line — the
    positional text parser grabs the wrong tokens (reading the rate, ~1.00, as the
    amount). This reader instead learns each column's x-position from the HEADER row and
    assigns every value to a column by its x-centre, IGNORING rate/LCY/charge columns,
    and folds a balance-only continuation line into the preceding transaction.

    Returns a DataFrame with Date/Narration/Debit/Credit/Balance, or None if no usable
    coordinate header is found. Driven entirely by header text + geometry — never by a
    bank name or filename.
    """
    import pandas as pd

    def _norm(s):
        return re.sub(r"\s+", " ", (s or "").strip().lower())

    try:
        rows = []
        with pdfplumber.open(file_path) as pdf:
            col_x = None  # {role: (x0,x1)} learned from the first header
            ignore_x = []
            for page in pdf.pages:
                try:
                    words = page.extract_words(use_text_flow=False) or []
                except Exception:
                    continue
                # group words into visual lines by rounded top
                lines = {}
                for w in words:
                    lines.setdefault(round(w["top"] / 3) * 3, []).append(w)
                ordered = [sorted(lines[top], key=lambda w: w["x0"]) for top in sorted(lines)]

                def _learn_roles(word_lists, into, ign):
                    toks = [(w["text"].lower(), w["x0"], w["x1"]) for lw in word_lists for w in lw]
                    for role, names in _COORD_HEADER_ROLES.items():
                        if role in into:
                            continue
                        for nm in names:
                            parts = nm.split()
                            for i in range(len(toks) - len(parts) + 1):
                                if all(parts[k] in toks[i + k][0] for k in range(len(parts))):
                                    into[role] = (toks[i][1], toks[i + len(parts) - 1][2]); break
                            if role in into:
                                break
                    for lw in word_lists:
                        for w in lw:
                            if any(g in w["text"].lower() for g in _COORD_IGNORE):
                                ign.append((w["x0"] + w["x1"]) / 2)

                for li, lw in enumerate(ordered):
                    joined = _norm(" ".join(w["text"] for w in lw))
                    # learn columns from the header — which may wrap across 2 lines
                    # (e.g. "… Dr Amount Cr Amount …" then "Running Balance" beneath).
                    if col_x is None and ("amount" in joined or "withdrawal" in joined
                                          or "debit" in joined or "credit" in joined) \
                            and ("dt" in joined or "date" in joined):
                        col_x = {}
                        window = [lw]
                        if li + 1 < len(ordered):
                            nxt = ordered[li + 1]
                            if not any(_DATE_START_RE.match(w["text"].strip()) for w in nxt):
                                window.append(nxt)   # 2nd header line (Running Balance, etc.)
                        _learn_roles(window, col_x, ignore_x)
                        continue
                    if not col_x or "date" not in col_x:
                        continue
                    # a transaction line: a date-shaped token under the date column
                    def _center(w): return (w["x0"] + w["x1"]) / 2
                    dx0, dx1 = col_x["date"]
                    date_word = next((w for w in lw if _DATE_START_RE.match(w["text"].strip())
                                      and dx0 - 20 <= _center(w) <= dx1 + 40), None)
                    money = [w for w in lw if _MONEY_LIKE_RE.match(w["text"].strip())]
                    if date_word:
                        rec = {"Date": date_word["text"].strip(), "Narration": "",
                               "Debit": "", "Credit": "", "Balance": ""}
                        narr = []
                        for w in lw:
                            c = _center(w); txt = w["text"].strip()
                            if w is date_word:
                                continue
                            if any(abs(c - ix) < 25 for ix in ignore_x) and _MONEY_LIKE_RE.match(txt):
                                continue  # rate / LCY / ignored money column
                            assigned = False
                            for role in ("debit", "credit", "balance"):
                                if role in col_x and _MONEY_LIKE_RE.match(txt):
                                    rx0, rx1 = col_x[role]
                                    if rx0 - 35 <= c <= rx1 + 35:
                                        rec[role.capitalize()] = txt; assigned = True; break
                            if not assigned and not _DATE_START_RE.match(txt):
                                narr.append(txt)
                        rec["Narration"] = " ".join(narr).strip()
                        # A real transaction carries an amount. Page furniture that is
                        # merely date-shaped (e.g. a print-timestamp header line
                        # "11/28/25, 3:40 PM blob:https://…") has no Debit/Credit value
                        # and is dropped — this prevents phantom rows.
                        if str(rec["Debit"]).strip() or str(rec["Credit"]).strip():
                            rows.append(rec)
                    elif rows and money:
                        # balance-only / continuation line: a money token under the balance
                        # column belongs to the preceding transaction's running balance
                        if "balance" in col_x and not rows[-1]["Balance"]:
                            bx0, bx1 = col_x["balance"]
                            for w in money:
                                if bx0 - 35 <= _center(w) <= bx1 + 35:
                                    rows[-1]["Balance"] = w["text"].strip(); break
        if rows:
            df = pd.DataFrame(rows)
            if (df["Balance"].astype(str).str.strip() != "").sum() >= 0.5 * len(df):
                logger.info("extractor_digital_pdf.extract_coordinate_table_df: '%s' → "
                            "%d coordinate rows; columns=%s", Path(file_path).name,
                            len(df), col_x)
                return df
    except Exception as e:
        logger.warning("extractor_digital_pdf.extract_coordinate_table_df: failed for '%s': %s",
                       file_path, e)
    return None


def _row_is_table_header(cells) -> bool:
    """
    True if a table row is a TRANSACTION-table header. Requires a genuine date COLUMN
    cell (not 'Date of Birth' etc.) AND at least two distinct money/balance column cells
    — the structural shape that separates a ledger header from a metadata key:value
    table that merely mentions a date and a balance.
    """
    texts = [(_join_cell(c) or "").strip() for c in cells]
    has_date_col = any(_DATE_COL_CELL.match(t) and not _NON_TXN_DATE_CELL.search(t)
                       for t in texts)
    money_cols = sum(1 for t in texts if _MONEY_COL_CELL.match(t))
    return has_date_col and money_cols >= 2


def extract_transaction_table_df(file_path: str):
    """
    STRUCTURED-table fallback (generalised, bank-agnostic): returns the transaction
    ledger as a pandas DataFrame with the table's own header as columns and every data
    row preserved — including BLANK cells (an empty Withdrawal stays empty instead of
    collapsing) and extra trailing columns (Channel / Alpha / Chq). This is what the
    flat-text path cannot do: when amounts sit in fixed table columns and a non-money
    column trails the balance, the positional text parser fails, but a header-mapped
    column read does not.

    The transaction table is identified purely by its header shape (a date column word
    AND a money/balance column word) — never by filename or bank. Rows from continuation
    pages that repeat the same column count are appended. Returns None if no such table
    is found, so callers fall back to the existing text path unchanged.
    """
    try:
        with pdfplumber.open(file_path) as pdf:
            header = None
            ncols = 0
            data_rows = []
            header_seen = False

            def _is_txn_data_row(cells):
                # A transaction row leads with its date: the date is in the first or
                # second cell (cell 0 may be a serial/Sr-No). Metadata rows carry their
                # date deep in the row, so they are excluded.
                lead = [(c or "").strip() for c in cells[:2]]
                return any(_DATE_START_RE.match(c) for c in lead)

            for page in pdf.pages:
                try:
                    tables = page.extract_tables() or []
                except Exception:
                    continue
                for tbl in tables:
                    if not tbl:
                        continue
                    hidx = next((i for i, r in enumerate(tbl) if r and _row_is_table_header(r)), None)
                    if hidx is not None and header is None:
                        header = [(_join_cell(c) or f"col_{j}").strip()
                                  for j, c in enumerate(tbl[hidx])]
                        ncols = len(header)
                        header_seen = True
                        body = tbl[hidx + 1:]
                    elif header_seen:
                        # After the header is found, collect transaction rows from this
                        # table too (continuation pages, repeated headers, same template).
                        body = tbl[hidx + 1:] if hidx is not None else tbl
                    else:
                        continue
                    for r in body:
                        if not r:
                            continue
                        cells = [_join_cell(c) for c in r]
                        if _is_txn_data_row(cells):
                            row = (cells + [""] * ncols)[:ncols]  # rectangular to header width
                            data_rows.append(row)
            if header and data_rows:
                df = pd.DataFrame(data_rows, columns=header)
                logger.info("extractor_digital_pdf.extract_transaction_table_df: '%s' → "
                            "%d structured table rows, columns=%s",
                            Path(file_path).name, len(df), header)
                return df
    except Exception as e:
        logger.warning("extractor_digital_pdf.extract_transaction_table_df: failed for '%s': %s",
                       file_path, e)
    return None


def _extract_page_as_text(page, page_number: int = 1) -> str:
    """
    Extracts text from a single PDF page using the best available method.

    Algorithm:
        1. Always run extract_text() — on page 1 this gives us the account header /
           metadata block above the transaction table; on pages 2+ the output is NOT
           used as a prefix (see below).
        2. Build an inline narration supplement map from extract_text() so that
           narration fragments that pdfplumber's table extractor drops (because they
           share a y-coordinate with the date row) can be recovered.
        3. Try extract_tables(). If a table with ≥4 columns and ≥2 data rows is
           found, format its rows as flat text (multi-line cells joined) with the
           inline supplement applied.
        4. Compose the output:
               page 1  → metadata_lines (before first date row) + table_lines
               page 2+ → table_lines only
           Pages 2+ must NOT include a raw extract_text() prefix: for many bank
           layouts (e.g. IDFC First Bank) that prefix contains y-scrambled
           transaction data — pdfplumber emits characters in y-coordinate order,
           so the narration rows appear before their date rows in the text. This
           causes the standardiser to see every transaction twice: once scrambled
           (from the prefix) and once correct (from the table cells), producing the
           "first 7–8 transactions of a new page are wrong, then correct" symptom.
        5. If no suitable table is detected, return the extract_text() output as-is
           (handles cover pages, summary pages, and PDFs with no ruled tables).
    """
    full_text = page.extract_text() or ""

    try:
        tables = page.extract_tables()
        if not tables:
            return full_text

        # Pick the widest table (most columns in its widest row) that meets the
        # minimum size threshold. Widest = most likely to be the transaction ledger.
        def _table_score(t):
            if not t:
                return 0
            max_cols = max((len(r) for r in t if r), default=0)
            return max_cols * len(t)

        candidate = max(tables, key=_table_score, default=None)
        if candidate is None:
            return full_text

        # Check that the best candidate is wide and tall enough.
        max_cols = max((len(r) for r in candidate if r), default=0)
        data_rows = sum(1 for r in candidate
                        if r and any(_join_cell(c) for c in r))
        if max_cols < _MIN_TABLE_COLS or data_rows < _MIN_TABLE_ROWS:
            return full_text

        # Build the supplement map before converting the table, then filter out
        # entries whose text is already captured in a table narration cell. This
        # prevents timestamp collisions (two transactions at the same second) from
        # applying one transaction's supplement to another transaction's row.
        inline_map = _build_inline_narration_map(full_text)
        inline_map = _filter_inline_map(inline_map, candidate)

        table_lines = _table_to_lines(candidate, inline_map)
        if not table_lines:
            return full_text

        if page_number == 1:
            # Page 1: prepend only the metadata section (lines before the first
            # transaction date). These are never inside the table, so we need them
            # from extract_text(). Lines FROM the first date onward are NOT included
            # — the table covers all transactions.
            meta_lines = _metadata_lines(full_text)
            page_out = "\n".join(meta_lines) + "\n" + "\n".join(table_lines)
        else:
            # Pages 2+: return ONLY the table lines. The account metadata was
            # already emitted for page 1. Including a raw extract_text() prefix
            # here would duplicate transactions in scrambled y-order.
            page_out = "\n".join(table_lines)

        logger.debug(
            "extractor_digital_pdf._extract_page_as_text: "
            "page %d — table extraction (%d rows, %d cols), "
            "inline_map entries: %d",
            page_number, data_rows, max_cols, len(inline_map),
        )
        return page_out

    except Exception as exc:
        logger.debug(
            "extractor_digital_pdf._extract_page_as_text: "
            "page %d — table extraction failed (%s); falling back to extract_text().",
            page_number, exc,
        )
        return full_text


def extract_text_from_digital_pdf(file_path: str) -> str:
    """
    Extracts all text from a digital PDF bank statement.

    Uses a table-aware strategy: for pages that contain a bordered transaction
    table, pdfplumber's cell-level table extraction is used to correctly join
    multi-line narration cells before the text is handed to the downstream
    standardiser. This prevents the cross-row y-coordinate mixing problem where
    the last narration line of transaction N is emitted after the date line of
    transaction N+1, causing narration fragmentation and wrong-transaction attachment.

    Falls back to plain extract_text() for pages without a detectable table.

    Parameters:
        file_path (str): Absolute path to the digital PDF file.

    Returns:
        str: Full extracted text from all pages combined.
             Returns empty string if extraction fails completely.
    """
    logger.info(
        "extractor_digital_pdf.extract_text_from_digital_pdf: "
        "Starting extraction from '%s'",
        Path(file_path).name,
    )

    try:
        with pdfplumber.open(file_path) as pdf:
            total_pages = len(pdf.pages)
            logger.info(
                "extractor_digital_pdf.extract_text_from_digital_pdf: "
                "PDF has %d page(s)", total_pages,
            )

            page_texts = []
            for page_number, page in enumerate(pdf.pages, start=1):
                try:
                    page_text = _extract_page_as_text(page, page_number=page_number)
                    if page_text:
                        page_texts.append(page_text)
                        logger.debug(
                            "extractor_digital_pdf.extract_text_from_digital_pdf: "
                            "Page %d: %d characters", page_number, len(page_text),
                        )
                    else:
                        logger.debug(
                            "extractor_digital_pdf.extract_text_from_digital_pdf: "
                            "Page %d: no text found (blank or image-only page)",
                            page_number,
                        )
                except Exception as page_error:
                    logger.warning(
                        "extractor_digital_pdf.extract_text_from_digital_pdf: "
                        "Page %d extraction failed: %s. Skipping.",
                        page_number, page_error,
                    )
                    continue

            combined_text = _clean_pdf_text("\n".join(page_texts))
            logger.info(
                "extractor_digital_pdf.extract_text_from_digital_pdf: "
                "Extraction complete. Total characters: %d", len(combined_text),
            )
            return combined_text

    except FileNotFoundError:
        logger.error(
            "extractor_digital_pdf.extract_text_from_digital_pdf: "
            "File not found: %s", file_path,
        )
        return ""

    except Exception as error:
        logger.error(
            "extractor_digital_pdf.extract_text_from_digital_pdf: "
            "Unexpected error reading PDF '%s': %s", file_path, error,
        )
        return ""
