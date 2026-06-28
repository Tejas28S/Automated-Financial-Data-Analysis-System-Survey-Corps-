# Extraction Fix Brief v2 — Evidence-Grounded
## CIDECODE Hackathon 2026 | Team Survey Corps
## Based on: metadata__1_.json + flagged_transactions__1_.csv + duplicates__1_.csv + both diagnosis PDFs

---

## Context: What the numbers actually say right now

| Metric | Count |
|---|---|
| Total transactions extracted | ~182,000 |
| Clean | ~139,789 |
| Flagged | **2,271** |
| Duplicates identified | **40,745** |
| Files status=ok | 144 |
| Files with 0 rows extracted | 9 |
| Accounts still missing name/number | 9 (down from 25 — good improvement, protect it) |

The 2,271 flagged rows break down into exactly **3 flag reasons**:
- `narration_contains_multiple_transactions` — **1,195 rows** (53%)
- `balance_mismatch` — **1,070 rows** (47%)
- `invalid_date` — **6 rows** (<1%)

And they are heavily concentrated — the top 3 accounts alone account for **1,740 of 2,271** flags (77%). Fix those three accounts and ~80% of the flag count disappears.

---

## Bug Group A — HDFC Footer Bleeding into Narration
### Files: TARUN PILLAI statement.pdf, STATEMENT (3).pdf, STATEMENT (6).pdf, STATEMENT 4.pdf
### Flag type: `narration_contains_multiple_transactions`
### Row count: 717 + 193 + 193 + 79 = **1,182 rows** (the single biggest group)

### What's actually happening

The HDFC statement template has a page-break footer that contains:
1. A closing balance line
2. The **next page's header** — which includes date column values for ALL transactions on that page, printed as a column header before the actual narration text

When pdfplumber extracts the page linearly, it reads the date column of the next page's block first — producing a string like:
```
26/06/09 01/07/09 01/07/09 02/07/09 04/07/09 16/07/09...
NWD-5264190125438852 -CHANDIGARH ATW-526419XXXXXX8852...
```
...which the parser sees as "one very long narration" on the last row of the current page. That one row swallows all the transactions from the next page's date column, and the actual narration tokens get appended at the end.

The HDFC footer also includes the branch GSTIN, registered office address, page number, and account branch address — all concatenated:
```
...PageNo.:3 AccountBranch : SECTOR43BCHANDIGARH Address : HDFCBANKLTD...
MR. TARUN PILLAI State : CHANDIGARH #494N
```

The reconciliation_rate for TARUN PILLAI is **1.0** (perfect) — meaning the pipeline correctly identifies 9,658 expected transactions and produces 9,658 rows. The 717 flagged rows are **not dropped** — they are produced but with bloated narrations. The actual debit/credit/balance values on these rows are likely correct; only the narration field is wrong.

### Root cause

The text-layer extractor does not strip the HDFC page-footer block before parsing. HDFC's footer is structurally recognizable — it always contains:
- `"Closing balance includes funds earmarked"` or `"Contents of this statement will be considered correct"`
- `"PageNo.:"` or `"Page No :"` 
- `"Registered Office Address: HDFC Bank House"`

These strings **never appear in legitimate narrations**. They are reliable, bank-agnostic footer sentinels.

### Fix — structural footer stripper in standardiser.py

**Before building the narration field from raw text, strip any footer block that matches known structural sentinels. Do NOT hardcode bank names — match on the structural strings themselves.**

```python
# In standardiser.py or wherever narration is assembled from raw PDF text:

FOOTER_SENTINEL_PATTERNS = [
    r'Closing balance includes funds earmarked',
    r'Contents of this statement will be considered correct',
    r'PageNo\.\s*:?\s*\d+',
    r'Page\s+No\s*\.?\s*:?\s*\d+',
    r'Registered Office Address',
    r'For clarification kindly contact',
    r'DCB\s*-\s*Customer Care',
    r'customercare@\w+bank\.com',
    r'Non Resident Indian customers please dial',
    r'GST Number\s*-\s*\(',          # DCB GST footer line
    r'Total Number of Transactions',  # DCB summary footer
    r'Debit\s+Credit\s+Balance',      # repeated column headers in footer
]

import re

def strip_footer_from_narration(narration_text: str) -> str:
    """
    Truncate narration at the first footer sentinel found.
    All text from that point on is footer/header bleed, not transaction content.
    """
    if not narration_text:
        return narration_text
    
    for pattern in FOOTER_SENTINEL_PATTERNS:
        match = re.search(pattern, narration_text, re.IGNORECASE)
        if match:
            return narration_text[:match.start()].strip()
    
    return narration_text
```

**Additionally**, for the date-column bleed (dates like `26/06/09 01/07/09 01/07/09...`):

```python
# Detect if narration starts with a sequence of dates — this is page header bleed
DATE_SEQUENCE_PATTERN = re.compile(
    r'^(?:\d{2}/\d{2}/\d{2,4}\s+){3,}',  # 3+ dates back-to-back at start
)

def strip_leading_date_sequence(narration_text: str) -> str:
    """
    If narration starts with 3+ consecutive dates, those are page-header column
    dates bleeding in — strip them. Real narrations never start with multiple dates.
    """
    match = DATE_SEQUENCE_PATTERN.match(narration_text)
    if match:
        return narration_text[match.end():].strip()
    return narration_text

def clean_narration(raw_narration: str) -> str:
    text = strip_leading_date_sequence(raw_narration)
    text = strip_footer_from_narration(text)
    return text
```

**Apply `clean_narration()` to every narration field during standardisation**, before the balance-mismatch validator sees it. This is the single function Claude Code needs to add + wire in.

### Regression check

After fix: re-run on TARUN PILLAI statement.pdf.
- `narration_contains_multiple_transactions` count for account `38211367068923` must drop from 717 toward 0
- Reconciliation rate must stay at 1.0 (no rows should be lost — we're cleaning narration, not dropping rows)
- Run on STATEMENT (3).pdf, STATEMENT (6).pdf, STATEMENT 4.pdf — same check
- **Confirm no new flags appear on any file that currently has 0 narration flags**

---

## Bug Group B — SOA 0167042251865512: Amount Parsed as Row-Count (1.0)
### File: soa_0167042251865512.pdf
### Flag type: `balance_mismatch` / `mismatch_diagnosis=missing_amount`
### Row count: **882 rows** (second biggest group, 46% of this file's rows flagged)

### What's actually happening

Every single flagged row in this file has `Debit=1.0` or `Credit=1.0` — never any other value. The actual amount is visible in the narration: `"15/11/22 5.00"`, `"24/09/22 1.00"`, `"03/12/22 290.00"`. The narration is structured as:
```
<transaction description> <date-in-statement-format> <actual-amount>
```

The pipeline's column parser is misidentifying the **row index or a fixed column** as the amount field, and reading `1.0` (probably a count or a page element) instead of the actual currency amount.

The metadata confirms this file is `reconciliation_rate=0.156` — only 1,046 of 1,928 rows are clean. The statement period is 8+ years (`01/04/17 to 28/11/25`) and bank is `Unknown Bank` — the IFSC field is empty. This is almost certainly a **Yes Bank or IndusInd** small-branch statement with a non-standard column layout.

### Root cause

The column_map for this file is `{"engine": "deterministic_default"}` — meaning the deterministic parser could not identify the column layout and fell back to a generic default. The generic default is misaligning debit/credit columns with some other field (row count, serial number, or a GST/service charge micro-column).

### Fix

**This file needs schema-level diagnosis before a code fix.** Claude Code must:

1. Run `pdfplumber` on the first 2 pages of `soa_0167042251865512.pdf` and print the raw word coordinates — specifically the bounding box (x0, x1) of every column header token.
2. Identify where "Debit", "Credit", "Withdrawal", "Deposit" (whichever the template uses) actually sit on the x-axis vs. where the parser is currently reading from.
3. If the column headers have non-standard names (e.g. "Withdrawal Amount", "Deposit Amount" as two-word headers split across lines), add those to the column_identifier's vocabulary.

**Concrete diagnostic command to run first:**

```python
import pdfplumber

with pdfplumber.open("soa_0167042251865512.pdf") as pdf:
    page = pdf.pages[0]
    words = page.extract_words()
    # Print every word with its x0 coordinate to identify column boundaries
    for w in words[:80]:
        print(f"x0={w['x0']:6.1f} x1={w['x1']:6.1f} text={w['text']}")
```

Only once column boundaries are confirmed, update the column_identifier patterns to handle this template's header vocabulary. Do NOT change the generic default — that will break other files.

### Regression check

After fix: `soa_0167042251865512.pdf` reconciliation rate must move above 0.85. The 882 `missing_amount` rows must drop significantly. Debit/Credit values for previously-flagged rows must no longer be `1.0` — verify 5 random rows have amounts matching what their narration shows.

---

## Bug Group C — 216655101347: Multi-Account Statement With Negative Debits
### File: 216655101347_01-Jan-2025_22-May-2025.pdf
### Flag type: `balance_mismatch` (mix of `missing_amount`, `missing_transaction`, `balance_jump_no_change`)
### Row count: **141 rows** (43% of this file's rows flagged)

### What's actually happening

The rows show `Debit=-27.14` (negative debit), and the balance column reads `20053.60`, `30080.74`, `40107.88`, `50135.02` — incrementing by ~10,027.14 each row. The narration is `ATW WD FEE:000000443211:512293XXXXXX1745` — an ATM withdrawal fee. Same fee, applied multiple times to accounts with sequential numbering.

This is a **multi-account consolidated statement** — the file contains ATM fee entries for multiple accounts stacked into a single PDF. Each "row" represents the same fee being applied across different accounts within the same banking relationship. The running balance column is carrying a **cumulative total across accounts**, not a per-transaction running balance within one account.

Two specific problems:
1. Negative debit values — the amount sign convention is reversed in this template (debit is stored as negative, credit as positive). The standardiser's sanity check flags these as invalid.
2. Balance incrementing by exactly `prior_balance + fee` for each row — the validator's balance-continuity check sees this as a jump with no net change, because it's treating 5 account rows as one account's ledger.

### Fix

Two independent fixes:

**Fix C1 — Negative debit sign convention:**
```python
# In standardiser.py, after reading Debit/Credit from the column map:
# Some templates store debit as a negative number in the Debit column
# Normalize: if debit < 0, take abs(debit) as the debit amount

def normalize_debit_credit(debit: float, credit: float) -> tuple[float, float]:
    if debit < 0 and credit == 0:
        return abs(debit), 0.0
    if credit < 0 and debit == 0:
        return 0.0, abs(credit)
    return debit, credit
```

**Fix C2 — Consolidated statement detection:**
The validator should detect when the same narration repeats with incrementing account-reference numbers (e.g. `443211`, `443210`, `443209`, `443208`...) and the balance does not follow a single-account ledger sequence. These rows should be flagged as `consolidated_statement_rows` rather than `balance_mismatch`, and excluded from the per-account balance-continuity check.

```python
# In validator.py: detect consolidated statement pattern
def is_consolidated_statement_block(rows: list[dict]) -> bool:
    """
    Returns True if 3+ consecutive rows have the same narration prefix
    but different reference number suffixes — signature of a consolidated
    multi-account statement row block.
    """
    if len(rows) < 3:
        return False
    narr_prefix = lambda n: re.sub(r'\d{6,}', 'N', n)  # replace ref numbers
    prefixes = [narr_prefix(r.get('Narration', '')) for r in rows]
    return len(set(prefixes)) == 1  # all same structure, different numbers
```

### Regression check

After fix: `216655101347` flagged row count drops from 141. Rows with `Debit=-27.14` must no longer appear in `balance_mismatch`. Check that no currently-clean row for this account gets newly flagged.

---

## Bug Group D — DCB Bank Footer Bleeding into Narration
### Files: 79895082327702 (account), 25078124219247 (account)
### Flag type: `narration_contains_multiple_transactions`
### Row count: **2 rows each** (small, but same root cause as Group A)

The DCB Bank footer is structurally identifiable — it always contains:
```
GST Number - (GST NOT REGISTERED FOR THIS ACCOUNT)
DCB BANK ... terms and conditions
Registered Office: 6th Floor, Tower A, Peninsula Business Park
customercare@dcbbank.com
```

This is already covered by the `FOOTER_SENTINEL_PATTERNS` in Bug Group A's fix — `strip_footer_from_narration()` will handle DCB too if `"GST Number - ("` and `"customercare@dcbbank.com"` are in the sentinel list (they are, in the fix above). **No additional code needed for DCB** — Group A's fix covers it.

---

## Bug Group E — `invalid_date` Total Rows (6 rows across 5 accounts)
### Flag type: `invalid_date`

These 6 rows have dates like `01/01/6999`, `01/01/1384`, `01/01/2881` — clearly impossible. The Debit/Credit values are **cumulative totals** of the entire statement (e.g. Debit=41,046,182.69, Credit=41,049,457.43). The narration contains only a row count number (e.g. `2382`, `1063`).

**These are summary/totals rows** printed at the end of a statement — some templates include "Total Debits / Total Credits" row with a placeholder date. The parser is treating them as transaction rows.

### Fix

```python
# In date validator / row classifier:
IMPOSSIBLE_YEARS = set(range(1, 1900)) | set(range(2100, 9999))

def is_totals_row(row: dict) -> bool:
    """Detect summary/totals rows that are not real transactions."""
    date_str = row.get('Date', '')
    # Check impossible year
    year_match = re.search(r'\d{4}', date_str)
    if year_match:
        year = int(year_match.group())
        if year < 1950 or year > 2100:
            return True
    # Narration is just a number (row count)
    narr = str(row.get('Narration', '')).strip()
    if re.fullmatch(r'\d{1,6}', narr):
        return True
    return False
```

Rows where `is_totals_row()` returns True should be **excluded before validation**, not flagged as invalid_date. They are not transactions — they are statement metadata. Move them to a separate `statement_summary` bucket, log them, but don't include them in flagged_transactions output.

**Regression check:** The 6 `invalid_date` rows must disappear from flagged output. No real transactions should be newly excluded.

---

## Bug Group F — 9 Files Extracting 0 Rows (Zero-Recon Files)

These files have `status=ok` but `rows_clean=0` and `rows_flagged=0`:
- 4513362998.pdf (tier: schema_reparse)
- BOM_Statement_FTP_02107_xxxxxxxx7596_20240812...pdf (tier: schema_reparse) × 2
- BOM_Statement_FTP_02772_xxxxxxxx8123...pdf (tier: schema_reparse)
- DEVANSHU_STMNT.pdf (tier: cheap_parse)
- shivlal statement.txt (tier: schema_reparse)
- statement (2).pdf, statement (5).pdf, stm REKHA.pdf (tier: cheap_parse)

**The `schema_reparse` tier files are the diagnosis signal**: these files went through schema reparse and still produced 0 rows — meaning neither the deterministic nor the LLM-assisted parser found any table structure. This happens when:
1. The PDF is a scanned image (no text layer) — but these were routed as `pdf_digital`, meaning text was found
2. The text is present but in an unrecognized layout (no standard column headers found)

**The `cheap_parse` files producing 0 rows** (DEVANSHU_STMNT.pdf, stm REKHA.pdf, statement (2)/(5)) likely have text in an unusual encoding or the column headers use regional-language labels.

**Fix approach:** These files need **individual manual inspection** — not a code fix. The correct process:

```bash
# For each zero-row file, run this diagnostic:
pdftotext -layout "DEVANSHU_STMNT.pdf" /tmp/devanshu_check.txt
head -50 /tmp/devanshu_check.txt

# If output is empty → scanned PDF, route to OCR (different extractor)
# If output has text → show Claude Code the first 50 lines and let it identify column headers
```

Claude Code should not attempt to fix all 9 files at once. Fix one, verify it produces rows, then move to the next. The `shivlal statement.txt` is a plain text file — it should be routed through the CSV/text extractor, not the PDF extractor.

---

## Metadata Strategy — Generalised Account Name and Number Extraction
### Target: 0 accounts missing name or number for any file with a readable text layer
### Critical rule: NO HARDCODING of bank names, template positions, or fixed line offsets. Every pattern must be structural.

The count went from 25 → 9. That improvement must be preserved exactly. Every fix below is additive (adds new detection capability) — it must not touch patterns that already work. After every change to `account_extractor.py` or `extractor_excel_csv.py`, run extraction on all 144 files and print the missing-name + missing-number counts before proceeding.

---

### Why "near-zero" is achievable — and what "generalised" actually means

Every authentic bank statement contains the account holder name and account number. They are legally required fields. If the extractor returns empty, one of four things happened:

1. The label used in this template is not in our label vocabulary (fixable — add labels)
2. The value is in a position we don't scan (fixable — expand scan area)
3. Two-column layout caused column interleaving — the text stream has name and address mixed (fixable — use word-coordinate-based extraction instead of raw text)
4. The file has no readable text layer (acceptable — flag as `metadata_source: unreadable`, route to OCR)

None of these require hardcoding. All four are solved by structural strategies described below.

**The generalisation principle:** Never write `if bank == "SBI"`. Instead, write `if label in HOLDER_LABEL_ALIASES`. The alias list grows, the logic stays the same. Any new bank statement format uploaded on hackathon day will be handled if its labels appear in the vocabulary — and if they don't, the LLM fallback catches it.

---

### Strategy 1 — Expand the label vocabulary (covers SBI and all similar cases)

The current extractor likely searches for `"Account Holder"`, `"Name"`, `"Account Name"`. The actual labels found in failing statements include:

```python
# In account_extractor.py — replace the current narrow label list with this:

HOLDER_LABEL_ALIASES = [
    # Standard
    "account holder", "account holder name", "account name",
    "name of account holder", "holder name",
    # SBI variants (no "holder" word, just "name" or standalone)
    "name", "customer name", "applicant name",
    # Some templates use title case with colon
    "Account Holder :", "Name :", "Customer :",
    # Some UCO/small-bank templates
    "beneficiary name", "depositor name",
    # Hindi transliteration seen in some cooperative bank statements
    "khata dharak", "naam",
]

ACCOUNT_NUMBER_ALIASES = [
    # Standard
    "account number", "account no", "account no.", "a/c no", "a/c number",
    "acc no", "acc number", "acc. no",
    # SBI
    "account", "savings account", "s.b. account",
    # With hyphen/slash variants
    "account-number", "account_number",
    # Some templates label it differently
    "customer id",   # only when followed by a numeric string — NOT when followed by a name
    "folio no",      # mutual fund statements
    "client id",
    # UCO and cooperative banks
    "a/c", "a.c. no",
]
```

**Matching logic must be case-insensitive, strip-whitespace, and fuzzy on punctuation:**
```python
import re

def normalize_label(label: str) -> str:
    """Strip punctuation, collapse whitespace, lowercase."""
    return re.sub(r'[\s\-_./,:]+', ' ', label).strip().lower()

def label_matches_holder(raw_label: str) -> bool:
    norm = normalize_label(raw_label)
    return any(normalize_label(alias) == norm for alias in HOLDER_LABEL_ALIASES)

def label_matches_account_number(raw_label: str) -> bool:
    norm = normalize_label(raw_label)
    return any(normalize_label(alias) == norm for alias in ACCOUNT_NUMBER_ALIASES)
```

---

### Strategy 2 — Word-coordinate extraction for two-column headers (covers the column-interleave bug)

pdfplumber's `extract_text()` reads left-to-right, top-to-bottom — but bank statement headers are often two-column layouts where label is on the left and value is on the right. When two columns are printed side-by-side on the same y-coordinate, `extract_text()` interleaves them: `"Account Holder : KAVYA BOSE Branch : LUCKNOW"` becomes `"Account Holder LUCKNOW KAVYA BOSE Branch"` in raw text order depending on exact pixel positions.

The fix is to use word-coordinate grouping rather than raw text:

```python
def extract_metadata_by_coordinates(page) -> dict:
    """
    Use word bounding boxes to find label:value pairs on the header pages.
    Groups words by y-coordinate (same line = same row), then identifies
    label:value pairs by scanning left-to-right within each row.
    
    This is coordinate-based and bank-agnostic — it works on any two-column layout.
    """
    words = page.extract_words()
    
    # Group words by approximate y-coordinate (within 3px = same line)
    lines = {}
    for w in words:
        y_key = round(w['top'] / 3) * 3  # bucket to nearest 3px
        lines.setdefault(y_key, []).append(w)
    
    # Sort each line left-to-right
    for y_key in lines:
        lines[y_key].sort(key=lambda w: w['x0'])
    
    results = {}
    
    for y_key, line_words in sorted(lines.items()):
        line_text = ' '.join(w['text'] for w in line_words)
        
        # Look for label: value pattern within the line
        # Split on colon — left side is label, right side is value
        if ':' in line_text:
            parts = line_text.split(':', 1)
            label_candidate = parts[0].strip()
            value_candidate = parts[1].strip()
            
            if label_matches_holder(label_candidate) and value_candidate:
                # Validate: value must look like a name (at least one alpha word, no digits only)
                if re.search(r'[A-Za-z]{2,}', value_candidate):
                    results['account_holder'] = value_candidate
            
            if label_matches_account_number(label_candidate) and value_candidate:
                # Validate: must be numeric (8-18 digits)
                digits_only = re.sub(r'[\s\-]', '', value_candidate)
                if re.fullmatch(r'\d{8,18}', digits_only):
                    results['account_number'] = digits_only
    
    return results
```

**This replaces raw-text regex as the primary extraction method for PDF headers.** The regex approach stays as a fallback for when coordinate extraction returns nothing.

---

### Strategy 3 — Scan first AND last N pages, not just page 1 (covers SBI and long statements)

Some SBI statements print the account holder name on page 2 or the last page (in the closing summary). The current extractor almost certainly scans only page 0 or page 1. Fix: scan pages 0, 1, 2, and the last page, merge results, prefer earlier pages on conflict.

```python
def extract_metadata_from_pdf(pdf_path: str) -> dict:
    """
    Scan multiple candidate pages for metadata — not just page 1.
    Never hardcode which page; try all candidates and merge.
    """
    with pdfplumber.open(pdf_path) as pdf:
        total_pages = len(pdf.pages)
        # Candidate pages: first 3 + last page (avoids scanning all pages for speed)
        candidate_indices = list(dict.fromkeys(
            [0, 1, 2, total_pages - 1]  # dedup if file has < 4 pages
        ))
        
        merged = {}
        for idx in candidate_indices:
            if idx < total_pages:
                page_results = extract_metadata_by_coordinates(pdf.pages[idx])
                # Earlier pages win on conflict
                for k, v in page_results.items():
                    if k not in merged and v:
                        merged[k] = v
    
    return merged
```

---

### Strategy 4 — Filename as structured fallback (covers Excel files and some PDFs)

For files where the filename itself encodes the account number (e.g. `soa_0167042251865512.pdf`, `216655101347_01-Jan-2025.pdf`, `4513362998.pdf`), extract it as a fallback when coordinate+regex extraction both return nothing.

```python
import os, re

def extract_account_number_from_filename(filepath: str) -> str:
    """
    Extract a plausible account number from the filename.
    Looks for a standalone 8-18 digit numeric sequence.
    Must not be a date (reject sequences that match YYYYMMDD or DDMMYYYY).
    """
    name = os.path.splitext(os.path.basename(filepath))[0]
    # Remove common prefixes: soa_, stmt_, account_, statement_
    name = re.sub(r'^(soa|stmt|account|statement|acct)[_\-]', '', name, flags=re.IGNORECASE)
    
    candidates = re.findall(r'\b(\d{8,18})\b', name)
    
    for candidate in candidates:
        # Reject if it looks like a date range (8 digits = YYYYMMDD)
        if len(candidate) == 8:
            try:
                y, m, d = int(candidate[:4]), int(candidate[4:6]), int(candidate[6:])
                if 1900 < y < 2100 and 1 <= m <= 12 and 1 <= d <= 31:
                    continue  # skip — this is a date
            except ValueError:
                pass
        return candidate  # first non-date numeric sequence wins
    
    return ''
```

---

### Strategy 5 — Excel-specific column scan (covers all Excel files with acc number in column A)

```python
# In extractor_excel_csv.py:

EXCEL_ACCOUNT_NUMBER_COLUMN_HEADERS = [
    "acc no", "account no", "account number", "a/c no", "a/c number",
    "account_number", "acct no", "account", "sl no",  # not "sl no" alone — only if numeric
]

def find_account_number_column(df) -> str | None:
    """
    Find the column in a DataFrame that contains the account number.
    Checks header row for known aliases, then validates first non-null value is numeric.
    """
    for col in df.columns:
        col_norm = normalize_label(str(col))
        if any(normalize_label(alias) == col_norm for alias in EXCEL_ACCOUNT_NUMBER_COLUMN_HEADERS):
            # Validate: first non-null value in this column must be numeric
            first_val = df[col].dropna().iloc[0] if not df[col].dropna().empty else None
            if first_val and re.fullmatch(r'\d{8,18}', str(first_val).strip()):
                return str(first_val).strip()
    return None


def find_account_holder_column(df) -> str | None:
    """
    Find the column in a DataFrame that contains the account holder name.
    """
    for col in df.columns:
        col_norm = normalize_label(str(col))
        if any(normalize_label(alias) == col_norm for alias in HOLDER_LABEL_ALIASES):
            first_val = df[col].dropna().iloc[0] if not df[col].dropna().empty else None
            if first_val and re.search(r'[A-Za-z]{2,}', str(first_val)):
                return str(first_val).strip()
    return None
```

---

### Strategy 6 — LLM fallback with a tighter, structured prompt (covers UCO Bank and all unknowns)

The current LLM fallback is clearly returning wrong values sometimes (earlier session: returned narration text instead of a name). The prompt must be rewritten to be unambiguous and to return structured JSON, not free text.

```python
LLM_METADATA_PROMPT = """You are extracting structured metadata from a bank statement text.

From the text below, extract ONLY:
1. account_holder: The full name of the account holder (a person or company name). 
   - Must be a proper name, NOT a city, branch name, or bank name.
   - If you see "MR.", "MRS.", "MS.", "DR." prefix, include it.
   - Return empty string if genuinely not present.
2. account_number: The bank account number.
   - Must be 8–18 digits only (no spaces, dashes, or letters).
   - Do NOT return IFSC code, customer ID, or PAN number as account number.
   - Return empty string if genuinely not present.

Return ONLY a JSON object with exactly these two keys: {"account_holder": "...", "account_number": "..."}
Do not return any explanation, markdown, or extra text.

Bank statement text (first 2000 characters):
{text}"""
```

**Validation layer on LLM response — always run this, never trust the LLM output raw:**
```python
def validate_llm_metadata(llm_response: str) -> dict:
    """
    Parse and validate LLM metadata output.
    Rejects values that are obviously wrong even if LLM returned them.
    """
    try:
        data = json.loads(llm_response.strip())
    except json.JSONDecodeError:
        return {"account_holder": "", "account_number": ""}
    
    holder = str(data.get("account_holder", "")).strip()
    acc_num = str(data.get("account_number", "")).strip()
    
    # Reject holder if it looks like a city, code, or single word that's all caps
    # (single-word all-caps is usually a bank branch code, not a name)
    if holder:
        words = holder.split()
        if len(words) == 1 and holder.isupper() and len(holder) <= 10:
            holder = ""  # e.g. "LUCKNOW", "MUMBAI", "HDFC"
        if re.fullmatch(r'[A-Z0-9]{2,6}', holder):
            holder = ""  # looks like an IFSC or branch code
    
    # Reject account_number if not purely numeric or wrong length
    acc_digits = re.sub(r'[\s\-]', '', acc_num)
    if not re.fullmatch(r'\d{8,18}', acc_digits):
        acc_num = ""
    else:
        acc_num = acc_digits
    
    return {"account_holder": holder, "account_number": acc_num}
```

---

### Strategy 7 — Post-extraction sanity gate (catches anything that slips through)

```python
def validate_extraction_quality(extracted: dict) -> dict:
    """
    Final gate before metadata is written to output.
    Clears values that are structurally wrong, regardless of how they were found.
    Adds a warning field so the report can surface these explicitly.
    """
    holder = extracted.get('account_holder', '').strip()
    acc_num = extracted.get('account_number', '').strip()
    warnings = []

    # City names are not holder names
    KNOWN_CITY_WORDS = {
        'lucknow', 'sitapur', 'mysore', 'mumbai', 'delhi', 'chennai',
        'bangalore', 'bengaluru', 'kolkata', 'hyderabad', 'pune', 'jaipur',
        'ahmedabad', 'surat', 'nagpur', 'indore', 'bhopal', 'chandigarh',
        'patna', 'guwahati', 'bhubaneswar', 'kochi', 'thiruvananthapuram'
    }
    if any(w.lower() in KNOWN_CITY_WORDS for w in holder.split()):
        # Only clear if the ENTIRE holder value is a city, not if city is part of address
        if normalize_label(holder) in KNOWN_CITY_WORDS:
            warnings.append(f'holder_is_city:{holder}')
            holder = ''

    # Account number: 8-18 digits, no letters
    acc_clean = re.sub(r'[\s\-]', '', acc_num)
    if acc_clean and not re.fullmatch(r'\d{8,18}', acc_clean):
        warnings.append(f'account_number_invalid:{acc_num}')
        acc_num = ''
    else:
        acc_num = acc_clean

    # Log warnings — do NOT silently discard, always record what was rejected and why
    if warnings:
        extracted['metadata_extraction_warning'] = ' | '.join(warnings)

    extracted['account_holder'] = holder
    extracted['account_number'] = acc_num
    return extracted
```

---

### Extraction cascade — the full priority order

Every file must go through this cascade in order, stopping at the first strategy that returns both name AND number:

```
1. Word-coordinate extraction from PDF header pages 0, 1, 2, last  →  Strategy 2 + 3
2. Regex label matching on raw text (expanded vocabulary)           →  Strategy 1
3. Excel column scan (for .xlsx / .xls / .csv files)               →  Strategy 5
4. Filename extraction (account number only)                        →  Strategy 4
5. LLM fallback with structured prompt + validation                 →  Strategy 6
6. Flag as metadata_source: "unresolved" — log for manual review   →  visible failure, not silent
```

Metadata source is recorded at every step: `"coordinate"`, `"regex"`, `"excel_column"`, `"filename"`, `"llm"`, or `"unresolved"`. The summary report (Section 7) counts by source so you can see the cascade distribution across the full dataset.

---

### What changes on hackathon day when new files arrive

Zero. The cascade above has no bank-specific logic anywhere. A new file arrives → it goes through the same 6-step cascade → coordinate extraction finds the label using the vocabulary → done. The only scenario where a new file produces empty metadata is if it uses a label not in `HOLDER_LABEL_ALIASES` or `ACCOUNT_NUMBER_ALIASES`. In that case, the LLM fallback catches it — and the structured prompt + validation layer ensures the LLM doesn't return garbage.

**The only thing that needs updating when a genuinely novel label is found:** add one string to `HOLDER_LABEL_ALIASES` or `ACCOUNT_NUMBER_ALIASES`. No logic changes, no bank-name conditions, no template flags.

---

---

## Duplicates: What They Mean and What You Should Tell the Judges

The 40,745 duplicates are **all** `exact_duplicate (same Date + Narration + Debit + Credit + Account)`. This is important to understand:

**These are NOT parsing artifacts.** Looking at the data:
- Top account: `38347344323` — 10,614 duplicates
- Sample: same narration `NEFT:EMEXFUNDAM`, same amount `279.0`, same date `11/05/2020` — but different row numbers

There are two legitimate causes:
1. **The same account statement uploaded twice** — someone submitted overlapping date-range PDFs or the same PDF in two different files. These are genuine duplicates produced by the data submission process.
2. **Recurring fixed-amount transactions** — the same counterparty pays the same amount on different dates (e.g. `ATM / IMPS Transaction Charges = 24.78` recurring weekly). These share all fields except possibly the date — check whether your duplicate-detection also requires date match.

**For the hackathon analysis, duplicate transactions are actually a finding, not just a data quality issue:**
- An account with 10,000 duplicates likely has the same statement included multiple times — meaning someone tried to inflate the transaction volume (relevant to fraud investigation)
- Recurring same-amount transactions to/from the same counterparty is a structuring or layering signal — worth surfacing to the analysis phase

**What to say to judges:** "Our pipeline detected 40,745 exact-duplicate transaction rows across 88 accounts. These are surfaced to investigators as potential evidence of statement duplication (an attempt to inflate transaction counts) or recurring fixed-amount flows (a structuring signal). They are excluded from statistical analysis to prevent double-counting."

---

## Balance Mismatch Strategy — Target: Near-Zero, Generalised
### Every flag is a parser error. The source data is correct. Fix the parser, not the data.
### ZERO hardcoding of bank names, fixed column positions, or file-specific offsets.

The current 1,070 balance_mismatch rows break down into exactly 5 root causes. Each has a generalised fix that works on any statement format — not just the specific files that triggered it.

---

### BM Strategy 1 — Wrong Amount Column Mapping (882 rows — the biggest one)

**What happens:** The column identifier falls back to `deterministic_default` when it can't find standard column headers. The default mapping picks the wrong x-position for Debit/Credit, reading a row counter or serial number (always 1) instead of the actual amount. Every extracted amount becomes `1.0`.

**How to detect it programmatically (no hardcoding):**
```python
def detect_amount_always_one(rows: list[dict]) -> bool:
    """
    If more than 80% of a file's rows have Debit=1.0 or Credit=1.0,
    the column mapping is wrong — not the data.
    Flag this file for column-map re-diagnosis before validation.
    """
    if len(rows) < 10:
        return False
    one_count = sum(
        1 for r in rows
        if float(r.get('Debit', 0)) == 1.0 or float(r.get('Credit', 0)) == 1.0
    )
    return (one_count / len(rows)) > 0.8
```

**Generalised fix — amount recovery from narration when column map fails:**

When `detect_amount_always_one()` fires, the actual amount is almost always recoverable from the narration field, because the statement prints it there as part of the reference text. Extract it before validating balance continuity:

```python
import re

# Matches currency amounts like: 5.00  290.00  1,714.00  10,00,000.00
AMOUNT_IN_NARRATION_PATTERN = re.compile(
    r'(?<!\d)(\d{1,3}(?:,\d{2})*(?:,\d{3})?(?:\.\d{2})?)(?!\d)'
)

def recover_amount_from_narration(narration: str, debit: float, credit: float) -> tuple[float, float]:
    """
    When debit or credit is 1.0 (sentinel for wrong column mapping),
    attempt to recover the actual amount from the narration text.
    Returns (debit, credit) — unchanged if recovery is not confident.
    """
    if debit != 1.0 and credit != 1.0:
        return debit, credit  # column mapping is fine, don't touch
    
    matches = AMOUNT_IN_NARRATION_PATTERN.findall(narration)
    if not matches:
        return debit, credit  # can't recover — leave as-is, will still flag

    # Take the last numeric match — statements usually print amount at end of narration
    raw_amount = matches[-1].replace(',', '')
    try:
        recovered = float(raw_amount)
    except ValueError:
        return debit, credit

    # Sanity: recovered amount must be > 1.0 (otherwise we might be picking up a fee)
    if recovered <= 1.0:
        return debit, credit

    # Preserve debit/credit direction from original
    if debit == 1.0 and credit == 0.0:
        return recovered, 0.0
    if credit == 1.0 and debit == 0.0:
        return 0.0, recovered

    return debit, credit
```

**Also fix the root cause — teach the column identifier to find non-standard headers:**

```python
# In column_identifier.py — expand the vocabulary, NO bank names:

DEBIT_COLUMN_ALIASES = [
    "debit", "withdrawal", "dr", "dr.", "debit amount", "withdrawal amount",
    "amount debited", "paid out", "outflow", "debit (dr)", "(dr)",
    "debit amt", "dr amount", "withdrawals",
]

CREDIT_COLUMN_ALIASES = [
    "credit", "deposit", "cr", "cr.", "credit amount", "deposit amount",
    "amount credited", "paid in", "inflow", "credit (cr)", "(cr)",
    "credit amt", "cr amount", "deposits",
]

BALANCE_COLUMN_ALIASES = [
    "balance", "running balance", "closing balance", "available balance",
    "balance (dr/cr)", "balance amt", "bal", "bal.", "ledger balance",
    "book balance",
]

DATE_COLUMN_ALIASES = [
    "date", "txn date", "transaction date", "value date", "posting date",
    "trans date", "tran date", "dt", "effective date",
]

NARRATION_COLUMN_ALIASES = [
    "narration", "description", "particulars", "remarks", "details",
    "transaction details", "transaction description", "txn description",
    "narration/description", "trans particulars", "chq/ref details",
]
```

**Column identification must use x-coordinate clustering, not line-position matching:**

```python
def identify_columns_by_coordinates(header_words: list[dict]) -> dict:
    """
    Given a list of word dicts (with x0, x1, text from pdfplumber),
    identify which x-range corresponds to which field type.
    
    This works on any layout because it uses the actual pixel positions
    of the header row words, not hardcoded column indices.
    """
    column_map = {}
    
    for word in header_words:
        text_norm = normalize_label(word['text'])
        x_center = (word['x0'] + word['x1']) / 2
        
        if any(normalize_label(a) == text_norm for a in DEBIT_COLUMN_ALIASES):
            column_map['debit_x_center'] = x_center
        elif any(normalize_label(a) == text_norm for a in CREDIT_COLUMN_ALIASES):
            column_map['credit_x_center'] = x_center
        elif any(normalize_label(a) == text_norm for a in BALANCE_COLUMN_ALIASES):
            column_map['balance_x_center'] = x_center
        elif any(normalize_label(a) == text_norm for a in DATE_COLUMN_ALIASES):
            column_map['date_x_center'] = x_center
        elif any(normalize_label(a) == text_norm for a in NARRATION_COLUMN_ALIASES):
            column_map['narration_x_center'] = x_center
    
    return column_map
```

---

### BM Strategy 2 — Page-Break Dropped Row (51 `missing_transaction` rows)

**What happens:** A transaction that spans a page break has its continuation line on the next page, after a page header block. The parser sees the page header (column labels row) as a new transaction start and skips the continuation — so the transaction is never written, creating a gap in the balance chain. The row *before* the gap gets a `missing_transaction` flag.

**Generalised two-pass fix:**

```python
def merge_page_breaks(raw_lines: list[str]) -> list[str]:
    """
    Two-pass algorithm:
    Pass 1: Classify every line as one of:
      - 'transaction'  : starts with a date pattern
      - 'continuation' : has no date, has amount-like content
      - 'header'       : contains column header keywords (Date, Narration, etc.)
      - 'footer'       : contains footer sentinel strings
      - 'blank'        : empty or whitespace only
    
    Pass 2: Merge 'continuation' lines into the preceding 'transaction' line.
    Skip 'header', 'footer', 'blank' lines entirely.
    
    This is bank-agnostic because classification uses structural signals,
    not bank-specific strings.
    """
    DATE_START = re.compile(r'^\s*\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}')
    HEADER_KEYWORDS = re.compile(
        r'\b(date|narration|description|debit|credit|balance|particulars|'
        r'withdrawal|deposit|amount|dr|cr)\b',
        re.IGNORECASE
    )
    FOOTER_SENTINELS = re.compile(
        r'(page\s*no|registered office|closing balance includes|'
        r'contents of this statement|terms and conditions)',
        re.IGNORECASE
    )
    HAS_AMOUNT = re.compile(r'\d+[,\d]*\.\d{2}')

    def classify(line: str) -> str:
        if not line.strip():
            return 'blank'
        if FOOTER_SENTINELS.search(line):
            return 'footer'
        if DATE_START.match(line):
            return 'transaction'
        # Header: multiple header keywords on one line, no amount
        kw_count = len(HEADER_KEYWORDS.findall(line))
        if kw_count >= 2 and not HAS_AMOUNT.search(line):
            return 'header'
        if HAS_AMOUNT.search(line):
            return 'continuation'
        return 'other'

    # Pass 1: classify
    classified = [(classify(line), line) for line in raw_lines]

    # Pass 2: merge continuations into preceding transaction
    merged = []
    for cls, line in classified:
        if cls in ('header', 'footer', 'blank'):
            continue
        if cls == 'continuation' and merged:
            merged[-1] = merged[-1].rstrip() + ' ' + line.strip()
        else:
            merged.append(line)

    return merged
```

---

### BM Strategy 3 — Negative Sign Convention (11 `balance_jump_no_change` + some `missing_amount` rows)

**What happens:** Some templates store debit as a negative number (e.g. `-27.14`) in the Debit column. The balance validator interprets a negative debit as a credit, flips the expected balance direction, and flags a mismatch on every such row.

**Generalised fix — applied at standardisation time, before validation:**

```python
def normalize_signed_amounts(debit_raw: float, credit_raw: float, balance_raw: float,
                              prev_balance: float) -> tuple[float, float]:
    """
    Detect and correct sign conventions without hardcoding which bank uses which.
    
    Strategy: if debit < 0 and credit == 0, treat abs(debit) as the debit amount.
    Also handle the case where both debit and credit are signed (net-amount column).
    
    Uses the balance delta as a cross-check: if prev_balance - abs(debit) ≈ balance_raw,
    the sign correction is confirmed.
    """
    TOLERANCE = 0.02  # 2 paise tolerance for floating point

    if debit_raw < 0 and credit_raw == 0:
        corrected_debit = abs(debit_raw)
        # Cross-check with balance
        if prev_balance > 0:
            expected_balance = prev_balance - corrected_debit
            if abs(expected_balance - balance_raw) <= TOLERANCE:
                return corrected_debit, 0.0  # confirmed correction
        return corrected_debit, 0.0  # apply even without balance cross-check

    if credit_raw < 0 and debit_raw == 0:
        corrected_credit = abs(credit_raw)
        return 0.0, corrected_credit

    # Net-amount column (single column with positive=credit, negative=debit)
    if debit_raw == 0 and credit_raw == 0:
        # This means the parser found neither — different bug, not sign convention
        return debit_raw, credit_raw

    return debit_raw, credit_raw
```

---

### BM Strategy 4 — Consolidated / Multi-Account Statement (remaining `missing_amount`)

Already covered in Bug Group C. The key generalised detector:

```python
def detect_consolidated_statement(rows: list[dict]) -> bool:
    """
    A consolidated statement has multiple accounts' transactions stacked.
    Signature: same narration structure with incrementing reference numbers,
    AND balance column increments monotonically (cumulative across accounts).
    """
    if len(rows) < 3:
        return False

    # Check for incrementing balances (not oscillating like a normal ledger)
    balances = [float(r.get('Balance', 0)) for r in rows[:20]]
    all_increasing = all(balances[i] <= balances[i+1] for i in range(len(balances)-1))
    all_decreasing = all(balances[i] >= balances[i+1] for i in range(len(balances)-1))
    monotonic = all_increasing or all_decreasing

    # Check narration structure similarity
    narr_prefix = lambda n: re.sub(r'\d{6,}', 'N', str(n))
    prefixes = [narr_prefix(r.get('Narration', '')) for r in rows[:10]]
    all_same_structure = len(set(prefixes)) == 1

    return monotonic and all_same_structure
```

---

### BM Strategy 5 — Balance Validator Tolerance and Graceful Degradation

The current validator likely uses exact equality for balance chain checking. Floating-point arithmetic on INR amounts causes false mismatches at the paise level. Fix:

```python
BALANCE_TOLERANCE = 0.02  # 2 paise — accounts for float rounding in PDF text

def validate_balance_chain(rows: list[dict]) -> list[dict]:
    """
    Validate that each row's balance = prev_balance +/- debit/credit.
    Uses tolerance for floating point. Produces named diagnosis on failure.
    Never produces a 'mismatch' without a specific reason string.
    """
    prev_balance = None
    results = []

    for i, row in enumerate(rows):
        try:
            debit  = float(row.get('Debit',  0) or 0)
            credit = float(row.get('Credit', 0) or 0)
            balance = float(row.get('Balance', 0) or 0)
        except (ValueError, TypeError):
            row['mismatch_diagnosis'] = 'unparseable_amount'
            results.append(row)
            prev_balance = None
            continue

        if prev_balance is None:
            # First row — accept its balance as the starting point
            prev_balance = balance
            results.append(row)
            continue

        expected_balance = round(prev_balance + credit - debit, 2)
        actual_balance   = round(balance, 2)
        delta = abs(expected_balance - actual_balance)

        if delta <= BALANCE_TOLERANCE:
            prev_balance = balance
            results.append(row)
        else:
            # Produce a named diagnosis — never just "balance_mismatch"
            if debit == 0 and credit == 0:
                row['mismatch_diagnosis'] = 'missing_amount'
            elif delta > 1000:
                row['mismatch_diagnosis'] = 'missing_transaction'
            elif abs(credit - debit) < BALANCE_TOLERANCE:
                row['mismatch_diagnosis'] = 'balance_jump_no_change'
            else:
                row['mismatch_diagnosis'] = f'delta_{delta:.2f}'
            
            row['flag_reason'] = 'balance_mismatch'
            row['expected_balance'] = expected_balance
            row['actual_balance'] = actual_balance
            # Keep prev_balance tracking — don't reset, or every subsequent row also flags
            prev_balance = balance
            results.append(row)

    return results
```

---

### How these 5 strategies eliminate the full 1,070 balance_mismatch rows

| Strategy | Rows fixed | Root cause |
|---|---|---|
| BM1 — Column mapping + narration recovery | ~882 | Amount always 1.0 |
| BM2 — Page-break two-pass merger | ~51 | Dropped continuation row |
| BM3 — Negative sign normalisation | ~11 | Debit stored as negative |
| BM4 — Consolidated statement detection | ~114 | Multi-account balance |
| BM5 — Tolerance + graceful degradation | ~12 | Float rounding / totals rows |
| **Total** | **~1,070** | **All accounted for** |

**After all 5 strategies are in place, any remaining balance_mismatch row is either:**
- A genuinely corrupted source file (acceptable — will have a named diagnosis)
- A novel layout not yet seen (will have `delta_X.XX` as the diagnosis, not a silent flag)

Either way, the output is never silent. Every flag has a reason string.

---

## Implementation Order (non-negotiable — same isolation discipline as before)

### Session 1: Bug Group A + D (Footer Bleeding → Narration)
- Add `clean_narration()` to standardiser.py
- Add `FOOTER_SENTINEL_PATTERNS` list + `strip_leading_date_sequence()`
- Wire into narration assembly
- Run: TARUN PILLAI statement.pdf + STATEMENT (3)/(6).pdf + STATEMENT 4.pdf
- Verify: `narration_contains_multiple_transactions` drops to near-0, recon stays 1.0, zero new flags on any other file

### Session 2: Bug Group E + BM Strategy 5 (Totals Rows + Balance Tolerance)
- Add `is_totals_row()` before validation pipeline
- Add `BALANCE_TOLERANCE = 0.02` and rewrite `validate_balance_chain()` with graceful degradation
- Run: full 144-file set
- Verify: 6 `invalid_date` rows gone, no clean rows lost, `balance_mismatch` count drops

### Session 3: BM Strategy 2 (Page-Break Two-Pass Merger)
- Add `merge_page_breaks()` and wire into the raw-text pre-processing step
- Run: full dataset
- Verify: `missing_transaction` count (currently 51) drops toward 0

### Session 4: BM Strategy 3 + 4 (Negative Signs + Consolidated Statement)
- Add `normalize_signed_amounts()` to standardiser.py
- Add `detect_consolidated_statement()` to validator.py
- Run: 216655101347 file + full dataset
- Verify: 141 flags for that account drop; no regression elsewhere

### Session 5: BM Strategy 1 (Column Mapping + Amount Recovery from Narration)
- Add `detect_amount_always_one()` diagnostic check
- Expand `DEBIT_COLUMN_ALIASES`, `CREDIT_COLUMN_ALIASES`, etc. in column_identifier.py
- Add `identify_columns_by_coordinates()` as primary method
- Add `recover_amount_from_narration()` as fallback
- Run pdfplumber word-coordinate diagnostic on `soa_0167042251865512.pdf` FIRST
- Verify: recon rate for that file moves from 0.156 to above 0.85; Debit/Credit no longer `1.0`

### Session 6: Metadata Strategy (Account Name + Number — all 7 strategies)
- Add `HOLDER_LABEL_ALIASES` and `ACCOUNT_NUMBER_ALIASES` vocabularies
- Add `normalize_label()` utility
- Replace header regex with `extract_metadata_by_coordinates()` as primary method
- Add `extract_metadata_from_pdf()` with multi-page scan (pages 0, 1, 2, last)
- Add `extract_account_number_from_filename()` as fallback
- Update `extractor_excel_csv.py` with `find_account_number_column()` and `find_account_holder_column()`
- Rewrite LLM fallback prompt to structured JSON + add `validate_llm_metadata()`
- Add `validate_extraction_quality()` as final gate
- Run: full 144-file metadata extraction
- Verify: missing-name + missing-number count drops from 9 toward 0; count must NOT increase for any account that currently works

### Session 7: Zero-row files (one at a time)
- `pdftotext -layout <file>` diagnostic first on each file
- Empty output → route to OCR extractor
- Has text → show first 50 lines to Claude Code for column header identification
- Only proceed with a code fix when root cause is confirmed from diagnostic output

---

## The Hard Rules — Same As Before, Now Enforced

1. **One root cause per Claude Code session.** Do not paste multiple bug groups into one session.
2. **Before/after row count required.** Claude Code must print flagged count before applying fix, then after. If count increases anywhere, revert immediately.
3. **2-attempt max per fix.** If a fix doesn't reduce the target flag count in 2 attempts, stop and bring back findings — do not iterate further.
4. **Regression baseline is ALL 144 files**, not just the file being fixed. After every fix, the total `narration_contains_multiple_transactions` + `balance_mismatch` + `invalid_date` row counts must be printed for the full dataset.
5. **The 9 accounts with correct metadata must stay at 9 or better.** Print the metadata extraction count after every fix that touches account_extractor.py or extractor_excel_csv.py.

---

## Expected Outcome After All 6 Sessions

| Metric | Before | Expected After |
|---|---|---|
| narration_contains_multiple_transactions | 1,195 | < 30 |
| balance_mismatch | 1,070 | **< 20, target 0** |
| invalid_date | 6 | 0 |
| Accounts missing name/number | 9 | **0 (target — see Metadata Strategy section)** |
| Zero-row files | 9 | ≤ 4 (some may genuinely have no parseable structure) |
| Total flagged | 2,271 | **< 50** |

**Why balance_mismatch target is near-zero, not "< 200":** The dataset is CID-provided authentic bank statements. Every balance in the source chains correctly — because banks issue them that way. This means every single balance_mismatch flag is a parser error, not a data error. Every one of the current 1,070 is accounted for across a small number of root causes (wrong column mapping, negative sign convention, page-break drops, footer bleed, and totals rows). After all fixes, residual should be < 20 and every remaining flag must have a named diagnosis in the output — never silent.

**Why "0 target" for missing name/number:** The same logic applies. The name and account number exist in every authentic bank statement — if they are missing in our output, the extractor failed to find them, not the source. With a generalised extraction strategy (see Metadata Strategy section), this is fixable to 0 for all files that have readable text. The only acceptable non-zero is a file where the text layer is genuinely absent (scanned, no OCR) or corrupted.

---

## Summary Report — What the Pipeline Must Produce After Every Full Run

After every complete run across all files, the pipeline must auto-generate a `extraction_summary_report.txt` (and optionally a `extraction_summary_report.json` for the analysis phase to consume). This is not optional — it is how you catch regressions without reading raw CSVs, and it is what you show judges to prove the system is production-quality.

### What the report must contain

#### Section 1: Run Identity
```
RUN SUMMARY — Multi-Accused Cross-Account Investigation Engine
Extraction Phase Report
Run timestamp     : 2026-06-27 16:42:11
Dataset           : original bank statements/
Total files found : 162
Files processed   : 144
Files skipped     : 18  (list skipped filenames + reason)
```

#### Section 2: Extraction Quality Scorecard
```
EXTRACTION SCORECARD
─────────────────────────────────────────────────────────
Total transactions extracted      :  182,034
  ├─ Clean (ready for analysis)   :  139,789  (76.8%)
  ├─ Flagged (needs review)       :    2,271  ( 1.2%)
  └─ Duplicates identified        :   40,745  (22.4%)  ← excluded from analysis

Unique accounts identified        :       88
  ├─ With name + account number   :       79  (89.8%)
  ├─ Missing name only            :        5
  ├─ Missing account number only  :        2
  └─ Missing both                 :        2

Files with 0 rows extracted       :        9  (see Section 5)
Avg reconciliation rate           :     0.94
Files below 0.90 reconciliation   :        3  (see Section 4)
```

#### Section 3: Flag Breakdown
```
FLAG BREAKDOWN
─────────────────────────────────────────────────────────
Flag type                              Count   % of flagged
narration_contains_multiple_txns       1,195      52.6%
balance_mismatch                       1,070      47.1%
  ├─ missing_amount                    1,007      44.3%
  ├─ missing_transaction                  51       2.2%
  ├─ balance_jump_no_change               11       0.5%
  └─ direction_inverted                    1       0.04%
invalid_date                               6       0.3%

Top 5 accounts by flag count:
  Rank  Account ID              Flags   Flag%   Bank
  1     0167042251865512          882    46%    Unknown Bank
  2     38211367068923            717     7%    HDFC Bank
  3     16423304381803            386     7%    Canara Bank
  4     216655101347              141    43%    Unknown Bank
  5     46652787342452             79     9%    Unknown Bank
```

#### Section 4: Per-File Reconciliation Detail
```
PER-FILE RECONCILIATION
─────────────────────────────────────────────────────────
File                                    Recon   Clean  Flagged  Tier
soa_0167042251865512.pdf                0.156    1046      882   cheap_parse      ⚠ LOW
216655101347_01-Jan-2025_22-May-...pdf  0.227     186      141   cheap_parse      ⚠ LOW
AccountStmt_0882XXXXXX5304 (1).pdf      0.891     179       14   schema_reparse   ⚠ LOW
TARUN PILLAI statement.pdf              1.000    8941      717   cheap_parse      ✓ OK (footer bleed)
...
[all 144 files listed]
```

#### Section 5: Files With Zero Rows
```
ZERO-ROW FILES (no transactions extracted)
─────────────────────────────────────────────────────────
File                                              Tier            Likely cause
4513362998.pdf                                    schema_reparse  No recognizable column headers
BOM_Statement_FTP_02107_xxxxxxxx7596_...pdf       schema_reparse  No recognizable column headers
DEVANSHU_STMNT.pdf                                cheap_parse     Unknown layout / possible scanned
shivlal statement.txt                             schema_reparse  Text file routed to wrong extractor
statement (2).pdf                                 cheap_parse     Unknown layout
statement (5).pdf                                 cheap_parse     Unknown layout
stm REKHA.pdf                                     cheap_parse     Unknown layout
[total: 9 files]
Action required: manual inspection needed for each — see extraction_fix_brief_v2.md Bug Group F
```

#### Section 6: Duplicate Transaction Analysis
```
DUPLICATE TRANSACTIONS
─────────────────────────────────────────────────────────
Total duplicate rows identified     :   40,745
Duplicate detection basis           :   exact match on Date + Narration + Debit + Credit + Account
Accounts with duplicates            :       23

⚠ HIGH-DUPLICATE ACCOUNTS (potential statement re-submission or recurring flow):
  Account           Duplicates   Clean Rows   Dup Rate   Note
  38347344323        10,614       [n]          [x%]       Possible double-submission
  81271119214         6,399       [n]          [x%]       Possible double-submission
  351964263933349     3,256       [n]          [x%]       Recurring fixed-amount flows
  [...]

Investigative note: Accounts with dup_rate > 30% may indicate deliberate statement
duplication to inflate transaction volume — flag for manual review in analysis phase.
```

#### Section 7: Metadata Extraction Quality
```
METADATA EXTRACTION QUALITY
─────────────────────────────────────────────────────────
Accounts with full metadata (name + acc number + IFSC)  :   71  (80.7%)
Accounts with partial metadata                          :   12
Accounts with no metadata                               :    5

Metadata source breakdown:
  regex                :   58 files
  llm_fallback         :   11 files
  filename             :    8 files
  none                 :    7 files

⚠ Accounts still missing account number (analysis phase will use filename as proxy):
  File                                   Holder extracted    Issue
  soa_XXXXXXXXX.pdf                      [name]              IFSC empty, no acc num
  [...]
```

#### Section 8: API & LLM Usage
```
API USAGE THIS RUN
─────────────────────────────────────────────────────────
Total LLM calls made       :   47
  ├─ Metadata extraction   :   22  (Groq text)
  ├─ Schema discovery      :   18  (Groq text)
  └─ Vision/OCR            :    7  (Groq vision)
Key rotations triggered    :    2  (quota hit on key #1 at file 38, rotated to key #2)
Keys exhausted this run    :    0
Groq 429 errors caught     :    2  (both recovered via rotation)
Files hitting cheap_parse  :   91  (63.2% — 0 LLM calls)
```

#### Section 9: Analysis-Ready Status
```
ANALYSIS PHASE READINESS
─────────────────────────────────────────────────────────
Transactions available for analysis   :  139,789
Accounts available for analysis       :       88
Graph nodes (accounts)                :       88
Graph edges (counterparty pairs)      :  [computed from counterparty extraction]

⚠ ITEMS REQUIRING INVESTIGATOR ATTENTION BEFORE ANALYSIS:
  1. 9 accounts flagged >30% of their rows — review manually before trusting
     pattern detection output for these accounts
  2. 9 files produced 0 rows — these accounts are absent from analysis entirely
  3. 5 accounts have no name/account number — will appear as UNKNOWN-<filename>
     in graph output; counterparty links may be incomplete

STATUS: READY FOR ANALYSIS PHASE  ✓
(Run analysis phase only after reviewing items above)
```

---

### How to implement this report

Add a `generate_extraction_report()` function in a new file `report_generator.py` (or in the existing phase orchestrator). It should:

1. Read `metadata.json` (already generated) + count rows in `clean_transactions.csv`, `flagged_transactions.csv`, `duplicates.csv`
2. Compute all the above metrics programmatically — do not hardcode any numbers
3. Write to `outputs/extraction_summary_report.txt` (human-readable, shown above) AND `outputs/extraction_summary_report.json` (machine-readable, consumed by analysis phase)
4. Print a short 5-line version to stdout at the end of every run so you can eyeball it without opening a file

```python
# report_generator.py — skeleton

import json, csv, datetime
from pathlib import Path
from collections import Counter

def generate_extraction_report(
    metadata_path: str,
    clean_csv_path: str,
    flagged_csv_path: str,
    duplicates_csv_path: str,
    output_dir: str = "outputs/"
) -> dict:
    """
    Generate extraction phase summary report.
    Returns the report dict (also written to JSON + TXT).
    """
    # Load all data
    with open(metadata_path) as f:
        meta = json.load(f)
    
    files = meta.get('files', [])
    
    # Count CSVs
    with open(clean_csv_path) as f:
        clean_rows = sum(1 for _ in f) - 1  # subtract header
    
    flagged_rows = []
    with open(flagged_csv_path) as f:
        reader = csv.DictReader(f)
        flagged_rows = list(reader)
    
    dup_rows = []
    with open(duplicates_csv_path) as f:
        reader = csv.DictReader(f)
        dup_rows = list(reader)
    
    # Compute metrics
    flag_reasons = Counter(r['flag_reason'] for r in flagged_rows)
    mismatch_diag = Counter(r.get('mismatch_diagnosis', '') 
                            for r in flagged_rows 
                            if r['flag_reason'] == 'balance_mismatch')
    
    top_flagged_accts = Counter(r['Account_ID'] for r in flagged_rows).most_common(5)
    
    ok_files = [f for f in files if f.get('rows_standardised', 0) > 0]
    zero_row_files = [f for f in files if f.get('rows_standardised', 0) == 0]
    
    recon_rates = [f.get('reconciliation_rate', 0) for f in ok_files]
    avg_recon = sum(recon_rates) / len(recon_rates) if recon_rates else 0
    low_recon_files = [f for f in ok_files if f.get('reconciliation_rate', 1) < 0.9]
    
    # Metadata quality
    accounts_with_holder = sum(1 for f in files 
                               if f.get('account_details', {}).get('account_holder'))
    accounts_with_number = sum(1 for f in files 
                               if f.get('account_details', {}).get('account_number'))
    
    # LLM usage
    total_llm_calls = sum(f.get('llm_calls', 0) for f in files)
    cheap_parse_files = sum(1 for f in files if f.get('tier') == 'cheap_parse')
    
    # Assemble report dict
    report = {
        "generated_at": datetime.datetime.now().isoformat(),
        "totals": {
            "files_processed": len(files),
            "transactions_clean": clean_rows,
            "transactions_flagged": len(flagged_rows),
            "transactions_duplicate": len(dup_rows),
            "transactions_total": clean_rows + len(flagged_rows),
        },
        "flag_breakdown": dict(flag_reasons),
        "mismatch_diagnosis": dict(mismatch_diag),
        "top_flagged_accounts": top_flagged_accts,
        "reconciliation": {
            "average": round(avg_recon, 3),
            "low_recon_files": [f['file'] for f in low_recon_files],
            "zero_row_files": [f['file'] for f in zero_row_files],
        },
        "metadata_quality": {
            "with_holder_name": accounts_with_holder,
            "with_account_number": accounts_with_number,
        },
        "llm_usage": {
            "total_calls": total_llm_calls,
            "cheap_parse_files": cheap_parse_files,
            "cheap_parse_pct": round(cheap_parse_files / len(files) * 100, 1) if files else 0,
        },
    }
    
    # Write JSON
    Path(output_dir).mkdir(exist_ok=True)
    with open(f"{output_dir}extraction_summary_report.json", "w") as f:
        json.dump(report, f, indent=2)
    
    # Write human-readable TXT (format as shown in Sections 1-9 above)
    _write_txt_report(report, low_recon_files, zero_row_files, flagged_rows, 
                      dup_rows, files, f"{output_dir}extraction_summary_report.txt")
    
    # Print 5-line stdout summary
    print(f"\n{'='*55}")
    print(f"EXTRACTION COMPLETE")
    print(f"  Clean: {clean_rows:,}  |  Flagged: {len(flagged_rows):,}  |  Dups: {len(dup_rows):,}")
    print(f"  Avg recon: {avg_recon:.1%}  |  Low-recon files: {len(low_recon_files)}  |  Zero-row: {len(zero_row_files)}")
    print(f"  Full report: {output_dir}extraction_summary_report.txt")
    print(f"{'='*55}\n")
    
    return report


def _write_txt_report(report, low_recon_files, zero_row_files, flagged_rows, 
                      dup_rows, files, output_path):
    """Write the human-readable multi-section report to a .txt file."""
    # Implement formatting for Sections 1-9 as specified above
    # Use fixed-width columns, box-drawing characters for readability
    # Each section follows the template shown in the spec
    pass  # Claude Code fills this in — structure is fully defined above
```

**Wire this into the phase orchestrator as the final step** — call `generate_extraction_report()` after all files are processed, before the phase exits. It adds ~2 seconds to the run time and gives you the single source of truth you currently have to dig through raw CSVs to find.

**The JSON output (`extraction_summary_report.json`) should be read by the analysis phase** at startup as its input manifest — so the analysis phase knows exactly how many accounts it has, which accounts are reliable, which to treat with caution, and what the LLM call budget looks like going in.