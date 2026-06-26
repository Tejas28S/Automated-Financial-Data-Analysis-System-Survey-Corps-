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

# Legal / disclaimer / footer boilerplate that banks print below or between the
# transaction rows (deposit-insurance notices, "computer generated statement",
# signature waivers, end-of-statement banners). These lines do NOT start with a
# date, so the continuation-stitching logic would otherwise glue them onto the
# preceding transaction's narration (observed in real statements). The phrases are
# generic banking/legal boilerplate — NOT any specific bank's name — so the
# anti-overfitting guard is unaffected. We only ever drop a line that matches one
# of these AND does not start with a date, so a real transaction can't be removed.
_FOOTER_NOISE = re.compile(
    r"computer[ \-]generated|system[ \-]generated|"
    r"requires?\s+no\s+signature|does\s+not\s+require\s+(?:a\s+|any\s+)?signature|"
    r"each\s+depositor|deposit\s+insurance|\bDICGC\b|insured\s+up\s+to|"
    r"schedule\s+of\s+charges|most\s+important\s+document|"
    r"constituent\s+notifies|discrepancy\s+in\s+this\s+statement|"
    r"transaction\(s\)\s+in\s+the\s+statement|"
    r"end\s+of\s+statement|closing\s+balance\s+as\s+on|"
    r"this\s+is\s+a\s+(?:computer|system)|"
    # ── report-style page furniture / footer (e.g. IDBI report exports) ──
    r"end\s+of\s+report|pages?\s+printed|chief\s+manager|manager\s*/\s*chief|"
    r"report\s+for\s+the\s+period|report\s+to\s*:|service\s+out\s*let|"
    r"account\s+opening\s+balance|brought\s+forward|carried\s+forward|"
    r"total\s*\(\s*curr|grand\s+total|opening\s+balance\s*:|"
    r"https?://|www\.|\bsignature\b|"
    # A standalone print-date label line ("Date :23-09-2025") in a report footer.
    # Requires the label at the START followed by ':'/'-' then a date — so it never
    # matches a column-header "Date Tran ..." (no separator) or a real transaction.
    r"^date\s*[:=\-]\s*\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}\s*$",
    re.IGNORECASE,
)
# A separator line of dashes / underscores / equals (table rules, page rules).
_SEPARATOR_LINE = re.compile(r"^[\s\-_=*~.]{6,}$")

# Pre-date leading token shapes: some bank exports print an Account Number or a
# long reference code as the FIRST column, before the transaction date (e.g.
# "216655101347 22-Jan-2025 …"). These have 5+ digit numeric values and are NOT
# dates, so DATE_AT_LINE_START_PATTERN misses them. The guard on the RIGHT side
# (DATE_AT_LINE_START_PATTERN.match on the remainder) ensures we only strip when
# a real date immediately follows, so no narration word can accidentally be eaten.
_PRE_DATE_LONG_ID_RE = re.compile(r"^\d{5,}$")

# FLEXCUBE (AU Small Finance Bank online portal) export format: columns include
# "Trans.LCY(INR)" and "Trans.Rate"; the Running Balance is on its own next line.
# Detected from the header text alone — no bank name ever special-cased.
_TRANS_LCY_HEADER_RE = re.compile(r"\bTrans\.?\s*LCY\b", re.IGNORECASE)
# A line that is ONLY a monetary amount (digits + 2-decimal fraction, optional commas
# and leading sign/currency): used to recognise a standalone Running Balance
# continuation line in FLEXCUBE statements. Only matched when we already know the
# format is FLEXCUBE AND the current record has no Balance yet, so a real narration
# number can never be mistaken for a balance.
_PURE_BALANCE_LINE_RE = re.compile(r"^\s*[-₹]*[\d,]+\.\d{2}\s*$")

# Markers that START a page-summary / statement-summary / footer BLOCK. Once one of
# these appears, every following line up to the next real transaction (a date-started
# line) is page metadata — counts, totals, carried-forward values, toll-free numbers,
# signatures — NOT transaction data. Filtering individual lines is not enough here
# because the block also contains bare number lines ("1,14,098.00") and count lines
# ("Dr. Count 19") that match no single keyword; block detection removes the whole
# section in one go. Generic banking vocabulary only — no bank identity.
_FOOTER_BLOCK_START = re.compile(
    r"\bpage\s+summary\b|\bstatement\s*summary\b|\bstatement\s+information\b|"
    r"\bcarried\s+forward\b|\bc/?f\s+total\b|\bpage\s+total\b|\bgrand\s+total\b|"
    r"\bend\s+of\s+statement\b|\bend\s+of\s+report\b|\btoll\s*free\b|"
    r"\bdr\.?\s*count\b|\bcr\.?\s*count\b|\btotal\s+debits?\b|\btotal\s+credits?\b|"
    r"\bgenerated\s+on\b|\bchief\s+manager\b",
    re.IGNORECASE,
)

# Standard column-header vocabulary (generic banking terms — NOT any bank's name, so
# the anti-overfitting guard is unaffected). A line carrying several of these and NO
# money amount is a repeated column-header row we should drop before parsing.
_HEADER_WORDS = ("date", "narration", "description", "particulars", "remarks",
                 "debit", "withdrawal", "credit", "deposit", "balance",
                 "cheque", "chq", "ref", "branch", "value", "txn", "transaction")


def _is_money_token(token: str) -> bool:
    """True if the token looks like a monetary amount (has a 2-digit decimal)."""
    return bool(_MONEY_TOKEN.match(token.strip()))


# A short token that some layouts print as a trailing column AFTER the balance, which
# is NOT a money token (no decimals) and would otherwise make the from-the-end amount
# peeler see "no amounts" and drop the whole row. Two generic, bank-agnostic shapes:
#   • a short bare-numeric code — an "Init. Br" branch / posting code (2535, 248, 100)
#   • a transaction-CHANNEL / mode code — the standard last column on some layouts
#     (UPI, NEFT, IMPS, RTGS, ATM, POS, CLG, …). Generic banking vocabulary, NOT any
#     bank's name, so the anti-overfitting guard is unaffected.
# We only ever skip such a token when doing so reveals a proper amount+balance pair, so
# this never eats a narration word.
_TRAILING_CODE_RE = re.compile(r"^\d{1,6}$")
_TRAILING_CHANNEL_RE = re.compile(
    r"^(?:UPI|NEFT|IMPS|RTGS|ATM|POS|CLG|ECS|NACH|ACH|MOB|INB|BIL|CASH|TFR|CHQ|"
    r"EMI|IFT|VMT|EDC|SETU|ABB|CWDR|PUR|MPS|FT|"
    # Compound channel tokens printed as "CODE/CODE" or "CODE-CODE" (e.g. ATM/POS).
    r"ATM/POS|ATM-POS|NET-BKG|INT-BKG|MOB-BKG|NET/BKG|INT/BKG|MOB/BKG)$",
    re.IGNORECASE,
)
# Generic "label-like" token: short, contains at least one letter, no decimal point.
# Matches branch codes (1701-BEHROR, 9999-CENTRAL), compound channel words (Banking,
# OFF) that are not in _TRAILING_CHANNEL_RE. Pure-digit strings are excluded so that
# reference numbers that happen to be the last token are never silently eaten.
_POTENTIAL_TRAILING_LABEL_RE = re.compile(r"^[A-Za-z0-9/\-_]{1,20}$")
# At most this many trailing non-money code columns are skipped. Three handles the
# longest observed compound channel: "9008-NEFT/ RTGS CELL" (3 tokens).
_MAX_TRAILING_CODES = 3


def _is_trailing_code(tok: str) -> bool:
    """True if a token is a short non-money trailing CODE column (numeric or channel)."""
    if _TRAILING_CODE_RE.match(tok) or _TRAILING_CHANNEL_RE.match(tok):
        return True
    # Short label-like token with at least one letter: handles branch codes
    # (1701-BEHROR), compound channel words (Banking, OFF) and similar trailing
    # metadata columns that vary by bank export. Only adopted when the guard in
    # _peel_trailing_amounts confirms ≥2 money tokens are exposed after stripping,
    # so a genuine narration word at end of a balance-terminated line is never lost.
    if "." not in tok and any(c.isalpha() for c in tok) and _POTENTIAL_TRAILING_LABEL_RE.match(tok):
        return True
    return False


def _peel_trailing_amounts(tokens: List[str]) -> List[str]:
    """
    Peels the trailing run of monetary tokens off `tokens` (MUTATING it) and returns
    them left-to-right. Up to 3 are taken (separate Debit | Credit | Balance columns).

    Layout robustness (generic, bank-agnostic — keys on token SHAPE, never identity):
    some statements print a short non-money CODE column AFTER the balance (e.g. Axis
    prints an "Init. Br" branch code; some banks print a trailing "Channel" column like
    UPI/NEFT). The balance is then no longer the last token, so a naive from-the-end
    peel finds nothing and the row is dropped. To handle that, if the line does NOT
    already end in a money token we skip up to _MAX_TRAILING_CODES short trailing code
    tokens — but ONLY when that exposes at least two trailing money tokens (amount +
    balance). The guard means a row genuinely ending in narration (no trailing balance)
    is never mis-peeled, so statements that already parse are completely unaffected
    (their last token is money, so the skip block is never entered).

    Empty-column dash placeholder: some bank exports print a lone '-' token for the
    zero debit or credit column (e.g. "800.00 - 1,700.00 UPI"). The guard is extended
    to accept '-' in the position of the second amount, and the peeling loop converts
    such a '-' to '0.00' once at least one real amount has already been seen to its
    right. This keeps the narration area completely unaffected: a '-' in the narration
    always comes before any amounts, so `money` is still empty when we'd try to consume
    it and the elif guard (`and money`) blocks it.
    """
    # Normalize leading-dot decimals in the trailing token positions (.00 → 0.00, .50 → 0.50).
    # Some banks (e.g. HDFC) print a zero balance as ".00" rather than "0.00". Without
    # normalization the token fails _is_money_token (which requires at least one digit
    # before the dot), so the whole row is dropped. Only the last 4 positions are touched —
    # the balance, optional amount, and up to 2 trailing codes — so a .dd fragment
    # buried in the middle of a narration is never affected.
    _leading_dot_re = re.compile(r'^\.\d{1,2}$')
    for _i in range(max(0, len(tokens) - 4), len(tokens)):
        if _leading_dot_re.match(tokens[_i]):
            tokens[_i] = '0' + tokens[_i]

    if tokens and not _is_money_token(tokens[-1]):
        skip = 0
        while (skip < _MAX_TRAILING_CODES and len(tokens) > skip
               and not _is_money_token(tokens[-1 - skip])
               and _is_trailing_code(tokens[-1 - skip])):
            skip += 1
        # Adopt the skip if it reveals balance + (amount OR empty-column dash).
        if (skip and len(tokens) >= skip + 2
                and _is_money_token(tokens[-1 - skip])
                and (_is_money_token(tokens[-2 - skip])
                     or tokens[-2 - skip] == '-')):
            del tokens[len(tokens) - skip:]

    money: List[str] = []
    _interleaved_skipped = False
    while tokens and len(money) < 3:
        tok = tokens[-1]
        if _is_money_token(tok):
            money.insert(0, tokens.pop())
            _interleaved_skipped = False  # reset: each inter-amount gap may skip once
        elif tok == '-' and money:
            # Lone dash as empty-column placeholder, consumed only after seeing ≥1 amount.
            tokens.pop()
            money.insert(0, '0.00')
            _interleaved_skipped = False
        elif (len(money) == 1 and not _interleaved_skipped
              and (_is_trailing_code(tok) or tok == ':')):
            # A single non-money watermark fragment stuck BETWEEN the running balance and
            # the transaction amount (e.g. "Na", ":", "R" from a page-header watermark
            # that bleeds across the transaction table). Consume it without adding to money
            # so the peeler can reach the amount token on the other side. Guard: only when
            # exactly 1 money token (balance) has been peeled so far, and only once per gap,
            # so a narration word before the amounts is never accidentally stripped.
            tokens.pop()
            _interleaved_skipped = True
        else:
            break
    return money


def _peel_leading_amounts(tokens: List[str]) -> List[str]:
    """
    Peels the LEADING run of monetary tokens off the FRONT of `tokens` (MUTATING it)
    and returns them left-to-right. Up to 3 are taken (Debit | Credit | Balance).

    This is the mirror of _peel_trailing_amounts, for NARRATION-LAST layouts that print
    the amount columns right after the date and the free-text narration LAST, e.g.:
        31-01-2025  1000.00  1000.00 Cr.  Transfer From A/C…
    For these the running balance is the LAST amount token of the leading run and the
    remaining tokens are the narration. It is only ever called as a fallback when the
    from-the-end peel found nothing (see _parse_single_transaction_line), so a normal
    balance-last statement — whose first non-date token is narration text, not money —
    never enters this path and is completely unaffected. Bank-agnostic (token shape only).
    """
    lead: List[str] = []
    while tokens and _is_money_token(tokens[0]) and len(lead) < 3:
        lead.append(tokens.pop(0))
    return lead


def count_transaction_like_lines(raw_text: str) -> int:
    """
    Estimate of how many lines in the raw text LOOK like transaction rows: a line
    that starts with a date (or a short leading serial/account token followed by a
    date) AND carries at least one monetary amount (decimal-based, 2dp).

    Requiring DATE_AT_LINE_START_PATTERN (rather than a date anywhere on the line)
    avoids counting metadata header lines like "A/COpenDate : 02/11/2020 AQB:
    10,000.00" — those have the date buried in the middle. One money token is
    sufficient: some formats print only the running balance with a decimal (e.g.
    "5.82") while the debit/credit amount is a bare integer that the strict
    _is_money_token misses. Two-token filtering caused such formats to return 0
    expected lines, which suppressed LLM escalation on statements that need it.

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
        if DATE_AT_LINE_START_PATTERN.match(s):
            if any(_is_money_token(t) for t in s.split()):
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

    Page-summary / statement-summary / footer SECTIONS are removed as a whole BLOCK:
    once a block-start marker (e.g. "Page Summary", "Carried Forward", "Dr. Count")
    is seen, every following line is dropped until the next real transaction (a
    date-started line) resumes on the following page. This catches the bare number
    and count lines inside such a block that no per-line keyword would match — while
    still never removing a transaction row, because a date-started line always ends
    the block.
    """
    out = []
    in_footer_block = False
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        is_date_start = bool(DATE_AT_LINE_START_PATTERN.match(s))

        # Inside a page-summary/footer block: keep dropping until a transaction
        # (date-started line) resumes, then leave the block and process that line.
        if in_footer_block:
            if is_date_start:
                in_footer_block = False
            else:
                continue

        # A block-start marker (never a date-started line) opens the footer block.
        if not is_date_start and _FOOTER_BLOCK_START.search(s):
            in_footer_block = True
            continue

        if _PAGE_NOISE.match(s) or _is_header_line(s):
            continue
        # Drop pure separator rules (dashes/underscores) — they would otherwise be
        # stitched into the preceding transaction's narration.
        if _SEPARATOR_LINE.match(s):
            continue
        # Drop individual legal/disclaimer/footer/report boilerplate lines.
        # Real transactions always carry ≥2 money tokens, so the _money_tok_count < 2
        # guard is sufficient to protect them. The date-start guard is relaxed for
        # zero-money-token lines: browser-printed netbanking PDFs embed their page URL
        # and a print timestamp (e.g. "11/28/25, 3:40 PM blob:https://…") that matches
        # DATE_AT_LINE_START_PATTERN on the timestamp but carries no amounts and must be
        # dropped so the FLEXCUBE balance-continuation is not displaced.
        _money_tok_count = sum(1 for _t in s.split() if _is_money_token(_t))
        if (_FOOTER_NOISE.search(s) and _money_tok_count < 2
                and (not is_date_start or _money_tok_count == 0)):
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
    "Date", "Time", "Narration",
    "Transaction_ID", "Reference_Number", "Transaction_Reference", "Cheque_Number",
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

# ── Leading id-column support (Transaction Id / Reference No printed as columns) ──
# Some statements print dedicated id columns BETWEEN the date and the narration:
#     Date | Tran Id | Ref Num | Particulars | Debit | Credit | Balance
# This is detected from the HEADER (never from a bank name) and the matching tokens
# are peeled off the FRONT of each row into Transaction_ID / Reference_Number. An
# id-shaped token is required, so a row that lacks the columns is never corrupted.
#
# id-shaped token: optional 0–4 letter prefix, then ≥3 digits, then optional
# alphanumerics and an optional "/NN" or "-NN" suffix. Matches "S19547297",
# "6533157212", "0214713/25"; does NOT match a narration token like "UPI/506.../X"
# (which starts with letters immediately followed by "/").
_ID_TOKEN_RE = re.compile(r"^[A-Za-z]{0,4}\d{3,}[A-Za-z0-9]*(?:[/\-]\d{1,4})?$")


def _detect_leading_id_columns(raw_lines: List[str]) -> List[str]:
    """
    Reads the transaction-table HEADER to see whether dedicated id columns are
    printed BEFORE the narration column, and returns them in print order — a subset
    of ["transaction_id", "reference_number"]. Returns [] when the statement has no
    such columns (the overwhelmingly common case), so the default parser is used
    unchanged. Driven purely by header text — no bank identity is involved.
    """
    region = "\n".join(raw_lines[:22]).lower()
    narr_positions = [region.find(w) for w in ("particular", "narration", "description")
                      if region.find(w) >= 0]
    if not narr_positions:
        return []
    narr_pos = min(narr_positions)

    # "Tran Id" / "Transaction Id" / "Transn Id" / "Txn Id", or a standalone "Tran"
    # that does not mean a date column.
    txn_m = re.search(
        r"\btrans(?:action|n)?\s*id\b|\btxn\s*id\b|\btran\s*id\b|\btran\b(?!\s*date)",
        region)
    ref_m = re.search(r"\bref(?:erence)?\s*(?:no|num|number)\b|\brrn\b|\butr\b", region)

    found = []
    if txn_m and txn_m.start() < narr_pos:
        found.append(("transaction_id", txn_m.start()))
    if ref_m and ref_m.start() < narr_pos:
        found.append(("reference_number", ref_m.start()))
    found.sort(key=lambda x: x[1])
    return [name for name, _ in found]


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


def _try_fill_amounts(line: str, require_balance: bool) -> dict:
    """
    Tries to extract the transaction amounts from a CONTINUATION line that belongs
    to a date-started record whose amounts are still pending (Bug A fix).

    A continuation line supplies amounts when it ends with ≥2 money tokens (if
    require_balance=True) or ≥1 (if require_balance=False). Any non-money prefix
    text on the line is returned as narration_prefix so it can be appended to the
    record's narration rather than lost.

    Returns a dict {"amount", "Balance", "narration_prefix"} on success, or None
    if this line does NOT look like an amounts line.
    """
    tokens = line.split()
    money = _peel_trailing_amounts(tokens)

    if require_balance:
        if len(money) < 2:
            return None
        balance_str = money[-1]
        amount_str = money[-2]
    else:
        if len(money) < 1:
            return None
        balance_str = ""
        amount_str = money[-1]

    return {
        "amount": _clean_amount(amount_str),
        "Balance": balance_str,
        "narration_prefix": " ".join(tokens).strip(),
    }


# ── Header-driven column parser ───────────────────────────────────────────────
# Some statements print the columns in an order the positional (balance-from-end)
# parser cannot read — most importantly when the NARRATION column is printed LAST
# and each row begins with a transaction-id instead of a date, e.g.:
#     Tran_ID  Tran_Date  Dr_Amt  Cr_Amt  Balance  Narration
#     M3300329 06-10-2022 0       50000   50000    TRFR FROM: SUNITA KAVYA SETHI
# For these the date is not at the line start and the money columns are in the
# MIDDLE, so the positional parser yields zero rows (and the pipeline would then
# escalate to the LLM and hit token limits). When a header row is present we can
# read the column ORDER from it and parse each row by walking that order. This is
# fully bank-agnostic: it is driven entirely by the statement's own header text.

# A loose amount token for the header parser: bare integers, decimals, Indian
# commas, optional leading minus, optional CR/DR suffix. Unlike _MONEY_TOKEN this
# does NOT require a decimal part, because dedicated debit/credit columns often
# print whole-rupee amounts ("0", "50000").
_LOOSE_AMOUNT_RE = re.compile(
    r"^-?(?:\d{1,3}(?:,\d{2,3})*|\d+)(?:\.\d{1,2})?\s*\(?(?:CR|DR)?\)?$",
    re.IGNORECASE,
)
# A single date token (anchored). Reuses the same shapes as _DATE_ANYWHERE_PATTERN.
_DATE_TOKEN_RE = re.compile(
    r"^(?:\d{1,2}[/\-.][0-9A-Za-z]{2,9}[/\-.]\d{2,4}|\d{4}[/\-.]\d{1,2}[/\-.]\d{1,2})$"
)


def _classify_header_token(tok: str) -> str:
    """Maps one header-cell word to a column role. Generic banking vocabulary only."""
    low = tok.lower()
    if any(w in low for w in ("narration", "description", "particular",
                              "remarks", "detail")):
        return "narration"
    if "balance" in low:
        return "balance"
    if "withdraw" in low or "debit" in low or low == "dr" or low.startswith("dr_") \
            or low.startswith("dr ") or low.startswith("dramt") or low.startswith("dr."):
        return "debit"
    if "deposit" in low or "credit" in low or low == "cr" or low.startswith("cr_") \
            or low.startswith("cr ") or low.startswith("cramt") or low.startswith("cr."):
        return "credit"
    if "value" in low and "date" in low:
        return "value_date"
    if "date" in low:
        return "date"
    if "time" in low:
        return "time"
    if "cheque" in low or "chq" in low or "instrument" in low:
        return "cheque"
    if "ref" in low or low.endswith("id") or "utr" in low or "rrn" in low \
            or "txn" in low or "serial" in low or low in ("sr", "srno", "sno"):
        return "ref"
    return "other"


def _find_header_row(lines: List[str]):
    """
    Locates the transaction-table HEADER row near the top of the text and returns
    (index, roles) where roles is the ordered list of column roles. Returns
    (None, None) if no usable header is found.

    A usable header: has a date column, a narration column, and at least one of
    debit/credit/balance; contains no actual amount VALUE (so a data row is not
    mistaken for a header); and — critically — the narration is the LAST data
    column. The narration-last constraint is what makes front-to-back walking
    unambiguous; statements with narration in the middle are left to the positional
    parser (which already handles them).
    """
    for i, ln in enumerate(lines[:40]):
        s = ln.strip()
        if not s:
            continue
        toks = [t for t in re.split(r"\s+", s) if t]
        if len(toks) < 4:
            continue
        roles = [_classify_header_token(t) for t in toks]
        role_set = set(roles)
        if not ("date" in role_set and "narration" in role_set
                and ({"debit", "credit", "balance"} & role_set)):
            continue
        # A header row carries no real amount value.
        if any(_LOOSE_AMOUNT_RE.match(t) and any(c.isdigit() for c in t) for t in toks):
            continue
        # Narration must be the last amount/text column (nothing numeric after it).
        narr_idx = roles.index("narration")
        if {"debit", "credit", "balance"} & set(roles[narr_idx + 1:]):
            continue
        return i, roles
    return None, None


def _row_by_roles(toks: List[str], roles: List[str], account_id: str,
                  bank_name: str):
    """
    Parses one tokenised line according to the header column ROLES (narration is
    guaranteed last by _find_header_row). Returns a record dict, or None if the
    line does not fit the role layout (→ it is a narration continuation line).
    """
    i = 0
    date_str = time_str = balance_str = narration = ""
    debit = credit = None
    for role in roles:
        if role == "narration":
            narration = " ".join(toks[i:]).strip()
            i = len(toks)
            break
        if i >= len(toks):
            return None
        tok = toks[i]
        if role in ("date", "value_date"):
            if not _DATE_TOKEN_RE.match(tok):
                return None  # expected a date here → this is not a new row
            if role == "date":
                date_str = tok
            i += 1
        elif role == "time":
            if _TIME_ONLY_LINE.match(tok):
                time_str = tok
                i += 1  # time is optional; only consume if present
        elif role in ("debit", "credit", "balance"):
            if not _LOOSE_AMOUNT_RE.match(tok):
                return None  # expected an amount here → not a data row
            if role == "debit":
                debit = _clean_amount(tok)
            elif role == "credit":
                credit = _clean_amount(tok)
            else:
                balance_str = tok
            i += 1
        else:  # ref / cheque / other → consume one positional token if present
            i += 1

    if not date_str:
        return None

    # Derive the single amount + explicit direction from the debit/credit columns.
    d = debit or 0.0
    c = credit or 0.0
    if d > 0 and c == 0:
        amount, direction = d, "D"
    elif c > 0 and d == 0:
        amount, direction = c, "C"
    elif d > 0 and c > 0:
        amount, direction = (d, "D") if d >= c else (c, "C")
    else:
        amount, direction = 0.0, ""

    return {
        "Date": date_str,
        "Time": time_str,
        "Narration": narration,
        "amount": amount,
        "dr_cr": direction,
        "Balance": balance_str,
        "Account_ID": account_id,
        "Bank_Name": bank_name,
    }


def _parse_by_header(lines: List[str], roles: List[str], account_id: str,
                     bank_name: str) -> list:
    """
    Parses all transaction rows using the column ROLES read from the header. Lines
    that do not fit the role layout are treated as narration continuations of the
    preceding transaction (multi-line narration support). `lines` should already be
    noise-stripped (page furniture, repeated headers and footer boilerplate removed).
    """
    records = []
    current = None
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        toks = s.split()
        rec = _row_by_roles(toks, roles, account_id, bank_name)
        if rec is not None:
            if current:
                records.append(current)
            current = rec
        elif current is not None:
            current["Narration"] = (current.get("Narration", "") + " " + s).strip()
    if current:
        records.append(current)
    return records


def _detect_trans_lcy_format(raw_lines: List[str]) -> bool:
    """True when the raw line set contains a Trans.LCY column header (FLEXCUBE portal format)."""
    for line in raw_lines[:60]:
        if _TRANS_LCY_HEADER_RE.search(line):
            return True
    return False


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
    raw_lines = [ln for ln in (raw_text or "").splitlines() if ln.strip()]
    # Detect the column-header row (and its column order) BEFORE noise stripping —
    # _strip_noise_lines removes header rows, but we need the header to learn the
    # column layout for the header-driven fallback parser further below.
    header_idx, header_roles = _find_header_row(raw_lines)
    # Detect dedicated leading id columns (Tran Id / Ref No printed between the date
    # and the narration). Generalised from the header text — [] for the vast majority
    # of statements, so the default parser path is unchanged.
    leading_id_columns = _detect_leading_id_columns(raw_lines)
    if leading_id_columns:
        logger.info(
            "standardiser.standardise_digital_pdf_transactions: detected leading id "
            "columns from header: %s", leading_id_columns)
    # FLEXCUBE portal format: Trans.LCY(INR) + Trans.Rate columns; Running Balance on
    # its own continuation line. Detected from the header — no bank name used.
    has_trans_lcy = _detect_trans_lcy_format(raw_lines)
    if has_trans_lcy:
        logger.info(
            "standardiser.standardise_digital_pdf_transactions: "
            "Trans.LCY format detected — FLEXCUBE balance-on-next-line mode active.")
    # Drop page furniture (page numbers, repeated column-header rows) so a multi-page
    # statement's repeating header/footer can't be stitched into a real transaction.
    lines = _strip_noise_lines(raw_lines)

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
    # Two classes of multi-line narration bugs are handled here:
    #
    # Bug A — amounts on a continuation line (not on the date line). Some PDFs
    # (or some pdfplumber extraction modes) put the amounts column on a different
    # physical line than the date. The old code returned None from
    # _parse_single_transaction_line when there were no money tokens, setting
    # current=None and silently dropping ALL continuation lines for that transaction.
    # Fix: return a PARTIAL record (amounts_pending=True) when the date line has no
    # money tokens, then fill amounts from the first continuation line that does.
    #
    # Bug B — cross-row y-coordinate mixing — is fixed upstream in the extractor
    # (table-aware cell extraction). The continuation stitching here is a defensive
    # second layer that still handles whatever text comes in.
    records = []
    current = None
    for line in lines:
        s = line.strip()
        # ── Pre-date long-ID stripping ─────────────────────────────────────────
        # Some bank exports print a long numeric column (Account No, Reference)
        # BEFORE the transaction date (e.g. "216655101347 22-Jan-2025 …").
        # DATE_AT_LINE_START_PATTERN only allows 1–4 digit leading serials; a
        # 5+ digit number fails the match and the whole line is silently dropped.
        # Strip up to 2 such leading tokens when they are purely numeric (≥5
        # digits) and a real date immediately follows — so no narration word can
        # accidentally be eaten and existing statements are completely unaffected.
        if not DATE_AT_LINE_START_PATTERN.match(s):
            toks = s.split()
            for n_pre in range(1, min(3, len(toks))):
                rest_candidate = " ".join(toks[n_pre:])
                if (DATE_AT_LINE_START_PATTERN.match(rest_candidate)
                        and all(_PRE_DATE_LONG_ID_RE.match(toks[i]) for i in range(n_pre))):
                    s = rest_candidate
                    break
        if DATE_AT_LINE_START_PATTERN.match(s):
            if current:
                # Only keep records that received amounts (either on the date line
                # or from a subsequent continuation line).
                if not current.get("_amounts_pending"):
                    records.append(current)
            current = _parse_single_transaction_line(
                s, require_balance=require_balance,
                leading_id_columns=leading_id_columns,
                has_trans_lcy=has_trans_lcy)
            if current:
                current["Account_ID"] = account_id
                current["Bank_Name"] = bank_name
        elif current is not None and narration_wraps:
            if _TIME_ONLY_LINE.match(s):
                if not current.get("Time"):
                    current["Time"] = s
            elif current.get("_amounts_pending"):
                # This record still needs its amounts. Check whether THIS
                # continuation line supplies them (has ≥2 money tokens at the end).
                pending = _try_fill_amounts(s, require_balance)
                if pending:
                    current["amount"] = pending["amount"]
                    current["Balance"] = pending["Balance"]
                    current["_amounts_pending"] = False
                    # Any non-amount text on the amounts line belongs in narration.
                    if pending.get("narration_prefix"):
                        current["Narration"] = (
                            current.get("Narration", "") + " " + pending["narration_prefix"]
                        ).strip()
                else:
                    # No amounts yet — still narration text.
                    current["Narration"] = (current.get("Narration", "") + " " + s).strip()
            elif (has_trans_lcy
                  and not current.get("Balance")
                  and _PURE_BALANCE_LINE_RE.match(s)):
                # FLEXCUBE: the Running Balance is printed on its own line directly
                # after each transaction row. Only match when Balance is still empty
                # so a real narration number in a non-FLEXCUBE doc can't land here.
                current["Balance"] = s.strip()
            else:
                current["Narration"] = (current.get("Narration", "") + " " + s).strip()
    if current and not current.get("_amounts_pending"):
        records.append(current)

    # ── Header-driven fallback ────────────────────────────────────────────────
    # If a usable column header was detected (narration printed last) and the
    # positional parser under-produced, re-parse by the header's column order. We
    # only adopt the header parse when it recovers MORE rows, so statements the
    # positional parser already handles correctly are never disturbed. This is what
    # rescues layouts like "Tran_ID Date Dr Cr Balance Narration" (0 positional rows)
    # WITHOUT any LLM call — which is also why those statements no longer hit the
    # token-limit (413) errors on the LLM fallback path.
    if header_roles is not None:
        hdr_records = _parse_by_header(lines, header_roles, account_id, bank_name)
        if len(hdr_records) > len(records):
            logger.info(
                "standardiser.standardise_digital_pdf_transactions: header-driven "
                "parse recovered %d rows (positional parser got %d) — using header parse.",
                len(hdr_records), len(records),
            )
            records = hdr_records

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

    # ── 4. Identifiers ────────────────────────────────────────────────────────
    # Transaction_Reference = an id parsed OUT OF the narration (UPI/UTR/IMPS id).
    # Cheque_Number         = a cheque/instrument number from the narration.
    # Transaction_ID / Reference_Number were already peeled per-row from the
    # statement's dedicated columns (when the header declared them) and live on the
    # records; we only ensure the columns exist and never overwrite them here.
    has_ref = bool(schema.get("has_reference_number", True))
    has_chq = bool(schema.get("has_cheque_number", True))
    txn_refs, cheques = [], []
    for narr in df["Narration"].tolist():
        r, c = _extract_ref_cheque(str(narr), has_ref, has_chq)
        txn_refs.append(r)
        cheques.append(c)
    df["Transaction_Reference"] = txn_refs
    df["Cheque_Number"] = cheques
    if "Transaction_ID" not in df.columns:
        df["Transaction_ID"] = ""
    if "Reference_Number" not in df.columns:
        df["Reference_Number"] = ""

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
            # The LLM returns its understanding of a reference as "reference_number";
            # under the new field semantics that is a narration-derived identifier, so
            # it maps to Transaction_Reference. A dedicated id column, if the LLM
            # reports one, maps to Transaction_ID / Reference_Number.
            "Transaction_ID": t.get("transaction_id", ""),
            "Reference_Number": t.get("reference_no_column", ""),
            "Transaction_Reference": t.get("reference_number", ""),
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
    for col in ["Time", "Narration", "Transaction_ID", "Reference_Number",
                "Transaction_Reference", "Cheque_Number",
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
        # Use absolute values for the magnitude — some bank exports print debits as
        # negative numbers (e.g. "-42.37") in the amount column. abs() ensures the
        # direction-correction arithmetic is correct regardless of the sign convention.
        d_raw = row["Debit"] or 0.0
        c_raw = row["Credit"] or 0.0
        amount = abs(d_raw) if d_raw else abs(c_raw)
        d, c = d_raw, c_raw
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


# Header keywords that denote the bank's own per-row TRANSACTION ID column (distinct
# from a transaction reference). Generic vocabulary, no bank names.
_TXN_ID_HINTS = ("tranid", "txnid", "transactionid")


def _match_transaction_id_column(columns, exclude=None):
    """
    Finds, by MEANING, a dedicated Transaction-ID column ("Tran Id", "Txn ID",
    "Transaction ID"). A column that also mentions "ref" is treated as a reference,
    not a transaction id, so "Ref Txn No" is never misfiled here. Returns the column
    name or None. Generalised by label, never by bank.
    """
    exclude = set(c for c in (exclude or []) if c is not None)

    def norm(c):
        return re.sub(r"[^a-z0-9]", "", str(c).lower())

    for col in columns:
        if col in exclude:
            continue
        n = norm(col)
        if n and any(h in n for h in _TXN_ID_HINTS) and "ref" not in n:
            return col
    return None


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

    # Avoid a duplicate-column crash after rename. Some statements carry BOTH a
    # "Date" and a "Value Date" column (or "Balance" and "Closing Balance"); when the
    # mapper points the standard field at one of them, the OTHER raw column whose
    # literal name already equals the rename target would survive, producing two
    # identically-named columns. df["Date"] would then return a DataFrame and crash
    # date parsing (the whole file failed with 0 rows). We drop the un-mapped raw
    # column whose name collides with a rename target before renaming. Generalised —
    # no bank or column is special-cased.
    rename_targets = set(column_rename.values())
    collide = [c for c in raw_df.columns
               if c not in column_rename and str(c) in rename_targets]
    source_df = raw_df.drop(columns=collide) if collide else raw_df

    # Create a copy with renamed columns
    df = source_df.rename(columns=column_rename).copy()
    # Belt-and-braces: if any duplicate column names remain, keep the first.
    if df.columns.duplicated().any():
        df = df.loc[:, ~df.columns.duplicated()]

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

    # Transaction_ID (a dedicated per-row id column) and Transaction_Reference (an id
    # parsed out of narration text) are populated by the text-PDF path; for a direct
    # Excel/CSV DataFrame they stay blank unless a dedicated column is matched. They
    # are added here so the schema is consistent across every source.
    tranid_src = _match_transaction_id_column(
        raw_df.columns, exclude=set(column_rename.keys()) | {ref_src, chq_src})
    df["Transaction_ID"] = (df[tranid_src].map(_clean_identifier)
                            if tranid_src is not None and tranid_src in df.columns else "")
    df["Transaction_Reference"] = ""

    # Clean and type each column
    df = _clean_and_type_columns(df)

    # ── DR/CR indicator split ──────────────────────────────────────────────────
    # When the column_map points BOTH debit and credit at the same source column
    # (common in internal bank exports: one amount column + a 'D'/'C' direction
    # flag), the rename loop overwrites the key so only one side survives and
    # every row gets the same amount in both Debit and Credit (or one side is NaN).
    # Fix: find a direction column whose values are exclusively 'D'/'C' (etc.) and
    # use it to route the amount to the correct side. Generalised — no bank name.
    _debit_src  = column_map.get("debit")
    _credit_src = column_map.get("credit")
    if (_debit_src is not None and _credit_src is not None
            and str(_debit_src).strip() == str(_credit_src).strip()):
        dir_col = _find_drcr_indicator_column(raw_df)
        if dir_col is not None:
            # After _clean_and_type_columns both Debit and Credit are floats.
            # The amount landed in whichever side the rename kept; take the max.
            raw_amt = df[["Debit", "Credit"]].max(axis=1)
            dir_series = (
                raw_df[dir_col]
                .astype(str).str.strip().str.upper()
                .reset_index(drop=True)
            )
            is_credit = dir_series.isin(["C", "CR", "CREDIT"])
            df["Credit"] = np.where(is_credit, raw_amt, 0.0)
            df["Debit"]  = np.where(~is_credit, raw_amt, 0.0)
            logger.info(
                "standardiser.standardise_dataframe_direct: split single-amount "
                "column by DR/CR indicator %r for account %r", dir_col, account_id)

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

# Values that identify a DR/CR direction indicator column.
_DRCR_INDICATOR_VALS = {"D", "C", "DR", "CR", "DEBIT", "CREDIT"}


def _find_drcr_indicator_column(df: pd.DataFrame) -> Optional[str]:
    """
    Returns the name of the first column whose non-null values are exclusively
    DR/CR direction markers ('D'/'C', 'Dr'/'Cr', 'Debit'/'Credit').

    Used when the column map points both debit and credit at the SAME source
    column (a single-amount-plus-direction export from some internal banking
    systems). Generalised — no bank name is referenced.
    """
    for col in df.columns:
        vals = set(
            df[col].dropna().astype(str).str.strip().str.upper().unique()
        )
        if vals and vals.issubset(_DRCR_INDICATOR_VALS):
            return col
    return None


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


def _parse_single_transaction_line(line: str, require_balance: bool = True,
                                   leading_id_columns: List[str] = None,
                                   has_trans_lcy: bool = False) -> Optional[Dict[str, Any]]:
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
        leading_id_columns (list[str]): ordered id columns the HEADER declared
            BETWEEN the date and the narration (subset of "transaction_id",
            "reference_number"). When set, each is peeled off the front of the row
            ONLY if the next token is id-shaped; otherwise peeling stops and the
            token stays in the narration. Default None → classic behaviour.

    Returns:
        dict: Record with keys: "Date", "Narration", "amount", "Balance"
              (plus Transaction_ID / Reference_Number when present).
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

    # ── Step 1d: peel dedicated leading id columns (Tran Id / Ref No) ──────
    # Done AFTER the date(s) and time so it works whether the id column is printed
    # right after the transaction date (e.g. "Date|TranId|RefNum|Particulars") or
    # after a value date (e.g. "TransDt|ValueDt|TransnID|Particulars"). Driven by
    # the header (never a bank name); each id is consumed only when the next token
    # is id-shaped, so a row without these columns is never corrupted.
    txn_id = ref_no = ""
    if leading_id_columns:
        toks = rest.split()
        k = 0
        for col in leading_id_columns:
            if k < len(toks) and _ID_TOKEN_RE.match(toks[k]):
                if col == "transaction_id":
                    txn_id = toks[k]
                elif col == "reference_number":
                    ref_no = toks[k]
                k += 1
            else:
                break
        if k:
            rest = " ".join(toks[k:])

    # ── Step 1d: explicit Dr./Cr. marker ──────────────────────────────────
    # The most reliable direction signal — essential for statements listed
    # newest-first (e.g. IDBI), where the running-balance trick would be inverted.
    direction = ""
    md = re.search(r"\b(Dr|Cr)\.?\s+(?:INR|RS\.?|₹|\d)", rest, re.IGNORECASE)
    if md:
        direction = "D" if md.group(1).lower() == "dr" else "C"

    # Some banks print the amount/balance's Cr/Dr marker as a SEPARATE token
    # ("500.00 Cr" instead of "500.00Cr"). Re-attach a standalone trailing-style
    # Dr/Cr to the number before it so the amount/balance peeler sees a single money
    # token. \b after the marker means "Crore"/"Drone" inside a narration is safe.
    rest = re.sub(r"(\d)\s+(Dr|Cr)\b\.?", r"\1\2", rest, flags=re.IGNORECASE)

    # ── Step 2: pull the trailing amounts off the end ──────────────────────
    # The last money token is the running balance; the one before it is the
    # transaction amount. Reference/cheque numbers (no decimals) stay in the
    # narration. Layout-independent across bank column orders.
    tokens = rest.split()
    money = _peel_trailing_amounts(tokens)

    # NARRATION-LAST fallback: some layouts print the amounts right AFTER the date and
    # the narration LAST (e.g. "31-01-2025 1000.00 1000.00 Cr. Transfer From …"). The
    # from-the-end peel above finds no amounts there, so when it comes up short, peel the
    # amount run from the FRONT instead; the remaining tokens become the narration. This
    # only runs when the normal trailing peel failed, so a balance-last statement — whose
    # first post-date token is narration text, not money — is never affected.
    if require_balance and len(money) < 2:
        lead = _peel_leading_amounts(tokens)
        if len(lead) >= 2:
            money = lead

    if require_balance:
        if len(money) < 2:
            if len(money) == 0:
                # No money tokens on the date line at all. This happens when the PDF
                # layout puts the amounts column on a continuation line (Bug A). Return
                # a PARTIAL record so the continuation handler can fill the amounts in.
                narration = " ".join(tokens).strip()
                narration = re.sub(
                    r"\s+(?:Dr|Cr)\.?(?:\s+(?:INR|RS|Rs|₹))?\s*$", "", narration,
                    flags=re.IGNORECASE).strip()
                narration = re.sub(r"\s+(?:INR|RS|Rs|₹)\s*$", "", narration,
                                   flags=re.IGNORECASE).strip()
                return {
                    "Date": date_str,
                    "Time": time_str,
                    "Narration": narration,
                    "Transaction_ID": txn_id,
                    "Reference_Number": ref_no,
                    "amount": None,
                    "dr_cr": direction,
                    "Balance": "",
                    "_amounts_pending": True,
                }
            # Only one money token — not enough to tell amount from balance; discard.
            return None
        balance_str = money[-1]
        if len(money) == 3:
            if has_trans_lcy:
                # FLEXCUBE portal format: money = [Dr/Cr_Amount, Trans.Rate=1.00, Trans.LCY].
                # Trans.Rate is always 1.00 for INR and is NOT a transaction amount.
                # Trans.LCY == Dr/Cr_Amount (same value). Running Balance arrives on the
                # very next line (handled in the continuation loop above), so balance_str
                # is reset to "" here — we must not use the Trans.LCY value as a balance.
                amount_str = money[0]
                balance_str = ""
            else:
                # Three money tokens: Debit | Credit | Balance (statements with separate
                # debit and credit columns). The parser normally takes money[-2] as the
                # amount, which is the credit column. For debit transactions, credit = 0
                # and the real amount is in money[-3] (the debit column). Use whichever
                # of the two is non-zero; fall back to money[-2] if ambiguous.
                amt_candidate = money[-2]
                alt_candidate = money[-3]
                amt_val = _clean_amount(amt_candidate) or 0.0
                alt_val = _clean_amount(alt_candidate) or 0.0
                if amt_val == 0.0 and alt_val != 0.0:
                    amount_str = alt_candidate
                else:
                    amount_str = amt_candidate
        else:
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
        "Transaction_ID": txn_id,
        "Reference_Number": ref_no,
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


def _clean_balance_to_float(value: Any) -> float:
    """
    Like _clean_amount_to_float but SIGN-AWARE for a running-balance value.

    A balance often carries a CR/DR suffix that denotes its sign rather than a
    transaction direction:
        "3400.05CR" / "3400.05 Cr" / "3400.05(Cr)"  →  +3400.05  (account in credit)
        "1099.95DR" / "1099.95 Dr" / "1099.95(Dr)"  →  -1099.95  (account overdrawn)
    A bare leading minus ("-32.00") is also honoured. Statements that print no
    CR/DR suffix and no minus are returned exactly as before (positive). This keeps
    the balance chain correctly signed across an overdraft, which the downstream
    balance-reconciliation uses as a secondary check on debit/credit direction.
    """
    if pd.isna(value) or value is None:
        # A missing balance is "unknown", not zero. Returning NaN lets the
        # validator skip the reconciliation check for this row instead of
        # comparing against a spurious 0.0, which would flag every row in a
        # file that has no balance column (e.g. internal DR/CR indicator exports).
        return np.nan
    if isinstance(value, (int, float)):
        if np.isnan(value):
            return np.nan
        return float(value)

    raw = str(value).strip()
    if raw in ("", "nan", "None", "NaT", "-", "nil", "NIL", "N/A", "n/a"):
        return np.nan

    # Detect the direction marker before stripping it.
    m = re.search(r"\(?(Dr|Cr)\)?\s*$", raw, flags=re.IGNORECASE)
    sign = 1.0
    if m and m.group(1).lower() == "dr":
        sign = -1.0

    cleaned = raw.replace("₹", "").replace(",", "").replace(" ", "")
    cleaned = re.sub(r"\(?(?:Dr|Cr)\)?$", "", cleaned, flags=re.IGNORECASE)
    if cleaned in ("-", "nil", "NIL", "N/A", "n/a", ""):
        return np.nan

    try:
        val = float(cleaned)
    except ValueError:
        return np.nan
    # If the number already carried its own minus sign, don't double-negate.
    if val < 0:
        return val
    return sign * val


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

    # ── Numeric columns (Debit, Credit) ─────────────────────────────────────
    # Debit/Credit carry the transaction amount magnitude; a (Dr)/(Cr) marker on
    # them is only a direction hint, never a sign — keep the existing cleaner.
    for col in ["Debit", "Credit"]:
        if col in df.columns:
            df[col] = df[col].apply(_clean_amount_to_float)

    # ── Balance column (sign-aware) ──────────────────────────────────────────
    # A running balance may carry a CR/DR SUFFIX that denotes the sign of the
    # balance, not a transaction direction: "3400.05CR" = +3400.05 (in credit),
    # "1099.95DR" = -1099.95 (overdrawn). Interpreting this gives a correctly
    # signed balance chain, which makes the balance-reconciliation direction check
    # (_correct_direction_by_balance) accurate across an overdraft boundary. This
    # is purely additive — statements without a CR/DR suffix are unaffected.
    if "Balance" in df.columns:
        df["Balance"] = df["Balance"].apply(_clean_balance_to_float)

    # ── Narration column ────────────────────────────────────────────────────
    if "Narration" in df.columns:
        # Convert to string and strip extra whitespace
        df["Narration"] = df["Narration"].astype(str).str.strip()
        # Collapse embedded newlines/tabs/multiple spaces into a single space. Some
        # Excel/CSV exports store a multi-line narration inside ONE cell (e.g.
        # "ATM WDL\n\nATM CASH ..."), which would otherwise produce ragged multi-line
        # cells in the output CSV. Idempotent for already-single-line narrations.
        df["Narration"] = df["Narration"].str.replace(r"\s+", " ", regex=True).str.strip()
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
