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
    "idfc first": "IDFC First Bank", "idfc": "IDFC First Bank",
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
    "IDFB": "IDFC First Bank", "BDBL": "Bandhan Bank",
    # Additional common Indian bank IFSC prefixes (reference data, not per-bank logic).
    "JAKA": "Jammu & Kashmir Bank", "SIBL": "South Indian Bank",
    "TMBL": "Tamilnad Mercantile Bank", "CIUB": "City Union Bank",
    "DLXB": "Dhanlaxmi Bank", "KVBL": "Karur Vysya Bank", "DCBL": "DCB Bank",
    "AUBL": "AU Small Finance Bank", "ESFB": "Equitas Small Finance Bank",
    "UJVN": "Ujjivan Small Finance Bank", "PYTM": "Paytm Payments Bank",
    "FINO": "Fino Payments Bank", "AIRP": "Airtel Payments Bank",
    "UTBI": "United Bank of India", "ANDB": "Andhra Bank", "SYNB": "Syndicate Bank",
    "ORBC": "Oriental Bank of Commerce", "VIJB": "Vijaya Bank", "DENA": "Dena Bank",
    "ALLA": "Allahabad Bank", "BKDN": "Dena Bank", "TJSB": "TJSB Sahakari Bank",
    "SVCB": "SVC Co-operative Bank", "ABHY": "Abhyudaya Co-operative Bank",
    "NKGS": "NKGSB Co-operative Bank", "SURY": "Suryoday Small Finance Bank",
}

# The standard set of identity fields every statement produces.
# Extended with customer_id, branch_address, currency, and customer_address to
# handle the broader field set present in modern Indian bank statements.
ACCOUNT_FIELDS = [
    "account_holder", "account_number", "ifsc_code", "bank_name",
    "branch", "branch_address", "account_type", "statement_period",
    "opening_balance", "closing_balance",
    "customer_id", "currency", "customer_address",
    # Extended identity fields present in modern Indian bank statements. All are
    # OPTIONAL — extracted when clearly labelled, left blank otherwise (never guessed).
    "joint_holder", "nominee_name", "micr_code", "branch_code",
    "branch_phone", "branch_email", "branch_gstin", "product_type",
    "ckyc_number",
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

    # IFSC → bank fallback: every Indian IFSC encodes its bank in the first 4 letters.
    # If the bank name is still blank but we have a well-formed IFSC, derive it. This
    # works for EVERY source (Excel, CSV, PDF, image) and never overwrites a bank name
    # that was actually read from the document.
    if _is_blank(final.get("bank_name")) and not _is_blank(final.get("ifsc_code")):
        derived = IFSC_PREFIX_TO_BANK.get(str(final["ifsc_code"])[:4].upper(), "")
        if derived:
            final["bank_name"] = derived

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
    r"Status|Scheme|Address|Limit|Date|GSTIN|Nominee|Joint|Product|CKYC|Time|"
    r"Nomination|MAB|QAB|Cleared|Uncleared|Code|Open"
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


def _labelled_multiline(text: str, label_alts: str, max_lines: int = 4) -> str:
    """
    Like _labelled() but captures multi-line values (addresses, long branch names).

    After matching the label line, collects continuation lines until a blank
    line, another known field label (followed by a separator), or max_lines is
    reached.  All parts are joined with a single space.
    """
    pattern = rf"(?:{label_alts})\s*[:\-=]\s*([^\n]*)"
    m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
    if not m:
        return ""

    first_line = m.group(1).strip()
    parts = [first_line] if first_line else []

    _next_label_re = re.compile(
        rf"^\s*(?:{_NEXT_LABEL})\s*[:\-=]", re.IGNORECASE
    )
    # m.end() points to the character just before the \n that terminates the matched
    # line.  text[m.end():].splitlines() would therefore produce an empty first
    # element (the content between m.end() and that \n, which is nothing), causing
    # the loop to break immediately on "if not s".  Skip to the next \n first.
    remainder_start = text.find("\n", m.end())
    if remainder_start == -1:
        return " ".join(parts).strip()

    for raw_line in text[remainder_start + 1:].splitlines():
        if len(parts) >= max_lines:
            break
        s = raw_line.strip()
        if not s:
            break
        if _next_label_re.match(s):
            break
        parts.append(s)

    return " ".join(parts).strip()


# Holder is often a bare "Mr / Mrs / Ms / Shri / Smt ..." line with no label. We
# capture the title plus the following name words. Two generalisations over the
# original pattern:
#   1. The TITLE is matched case-insensitively (inline (?i:...)) so "Mr. Nidhi
#      Verma" (mixed-case, as SBI prints it) is recognised, not just "MR NIDHI".
#   2. Name words may be Title-Case OR ALL-CAPS — "[A-Z][A-Za-z'.]+" — so both
#      "Nidhi Verma" and "RITU SARITA" match. The first letter must be uppercase,
#      which still excludes lowercase header noise.
# Inter-word gaps use [ \t] (NOT \s) so the match never crosses a line boundary
# and swallows the next header field (e.g. "...Verma\nBranch Code").
# NOTE: deliberately NO "KUM" here — it was matching inside names like "KUMAR".
#
# The title must be followed by a real separator — a dot ("MR.KASULABADA") or at
# least one space ("Mr. Nidhi") — via (?:\.[ \t]*|[ \t]+). This is what stops the
# title "DR" from matching the first two letters of an unrelated all-caps word like
# "DRUK"/"DRDO" (which has no separator after DR). The leading \b prevents matching
# a title inside another word.
_HOLDER_TITLE_RE = re.compile(
    r"\b((?i:MR|MRS|MS|MISS|SHRI|SMT|M/S|DR)(?:\.[ \t]*|[ \t]+)"
    r"[A-Z][A-Za-z'.]+(?:[ \t]+[A-Z][A-Za-z'.&]+){0,5})",
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

# Corporate account holders (e.g. "STAR RETAIL LLP", "ABC PVT LTD") carry no
# personal title.  We recognise them by a trailing entity-type suffix.
# IMPORTANT: uses [ \t]+ (space/tab only, NOT \n) so the pattern never matches
# across line boundaries and accidentally captures a statement-period line
# ("STATEMENT FOR 16-Apr-2025\nSTAR RETAIL LLP" → matched the suffix "LLP").
_COMPANY_ENTITY_RE = re.compile(
    r"\b([A-Z][A-Z0-9&\./\-]*(?:[ \t]+[A-Z0-9&\./\-]+)*[ \t]+"
    r"(?:LLP|LLC|PLC|LTD\.?|PVT\.?[ \t]*LTD\.?|PRIVATE[ \t]+LIMITED|"
    r"PUBLIC[ \t]+LIMITED|LIMITED|INC\.?|CORP(?:ORATION)?|"
    r"INDUSTRIES|ENTERPRISES|SOLUTIONS|SERVICES|SYSTEMS))\b",
    re.IGNORECASE,
)

# Some banks (e.g. Bank of Baroda) print the holder as the very FIRST line with no
# label at all. As a last resort we take the first header line that looks like a
# name: 2–4 words, letters only, and not a known header/keyword line.
# Allows single-letter INITIALS as name tokens ("ASHOK T S", "T S RAVI") — common in
# Indian names — by letting each token after the first be a lone capital. Still anchored
# and word-shaped, so address/branch lines (which carry punctuation/digits) don't match.
_NAME_LINE_RE = re.compile(r"^[A-Z][A-Za-z.]*(?:\s+[A-Z][A-Za-z.&]*){1,3}$")
_NOT_NAME_WORDS = {
    "statement", "account", "bank", "branch", "address", "customer", "ifsc", "ifs",
    "micr", "current", "savings", "saving", "date", "page", "summary", "pin",
    "phone", "email", "detailed", "relationship", "registered", "profile",
    "opening", "closing", "balance", "transaction", "deposit", "withdrawal",
}
# Words that signal a line is a BRANCH or ADDRESS, not a person/company name. The
# first-line-name fallback must reject these so a branch line ("ASHOKA ROAD MYSURU")
# is never mistaken for the account holder. Generic geographic/postal vocabulary —
# not tied to any bank or city.
_ADDRESS_HINT_WORDS = {
    "road", "street", "nagar", "building", "marg", "floor", "block", "sector",
    "plot", "lane", "complex", "tower", "phase", "cross", "main", "colony",
    "layout", "extension", "society", "premise", "premises", "chowk", "ward",
    "district", "dist", "taluk", "tehsil", "village", "town", "city", "po",
    "p.o", "near", "opp", "behind", "no.", "bldg",
}


# A captured "name" value that is actually a stray LABEL (not a real name). In a
# two-column header, a regex anchored to one label can capture the NEIGHBOURING
# label's text — e.g. joint_holder="Opening" (start of "Opening Balance"),
# nominee_name="JOINT HOLDER :". These must be rejected as not-extracted rather than
# stored as if they were a person's name. Generic banking-label vocabulary, never a
# bank name or a person — so it cannot reject a real holder.
_LABEL_FRAGMENT_WORDS = {
    "opening", "closing", "period", "joint holder", "joint", "holder", "nominee",
    "nominee name", "nominee registered", "branch", "branch name", "branch address",
    "scheme", "currency", "account", "account no", "account number", "balance",
    "statement", "customer", "customer no", "ifsc", "ifsc code", "micr", "micr code",
    "b/f", "c/f", "name", "address", "registered", "not registered", "cust", "cif",
}


def _is_label_fragment(value: str) -> bool:
    """True if a captured value is really a label fragment, not a name/identity value."""
    if not value:
        return False
    v = re.sub(r"\s+", " ", str(value).strip())
    if v.endswith(":"):
        return True
    vn = v.lower().rstrip(":").strip()
    return vn in _LABEL_FRAGMENT_WORDS or all(ch in "-:. " for ch in vn)


def _first_line_name(header: str) -> str:
    """Last-resort holder: the first header line that looks like an unlabelled name.

    Scans the whole metadata region (not just the first few lines): many templates,
    notably SBI, print the holder name AFTER the bank/branch/address block — e.g. the
    name sits on line ~7, right before a 'Branch Code'/'CIF' label. The strong guards
    below (no digits, 2-4 name-shaped words, not an address/bank/known-keyword line)
    keep this from picking up a branch or city line."""
    for line in header.splitlines()[:14]:
        s = line.strip()
        if not s or any(ch.isdigit() for ch in s):
            continue
        words = s.split()
        if not (2 <= len(words) <= 4):
            continue
        low_words = set(w.strip(".,").lower() for w in words)
        if any(w in s.lower() for w in _NOT_NAME_WORDS):
            continue
        # Reject branch/address lines (e.g. "ASHOKA ROAD MYSURU").
        if low_words & _ADDRESS_HINT_WORDS:
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

# A money token with a 2-decimal part — used to tell a metadata line from a
# transaction row (so identity detection never reads the transaction table).
_MONEY_IN_LINE_RE = re.compile(r"\b\d{1,3}(?:,\d{2,3})*\.\d{2}\b|\b\d+\.\d{2}\b")
# A date anywhere in a line.
_DATE_IN_LINE_RE = re.compile(
    r"\d{1,2}[/\-.][0-9A-Za-z]{2,9}[/\-.]\d{2,4}|\d{4}[/\-.]\d{1,2}[/\-.]\d{1,2}")
# Column-header vocabulary; a line with >=3 of these is the transaction table header.
_TABLE_HEADER_WORDS = ("date", "narration", "description", "particular", "remarks",
                       "debit", "credit", "withdrawal", "deposit", "balance",
                       "cheque", "instrument")


def _looks_like_transaction(line: str) -> bool:
    """True if a line looks like a transaction row (a date AND a 2-decimal amount)."""
    s = line.strip()
    return bool(_DATE_IN_LINE_RE.search(s)) and bool(_MONEY_IN_LINE_RE.search(s))


def _metadata_region(text: str, max_lines: int = 25) -> str:
    """
    Returns the account-identity region: the lines BEFORE the transaction table
    begins. The table begins at the first column-header row (>=3 header words) or
    the first transaction-like row (date + amount). Identity detectors that scan
    unlabelled text (IFSC by shape, bank keyword, the unlabelled-holder fallbacks)
    must use THIS region, never the transaction rows — otherwise, on a statement
    with no metadata header (just a table), they would read a counterparty's IFSC
    or a transaction line as the account holder. Statements with a normal header are
    unaffected (their metadata sits above the table, inside this region).
    """
    out = []
    for ln in text.splitlines()[:max_lines + 15]:
        s = ln.strip()
        low = s.lower()
        if sum(1 for w in _TABLE_HEADER_WORDS if w in low) >= 3:
            break
        if _looks_like_transaction(s):
            break
        out.append(ln)
        if len(out) >= max_lines:
            break
    return "\n".join(out)


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

    # The account's own identity lives ABOVE the transaction table. We scope IFSC,
    # bank and the unlabelled-holder fallbacks to this metadata region so we never
    # pick up a counterparty's IFSC or a transaction row as identity — critical for
    # statements that have NO metadata header (just a table), where we prefer to
    # leave a field blank rather than read it wrong.
    header = _metadata_region(text)

    # ── Account holder ────────────────────────────────────────────────────────
    # IMPORTANT: holder detection is scoped to the metadata region (`header`), never
    # the full transaction body. The account holder is always printed in the header;
    # scanning the body lets a counterparty narration such as
    # "UPI/CUSTOMERNAME/5405655919/…" be mistaken for the holder (the "customer name"
    # label matching the spaceless "CUSTOMERNAME"). Restricting to the header removes
    # that whole class of false matches without losing any real holder.
    # Attempt 1: labels that END in "name" — the value follows, with or without a
    # colon ("Account Holders Name TOLLWAYS INFRA PROJECTS PRIVATE LIMITED").
    holder = _labelled(
        header,
        r"account\s*holders?\s*name|name\s*of\s*(?:the\s*)?account\s*holders?|"
        r"customer\s*name|cust(?:omer)?\s*name|holders?\s*name",
        sep_optional=True,
    )
    # Attempt 2: shorter labels that DO require a colon (avoids false matches).
    # "account title" is Bandhan Bank's label for the account holder.
    if not holder:
        holder = _labelled(
            header,
            r"account\s*holders?|account\s*name|account\s*title|a/?c\s*holders?",
        )
    # Attempt 2b: a bare "Name :" label, but only at the START of a header line so it
    # can never match the "Name" inside "Branch Name :" / "Scheme Name :" / "Nominee
    # Name :" (which start with another word). Some banks label the holder simply
    # "Name : SUMIT PATEL" on a line that also carries the next field. The value runs
    # to 2+ spaces or the next known label, exactly like _labelled.
    if not holder:
        mname = re.search(
            rf"^(?:a/?c\s*|account\s*)?name\s*[:=]\s*([^\n]+?)"
            rf"(?:\s{{2,}}|\s+(?:{_NEXT_LABEL})\b|$)",
            header, re.IGNORECASE | re.MULTILINE)
        if mname:
            cand = re.sub(r"\s{2,}", " ", mname.group(1)).strip()
            cand_low = cand.lower()
            if (cand and not cand[0].isdigit()
                    and not any(w in cand_low.split() for w in _NOT_NAME_WORDS)
                    and "bank" not in cand_low):
                holder = cand
    # Attempt 4: text that appears BEFORE "ACCOUNT :" on the same header line.
    # Some bank layouts (e.g. IDFC First Bank) print:
    #   "PIONEER HOLDINGS ACCOUNT :PRABHADEVI BRANCH"
    # The company name precedes the "ACCOUNT :" label on one combined line.
    if not holder:
        m = re.search(r"^(.+?)\s+ACCOUNT\s*:", header, re.IGNORECASE | re.MULTILINE)
        if m:
            candidate = m.group(1).strip()
            cand_low = candidate.lower()
            # Accept only if it looks like a name (not too long, no digits at start,
            # not itself a metadata phrase like "statement for ...").
            if (3 <= len(candidate) <= 60
                    and not candidate[0].isdigit()
                    and not any(w in cand_low for w in (
                        "statement", "customer", "acct", "account", "period",
                    ))):
                holder = candidate
    # Attempt 4a (BEFORE the label-anchored 4b): a CLEAN, UNLABELLED multi-word name on
    # the first header line. In a two-column header the holder's full name often sits
    # alone on the first line ("KAVYA BOSE") while a CITY prints before a right-column
    # label further down ("LUCKNOW  Customer No :971228349"). Without this, 4b grabs the
    # city. A first-line clean name (no digits, no label, 2–4 name words, not an address
    # / known-keyword line — all enforced by _first_line_name) is the stronger holder
    # signal, so it runs first. It is gated to ≥2 words so it never fires on a file
    # whose holder is a single token sitting before a label (handled by 4b below), and
    # _first_line_name skips any first line carrying digits/labels (e.g. "DEEPAK Period :
    # 01-03-2025"), so those files still resolve correctly through 4b.
    if not holder:
        fln = _first_line_name(header)
        if fln and len(fln.split()) >= 2:
            holder = fln
    # Attempt 4b: a name printed at the START of a header line, immediately FOLLOWED
    # by a strong identity label on the same line — e.g.
    #   "RAVI KUMAR IFSC: SBIN0965900 MICR: 887165858"
    # Here the holder carries no title and no "Account Holder" label, and the line
    # also contains digits (the IFSC/MICR), so the digit-free first-line fallback
    # below rejects it. This generalises Attempt 4 ("text before ACCOUNT :") to the
    # other strong identity labels banks pack onto the holder's line. Driven purely by
    # layout (a name token run that ends at a known label), never by a bank name.
    if not holder:
        # The name run allows a parenthesised token ("(OPC)") and digits so a company
        # name like "VELORA TEXTILES (OPC) PRIVATE LIMITED" is captured WHOLE (before
        # this, the parens broke the run and only the "PRIVATE LIMITED" suffix matched).
        m = re.search(
            r"^([A-Z][A-Za-z.&]+(?:[ \t]+[A-Za-z0-9.&()/-]+){0,6})[ \t]+"
            r"(?:IFSC|IFS|MICR|CIF|CKYC|Customer\s+(?:No|ID|Number)|Cust\.?\s*Reln|"
            r"A/?c\s*(?:No|Number)|Account\s+(?:No|Number)|Scheme|"
            r"Period|Statement|Currency)\b",
            header, re.IGNORECASE | re.MULTILINE)
        if m:
            cand = re.sub(r"\s{2,}", " ", m.group(1)).strip()
            cand_low = cand.lower()
            words = set(w.strip(".,").lower() for w in cand.split())
            # Accept only if it really looks like a personal/company name: not a known
            # header keyword, not a branch/address line, and not a bank name.
            if (not (words & _NOT_NAME_WORDS)
                    and not (words & _ADDRESS_HINT_WORDS)
                    and "bank" not in cand_low):
                holder = cand
    # Attempt 4c (lower priority than the label-anchored attempts above): an
    # unlabelled "MR/MRS/… NAME" line in the header. This is deliberately AFTER the
    # label-anchored attempts because a bare title match is lower-precision — a
    # statement's NOMINEE or JOINT holder also carries a title (e.g. "Nominee Name :
    # MRS REKHA"), and the real first holder ("DEEPAK") may carry none. We therefore
    # reject a title match that directly follows a nominee/joint/guardian label.
    if not holder:
        for m in _HOLDER_TITLE_RE.finditer(header):
            preceding = header[max(0, m.start() - 40):m.start()].lower()
            if re.search(r"nominee|joint\s*holder|second\s*holder|guardian|"
                         r"next\s*of\s*kin", preceding):
                continue  # this titled name is a nominee/joint holder, not the holder
            raw = re.sub(r"\s{2,}", " ", m.group(1)).strip()
            # Strip any trailing token that is a known field label (e.g. "MICR" in
            # "M/S CRYSTAL MARKETING MICR:" — captured as an ALL-CAPS name word but
            # actually the next field's label).
            raw = re.sub(rf"\s+(?:{_NEXT_LABEL})\s*$", "", raw, flags=re.IGNORECASE).strip()
            if raw:
                holder = raw
                break
    # Attempt 5: an unlabelled name as the very first header line (Bank of Baroda).
    if not holder:
        holder = _first_line_name(header)
    # Attempt 6: unlabelled company entity in the header (LLP, Ltd, Pvt Ltd, etc.).
    # Covers corporate accounts that carry no MR/MRS title and no "Account Holder"
    # label.  We skip any match that itself looks like a bank name (contains "bank").
    if not holder:
        for cm in _COMPANY_ENTITY_RE.finditer(header):
            candidate = re.sub(r"\s{2,}", " ", cm.group(1)).strip()
            if "bank" in candidate.lower():
                continue
            holder = candidate
            break
    # Attempt 7: the holder printed immediately AFTER the account number on the same
    # line, optionally past a "/CCY" currency tag — e.g.
    # "Account Number :8642666611469255/INR ANJALI". Generalised by layout, not bank.
    if not holder:
        m = re.search(
            r"account\s*(?:number|no\.?)\s*[:\-=]?\s*\d{6,}(?:\s*/\s*[A-Z]{3})?[ \t]+"
            r"([A-Z][A-Za-z]+(?:[ \t]+[A-Z][A-Za-z&.]+){0,3})",
            header, re.IGNORECASE)
        if m:
            cand = m.group(1).strip()
            if cand.lower() not in _NOT_NAME_WORDS:
                holder = cand
    # Normalise: strip leading separator/punctuation a label like "Name :- ANJALI DAS"
    # (colon-dash separator) leaves on the value, and collapse inner whitespace. A real
    # holder always starts with a letter (or a company "M/S"), so this only removes junk.
    if holder:
        holder = re.sub(r"^[\s\-:=.,/]+", "", re.sub(r"\s{2,}", " ", holder)).strip()
    # Reject a captured value that is really a stray label (two-column header bleed).
    if _is_label_fragment(holder):
        holder = ""
    details["account_holder"] = holder

    # ── Account number ────────────────────────────────────────────────────────
    # Collect every account-labelled numeric candidate in the HEADER (never the
    # transaction body, which is full of counterparty/reference numbers), then prefer
    # the LONGEST plausible one. Indian account numbers run ~9–18 digits, so an
    # 8-digit value sitting near an account label is usually a customer ID / branch
    # code, not the account number. The label set includes the generic
    # "Statement of Account No" and the "CASA … Details" account header. Length
    # arithmetic + header scoping only — no bank/filename-specific rule.
    acct_candidates = re.findall(
        r"(?:a/?c|account|acct|casa[\w \t/.-]*?details|statement\s+of\s+account)\s*"
        r"(?:number|no\.?|num)?\s*(?:[:\-=#]|\s)*?(\d{8,20})",
        header, re.IGNORECASE)
    if not acct_candidates:
        m = _ACCNO_LINE_RE.search(text)  # legacy full-text fallback
        if m:
            acct_candidates = [m.group(1)]
    if acct_candidates:
        plausible = [c for c in acct_candidates if 9 <= len(c) <= 20]
        details["account_number"] = max(plausible or acct_candidates, key=len)

    # ── Bare "ACCOUNT_NO  HOLDER NAME" metadata line (no labels) ──────────────
    # Some core-banking CSV/XLS exports print the identity UNLABELLED as the first
    # metadata line: "25078124219247  YASH DUBEY  for the period 11-09-2024 - to- …".
    # This is in-DOCUMENT data (never the filename), so extracting it is legitimate.
    # Scoped to the header's first lines and requires a long account-shaped number
    # immediately followed by a name, so a transaction row can't be misread as this.
    if not details["account_holder"] or not details["account_number"]:
        for line in header.splitlines()[:3]:
            mbn = re.match(
                r"^\s*(\d{9,20})\s+([A-Z][A-Za-z]+(?:[ \t]+[A-Z][A-Za-z&.]+){0,3})\b",
                line.strip())
            if mbn:
                cand_name = re.sub(r"\s+", " ", mbn.group(2)).strip()
                if cand_name.lower() not in _NOT_NAME_WORDS and "bank" not in cand_name.lower():
                    if not details["account_number"]:
                        details["account_number"] = mbn.group(1)
                    if not details["account_holder"]:
                        details["account_holder"] = cand_name
                    break

    # ── IFSC code — multi-window, account-scoped ──────────────────────────────
    # Search a cascade of candidate windows so an IFSC printed just BELOW the metadata
    # region (some templates put it under the table header) is still found, while never
    # scanning the deep transaction body (which carries COUNTERPARTY IFSCs). Prefer an
    # IFSC that follows an explicit IFSC label (the account's own) over a bare shape
    # match. All windows are header-scoped, so this widens coverage without grabbing a
    # counterparty IFSC. Generic — no bank-name branching.
    top_lines = "\n".join((text or "").splitlines()[:40])
    _IFSC_LABELLED = re.compile(
        r"(?:IFSC|IFS\s*Code|RTGS\s*/?\s*NEFT\s*IFSC|IFSC\s*Code)\s*[:\-]?\s*"
        r"([A-Z]{4}0[A-Z0-9]{6})", re.IGNORECASE)
    ifsc = ""
    for window in (header, top_lines):
        lm = _IFSC_LABELLED.search(window.upper())
        if lm:
            ifsc = lm.group(1); break
    if not ifsc:
        for window in (header, top_lines):
            sm = _IFSC_RE_FIND.search(window.upper())
            if sm:
                ifsc = sm.group(1); break
    if ifsc:
        details["ifsc_code"] = ifsc

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
        # Match a bank in the METADATA REGION only (the identity block above the
        # transaction table). We never scan the footer / transaction body: those
        # carry COUNTERPARTY bank names inside NEFT/IMPS narrations. On a pure-table
        # statement with no header this finds nothing and bank_name stays blank — we
        # prefer empty over a wrong guess.
        header_low = header.lower()

        # Pass 1 — LONGEST full canonical bank name present wins. This is what stops
        # a shorter name shadowing a longer one: "Central Bank of India" must win over
        # the "Bank of India" substring inside it, and "State Bank of India" over
        # "Bank of India". Purely length-ordered over the canonical name list — no
        # bank is special-cased, so it generalises to every name in the table.
        canonical_names = set(BANK_KEYWORDS.values()) | set(IFSC_PREFIX_TO_BANK.values())
        for name in sorted(canonical_names, key=len, reverse=True):
            if re.search(rf"\b{re.escape(name.lower())}\b", header_low):
                bank = name
                break

        # Pass 2 — abbreviation / keyword fallback (SBI, PNB, HDFC, IDFC, …) when the
        # full name is not spelled out. Longest keyword first so a short code can't
        # win over a more specific phrase; bounded \b…\b so "sbi" never matches inside
        # an "SBIN…" IFSC.
        if not bank:
            for keyword, name in sorted(BANK_KEYWORDS.items(),
                                        key=lambda kv: len(kv[0]), reverse=True):
                if re.search(rf"\b{re.escape(keyword)}\b", header_low):
                    bank = name
                    break
    details["bank_name"] = bank

    # ── Customer ID / CIF number ──────────────────────────────────────────────
    # Many banks print this as "Customer ID", "CIF No", or "CIF Number".
    details["customer_id"] = _labelled(
        text,
        r"customer\s*(?:id|number|no\.?)|cif\s*(?:no\.?|number|id)?|"
        r"client\s*(?:id|number|no\.?)|cust(?:omer)?\s*(?:no\.?|id)",
    )

    # ── Branch address (multi-line) ───────────────────────────────────────────
    details["branch_address"] = _labelled_multiline(
        text, r"branch\s*address|address\s*of\s*(?:the\s*)?branch", max_lines=3,
    )

    # ── Extended identity fields (all optional, clearly-labelled only) ─────────
    # Joint holder / second account holder. The label-fragment guard rejects a
    # two-column header bleed (e.g. joint_holder="Opening", nominee="JOINT HOLDER :").
    jh = _labelled(text, r"joint\s*holders?|second\s*holders?|joint\s*account\s*holders?")
    details["joint_holder"] = "" if _is_label_fragment(jh) else jh
    # Nominee.
    nm = _labelled(text, r"nominee\s*name|name\s*of\s*nominee|nominee")
    details["nominee_name"] = "" if _is_label_fragment(nm) else nm
    # MICR code (9-digit code; capture digits only to avoid trailing label spill).
    mm = re.search(r"MICR(?:\s*(?:code|no\.?|number))?\s*[:\-=]?\s*(\d{6,9})",
                   text, re.IGNORECASE)
    if mm:
        details["micr_code"] = mm.group(1)
    # Branch code / branch id.
    mb = re.search(r"branch\s*(?:code|id)\s*[:\-=]?\s*([A-Za-z0-9]{2,12})",
                   text, re.IGNORECASE)
    if mb:
        details["branch_code"] = mb.group(1)
    # Branch phone (digits, possibly with separators).
    details["branch_phone"] = _labelled(
        text, r"branch\s*phone(?:\s*number)?|branch\s*(?:tel|telephone)")
    # Branch email.
    me = re.search(r"branch\s*email(?:\s*address)?\s*[:\-=]?\s*([\w.\-]+@[\w.\-]+)",
                   text, re.IGNORECASE)
    if me:
        details["branch_email"] = me.group(1)
    # Branch GSTIN (15-char GST identifier).
    mg = re.search(r"(?:branch\s*)?gstin\s*[:\-=]?\s*([0-9A-Z]{15})",
                   text, re.IGNORECASE)
    if mg:
        details["branch_gstin"] = mg.group(1).upper()
    # Product / scheme name. A product name legitimately CONTAINS the word
    # "Account" (e.g. "Current Account Biz-Premium"), so we cannot use the generic
    # _labelled() here (its _NEXT_LABEL list would truncate at "Account"). Stop only
    # at a clearly different field label or 2+ spaces.
    mp2 = re.search(
        r"product(?:\s*type)?\s*[:\-=]\s*([^\n]+?)"
        r"(?:\s{2,}|\s+(?:Email|Branch|MAB|QAB|Nominee|IFSC|MICR|Account\s+Type|"
        r"Account\s+Branch|Currency|Status)\b|$)",
        text, re.IGNORECASE | re.MULTILINE)
    if mp2:
        details["product_type"] = mp2.group(1).strip()
    # CKYC number — REQUIRE a masked/alphanumeric KYC token (X's + digits, 10–14
    # chars). Some layouts print the label and value on separate lines / out of
    # order; demanding the token shape means we return blank rather than grabbing
    # adjacent text like "STATEMENT OF" (prefer empty over wrong).
    mck = re.search(r"ckyc\s*(?:no\.?|number)?\s*[:\-=]?\s*([X0-9]{10,14})\b",
                    text, re.IGNORECASE)
    if not mck:
        mck = re.search(r"([X]{6,}\d{2,6})\b", text)  # bare masked KYC token nearby
    if mck:
        details["ckyc_number"] = mck.group(1)

    # ── Currency ─────────────────────────────────────────────────────────────
    details["currency"] = _labelled(text, r"currency")
    if not details["currency"]:
        mc = re.search(r"\b(INR|USD|GBP|EUR|AED|SGD|JPY|CNY)\b", header)
        if mc:
            details["currency"] = mc.group(1).upper()

    # ── Customer address (multi-line) ─────────────────────────────────────────
    # Use specific labels only — generic "address" risks picking up the bank's own
    # registered office address printed in the footer.
    details["customer_address"] = _labelled_multiline(
        text,
        r"customer\s*address|address\s*of\s*(?:the\s*)?(?:account\s*)?holder|"
        r"correspondence\s*address|registered\s*address|mailing\s*address",
        max_lines=4,
    )

    logger.info(
        "account_extractor.extract_account_details_from_text: "
        "holder=%r account_number=%r ifsc=%r bank=%r customer_id=%r",
        details["account_holder"], details["account_number"],
        details["ifsc_code"], details["bank_name"], details["customer_id"],
    )
    return details
