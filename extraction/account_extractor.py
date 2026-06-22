"""
account_extractor.py — Read the ACCOUNT IDENTITY from a statement's own text.

THE PROBLEM (Problem 2):
    The account number was showing up as "ACC001" — taken from the filename — and
    the holder/IFSC were never read at all. For a court-facing investigation tool
    that is unacceptable: the identity must come from the statement itself.

WHAT THIS DOES:
    For text-based statements (digital PDF, DOCX) the header prints the identity
    in plain text, e.g.:

        State Bank of India
        Account Holder : Ravi Kumar Sharma   IFSC Code: SBIN0393634
        Account Number : 00000051399615291   Period: 01/08/2022 to 30/06/2024
        Branch : Pune Camp

    This module pulls those fields out deterministically with regex — no LLM, fully
    local — and returns them in the same shape the vision reader uses, so the rest
    of the pipeline treats every source the same way.

    Fields not present in the text are returned as "" (empty), never guessed.

Team: Survey Corps | CIDECODE Hackathon 2026 | CID Karnataka
"""

import logging
import re
from typing import Dict

logger = logging.getLogger(__name__)

# Bank detection works two generalisable ways, so we are not tied to any one
# statement layout:
#   1. a keyword seen ANYWHERE in the text  → canonical bank name
#   2. the first 4 letters of the IFSC code → bank (every Indian IFSC encodes its
#      bank, so this works even when the bank name is not printed as plain text)
BANK_KEYWORDS = {
    "state bank of india": "State Bank of India", "sbi": "State Bank of India",
    "hdfc": "HDFC Bank", "icici": "ICICI Bank", "axis": "Axis Bank",
    "kotak": "Kotak Mahindra Bank", "canara": "Canara Bank",
    "punjab national": "Punjab National Bank", "pnb": "Punjab National Bank",
    "bank of baroda": "Bank of Baroda", "union bank": "Union Bank of India",
    "yes bank": "Yes Bank", "idbi": "IDBI Bank", "indian bank": "Indian Bank",
    "bank of india": "Bank of India", "indusind": "IndusInd Bank",
    "federal bank": "Federal Bank", "rbl": "RBL Bank", "bandhan": "Bandhan Bank",
    "central bank": "Central Bank of India", "uco bank": "UCO Bank",
    "karnataka bank": "Karnataka Bank", "bank of maharashtra": "Bank of Maharashtra",
    "indian overseas": "Indian Overseas Bank", "citi": "Citibank",
    "standard chartered": "Standard Chartered", "hsbc": "HSBC",
}

# IFSC prefix (bank code) → bank name. Covers the common Indian banks.
IFSC_PREFIX_TO_BANK = {
    "SBIN": "State Bank of India", "HDFC": "HDFC Bank", "ICIC": "ICICI Bank",
    "UTIB": "Axis Bank", "KKBK": "Kotak Mahindra Bank", "CNRB": "Canara Bank",
    "PUNB": "Punjab National Bank", "BARB": "Bank of Baroda", "UBIN": "Union Bank of India",
    "IDIB": "Indian Bank", "BKID": "Bank of India", "YESB": "Yes Bank", "IBKL": "IDBI Bank",
    "MAHB": "Bank of Maharashtra", "IOBA": "Indian Overseas Bank", "CBIN": "Central Bank of India",
    "UCBA": "UCO Bank", "FDRL": "Federal Bank", "INDB": "IndusInd Bank", "RATN": "RBL Bank",
    "KARB": "Karnataka Bank", "PSIB": "Punjab & Sind Bank", "CORP": "Corporation Bank",
    "CITI": "Citibank", "SCBL": "Standard Chartered", "HSBC": "HSBC",
}

# The standard set of identity fields every statement produces.
ACCOUNT_FIELDS = [
    "account_holder", "account_number", "ifsc_code", "bank_name",
    "branch", "account_type", "statement_period", "opening_balance", "closing_balance",
]


def _empty_account_details() -> Dict[str, str]:
    """Returns the identity shape with every field blank."""
    return {field: "" for field in ACCOUNT_FIELDS}


def _is_blank(value) -> bool:
    """True for missing / empty / UNREADABLE values."""
    if value is None:
        return True
    s = str(value).strip()
    return s == "" or s.upper() == "UNREADABLE"


# IFSC must be exactly 4 letters + '0' + 6 alphanumerics. Account numbers are
# 6–20 digits. We use these to spot a blurry misread (e.g. an IFSC that came back
# one character too long) and fall back to the authoritative reference instead.
_IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
_ACCNO_RE = re.compile(r"^[0-9Xx]{6,20}$")


def _content_value_is_trustworthy(field: str, value: str) -> bool:
    """
    Decides whether a value read from the document is well-formed enough to trust
    over the authoritative reference. Most fields are free text (always trusted);
    IFSC and account number have a fixed shape, so a malformed one is rejected.
    """
    if _is_blank(value):
        return False
    v = str(value).strip()
    if field == "ifsc_code":
        return bool(_IFSC_RE.match(v.upper()))
    if field == "account_number":
        return bool(_ACCNO_RE.match(v))
    return True


def reconcile_account_details(
    content_details: Dict[str, str],
    account_ref: str,
    bank_name_hint: str = "",
    master_row: Dict[str, str] = None,
) -> Dict[str, str]:
    """
    Produces the FINAL real account identity for a statement (Problems 2 & 4).

    Priority for every field:
        1. what we read from the document content (vision or text) — primary,
        2. the investigator's authoritative reference row (accounts_master.csv),
        3. otherwise blank.
    This is how a blurry IFSC misread on a photo gets corrected, and how an Excel
    file that prints no account number on its rows still ends up with the real one.

    `account_ref` is the internal investigation id (e.g. ACC002). We keep it as a
    separate field so the analysis phase can still link accounts, while the
    account_number field carries the REAL bank account number.

    Parameters:
        content_details (dict): identity read from the document (may be partial).
        account_ref (str): internal investigation id for this account.
        bank_name_hint (str): bank name the investigator supplied for the upload.
        master_row (dict): the matching row from accounts_master.csv, if available,
            using its native column names (account_number, account_holder_name,
            bank_name, branch_name, ifsc_code, account_type, opening_balance).

    Returns:
        dict: the ACCOUNT_FIELDS, all real, plus "account_ref".
    """
    content_details = content_details or {}
    master_row = master_row or {}

    # Map accounts_master.csv column names onto our field names.
    master = {
        "account_holder": master_row.get("account_holder_name", ""),
        "account_number": master_row.get("account_number", ""),
        "ifsc_code": master_row.get("ifsc_code", ""),
        "bank_name": master_row.get("bank_name", "") or bank_name_hint,
        "branch": master_row.get("branch_name", ""),
        "account_type": master_row.get("account_type", ""),
        "statement_period": "",
        "opening_balance": master_row.get("opening_balance", ""),
        "closing_balance": "",
    }

    final = _empty_account_details()
    for field in ACCOUNT_FIELDS:
        content_val = content_details.get(field, "")
        # Trust the document value only if it is present AND well-formed (this is
        # what rejects a blurry, malformed IFSC/account-number misread).
        if _content_value_is_trustworthy(field, content_val):
            final[field] = str(content_val).strip()
        elif not _is_blank(master.get(field, "")):
            final[field] = str(master[field]).strip()
        else:
            # Last resort: keep whatever the document had (even if malformed) so
            # the field is never blank when the document showed *something*.
            final[field] = "" if _is_blank(content_val) else str(content_val).strip()

    final["account_ref"] = account_ref  # internal id, kept for analysis linkage
    return final


# A label followed by its value. We accept many spellings of each label and any
# of : - = whitespace as the separator. The value is captured up to the next
# 2+ spaces (statements often pack two fields on one line) or end of line.
# Statements often pack two fields on one line ("Holder : X  IFSC : Y" or even
# single-spaced). So a value ends at 2+ spaces, end-of-line, OR the start of the
# next known field label — otherwise we'd swallow the neighbouring field.
_NEXT_LABEL = (
    r"IFSC|IFS|Account|A/?C|Period|Branch|Page|City|State|Phone|Email|MICR|CIF|"
    r"Cust|Customer|Balance|RTGS|NEFT|Drawing|Interest|MOD|Nomination|Currency|"
    r"Status|Scheme|Address|Limit|Date"
)


def _labelled(text: str, label_alts: str, sep_optional: bool = False) -> str:
    """
    Finds `label : value` for any of the given label spellings (regex alternation),
    tolerant of missing spaces ("AccountNo"). The value runs until 2+ spaces, the
    next known field label, or end of line. Returns the value or ''.

    sep_optional=True allows "Label Value" with NO colon (some banks print
    "Account Holders Name TOLLWAYS …"). Use it only for labels that are specific
    enough that the next word is unambiguously the value.
    """
    sep = r"[:\-=]?" if sep_optional else r"[:\-=]"
    pattern = (
        rf"(?:{label_alts})\s*{sep}\s*"
        rf"([^\n]+?)(?:\s{{2,}}|\s+(?:{_NEXT_LABEL})\b|$)"
    )
    m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
    return m.group(1).strip() if m else ""


# Holder is often a bare "MR / MRS / MS / SHRI / SMT ..." line with no label. We
# capture the title plus following ALL-CAPS words (names are printed in caps),
# which stops cleanly before Title-Case header text like "Your Base Branch".
# NOTE: deliberately NO "KUM" here — it was matching inside names like "KUMAR".
# \s* (not \s+) so "MR.KASULABADA" (no space after the dot) is still captured.
_HOLDER_TITLE_RE = re.compile(
    r"\b((?:MR|MRS|MS|SHRI|SMT|M/S|DR)\.?\s*[A-Z]{2,}(?:\s+[A-Z]{2,}){0,5})",
)

# Opening/closing balance: the value MUST be a number (optionally after Rs/INR/₹).
# Requiring a number stops a summary header like "… ClosingBal" from grabbing the
# next line of text as the balance.
def _balance_after(text: str, label_alts: str) -> str:
    """Reads the numeric balance printed after a label, comma/symbol-stripped."""
    m = re.search(
        rf"(?:{label_alts})\s*[:\-=]?\s*(?:Rs\.?|INR|₹)?\s*([\d,]+\.\d{{2}})",
        text, re.IGNORECASE | re.MULTILINE,
    )
    return m.group(1).replace(",", "") if m else ""

# Some banks (e.g. Bank of Baroda) print the holder as the very FIRST line with no
# label at all. As a last resort we take the first header line that looks like a
# name: 2–4 words, letters only, and not a known header/keyword line.
_NAME_LINE_RE = re.compile(r"^[A-Z][A-Za-z.]+(?:\s+[A-Z][A-Za-z.&]+){1,3}$")
_NOT_NAME_WORDS = {
    "statement", "account", "bank", "branch", "address", "customer", "ifsc", "ifs",
    "micr", "current", "savings", "saving", "date", "page", "summary", "pin",
    "phone", "email", "detailed", "relationship", "registered", "profile",
    "opening", "closing", "balance", "transaction", "deposit", "withdrawal",
}


def _first_line_name(header: str) -> str:
    """Last-resort holder: the first header line that looks like an unlabelled name."""
    for line in header.splitlines()[:6]:
        s = line.strip()
        if not s or any(ch.isdigit() for ch in s):
            continue
        words = s.split()
        if not (2 <= len(words) <= 4):
            continue
        if any(w in s.lower() for w in _NOT_NAME_WORDS):
            continue
        if _NAME_LINE_RE.match(s):
            return s
    return ""
# A bank account number: a contiguous run of 8–20 digits after an account label.
# (Contiguous avoids spilling into the next number on the same line.)
_ACCNO_LINE_RE = re.compile(
    r"(?:a/?c|account|acct)\s*(?:number|no\.?|num)?\s*[:\-=#]?\s*(\d{8,20})",
    re.IGNORECASE,
)
# IFSC has a globally unique shape. We only trust the one in the HEADER (the
# account's own); scanning the whole document would grab counterparties' IFSCs
# out of the transaction rows.
_IFSC_RE_FIND = re.compile(r"\b([A-Z]{4}0[A-Z0-9]{6})\b")


def extract_account_details_from_text(text: str) -> Dict[str, str]:
    """
    Extracts the account identity from ANY bank statement's text — not just the
    mentoring dataset's format.

    It is deliberately format-agnostic:
      • each field accepts many label spellings (Account Name / Account Holder /
        Customer Name; Account No / AccountNo / A/c No; IFSC / IFS Code / RTGS-NEFT
        IFSC; etc.),
      • the holder also falls back to a bare "MR/MRS/… NAME" line when unlabelled,
      • the IFSC is found by its globally-unique shape anywhere in the text,
      • the bank is inferred from a keyword OR from the IFSC's bank-code prefix.

    Everything is local regex — no LLM, no data leaves the machine. Fields not
    present are "" (empty), never guessed.

    Parameters:
        text (str): Full extracted text of a digital PDF / DOCX statement.

    Returns:
        dict: the ACCOUNT_FIELDS, read from the document content.
    """
    details = _empty_account_details()
    if not text or not text.strip():
        return details

    # The account's own identity lives in the HEADER. We scope IFSC, bank and the
    # unlabelled-holder fallback to the header so we never pick up a counterparty's
    # details out of the transaction rows further down.
    header = "\n".join(text.splitlines()[:25])

    # ── Account holder ────────────────────────────────────────────────────────
    # Attempt 1: labels that END in "name" — the value follows, with or without a
    # colon ("Account Holders Name TOLLWAYS INFRA PROJECTS PRIVATE LIMITED").
    holder = _labelled(
        text,
        r"account\s*holders?\s*name|name\s*of\s*(?:the\s*)?account\s*holders?|"
        r"customer\s*name|cust(?:omer)?\s*name|holders?\s*name",
        sep_optional=True,
    )
    # Attempt 2: shorter labels that DO require a colon (avoids false matches).
    if not holder:
        holder = _labelled(text, r"account\s*holders?|account\s*name|a/?c\s*holders?")
    # Attempt 3: an unlabelled "MR/MRS/… NAME" line in the header.
    if not holder:
        m = _HOLDER_TITLE_RE.search(header)
        if m:
            holder = re.sub(r"\s{2,}", " ", m.group(1)).strip()
    # Attempt 4: an unlabelled name as the very first header line (Bank of Baroda).
    if not holder:
        holder = _first_line_name(header)
    details["account_holder"] = holder

    # ── Account number ────────────────────────────────────────────────────────
    m = _ACCNO_LINE_RE.search(text)
    if m:
        details["account_number"] = m.group(1)

    # ── IFSC code (header only, by unique shape) ──────────────────────────────
    m = _IFSC_RE_FIND.search(header.upper())
    if m:
        details["ifsc_code"] = m.group(1)

    # ── Branch / account type / period ────────────────────────────────────────
    details["branch"] = _labelled(text, r"account\s*branch|home\s*branch|base\s*branch|branch")
    details["account_type"] = _labelled(
        text, r"account\s*type|a/?c\s*type|account\s*description|scheme")
    if not details["account_type"]:
        # Fall back to a standalone account-kind word seen in the header.
        mt = re.search(r"\b(Savings|Current|Salary|Overdraft|Recurring Deposit|Fixed Deposit)\b",
                       header, re.IGNORECASE)
        if mt:
            details["account_type"] = mt.group(1)

    details["statement_period"] = _labelled(
        text, r"statement\s*period|account\s*statement\s*from|period|for\s*the\s*period")
    if not details["statement_period"]:
        # Catch a "<date> to/- <date>" range introduced by from/between/period,
        # tolerating colons ("From : 01/06/2018 To : 10/10/2018").
        # Two date shapes: numeric/short-month ("01/06/2018", "16 Apr 2018") and
        # full "Month DD, YYYY" ("April 01, 2019").
        _d = r"(?:\d{1,2}[-/ ][\w]{2,9}[-/ ]\d{2,4}|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})"
        mp = re.search(
            rf"(?:from|between|period)\s*[:\-]?\s*({_d})\s*(?:to|-|–|—)\s*[:\-]?\s*({_d})",
            text, re.IGNORECASE)
        if mp:
            details["statement_period"] = f"{mp.group(1).strip()} to {mp.group(2).strip()}"

    # ── Opening / closing balance (value must be a number) ────────────────────
    # "balance as on <date> :" is how SBI prints the opening balance.
    details["opening_balance"] = _balance_after(
        text, r"opening\s*balance|balance\s*b/?f|b/?f|balance\s*as\s*on[^:\n]*")
    details["closing_balance"] = _balance_after(text, r"closing\s*balance|balance\s*c/?f|c/?f")

    # ── Bank name ─────────────────────────────────────────────────────────────
    # Prefer the account's own IFSC prefix (most reliable). Else match a bank
    # keyword in the HEADER only, with word boundaries so "sbi" does NOT match
    # inside an "SBIN..." IFSC code sitting in a transaction line.
    bank = ""
    if details["ifsc_code"]:
        bank = IFSC_PREFIX_TO_BANK.get(details["ifsc_code"][:4].upper(), "")
    if not bank:
        # Leading word-boundary only (so "icici" still matches "icicibank.com"),
        # which is safe here because the reliable IFSC-prefix path already ran.
        header_low = header.lower()
        for keyword, name in BANK_KEYWORDS.items():
            if re.search(rf"\b{re.escape(keyword)}", header_low):
                bank = name
                break
    details["bank_name"] = bank

    logger.info(
        "account_extractor.extract_account_details_from_text: "
        "holder=%r account_number=%r ifsc=%r bank=%r",
        details["account_holder"], details["account_number"],
        details["ifsc_code"], details["bank_name"],
    )
    return details
