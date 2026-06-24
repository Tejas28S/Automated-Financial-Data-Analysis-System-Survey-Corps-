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

import pdfplumber

logger = logging.getLogger(__name__)

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
    all lines BEFORE the first transaction row (the first line that starts with a date).

    This is the only part of extract_text() we keep for table-extracted pages —
    the table cells give us the transactions, and the metadata header is never inside
    a table cell.
    """
    result = []
    for line in full_text.splitlines():
        if _DATE_START_RE.match(line.strip()):
            break
        result.append(line)
    return result[:60]  # safety cap against pathological layouts


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
