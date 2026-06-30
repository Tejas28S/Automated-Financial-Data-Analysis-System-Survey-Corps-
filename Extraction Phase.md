# Extraction Phase — Technical Documentation

**Project:** Automated Financial Data Analysis System
**Team:** Survey Corps · CIDECODE Hackathon 2026 · CID Karnataka
**Scope of this document:** the complete Extraction Phase — architecture, workflow, every parser, every metadata field, validation, standardisation, outputs, and known limits.

> Audience: a developer joining the project. After reading this single document you should understand what the extraction engine does, how it is built, why the key design decisions were made, and what it hands to the Analysis Phase.

---

## 1. Overview

### 1.1 Objective
Turn a pile of **heterogeneous bank statements** — from many different banks, in many different file formats, with no common layout — into **one clean, verified, standardised transaction table** plus a structured record of each account's identity, ready for the fraud-analysis engine.

### 1.2 Problem statement
Bank statements in the real world are maximally inconsistent:

- **Format diversity:** digital PDFs, scanned/photographed PDFs, phone photos (JPG/PNG), Excel (`.xlsx`/`.xls`), CSV, plain-text ledgers (`.txt`), Word (`.docx`).
- **Layout diversity:** every bank labels its columns differently (`Withdrawal`/`Debit`/`Dr`, `CHQNO`/`Ref No`/`UTR`), orders rows oldest-first or newest-first, wraps narration across lines, and prints amounts with `Dr`/`Cr` suffixes, forex rate columns, or running-balance signs.
- **Quality diversity:** OCR noise, multi-line cells, header/footer bleed, blank cells, counterparty data embedded in narration.

A naïve "one regex / one parser" approach cannot survive this. The extraction engine must be **format-agnostic and bank-agnostic**, must **never invent data**, and must **prove** its output is correct rather than assume it.

### 1.3 Goals
1. **Generalise, never overfit.** No logic is keyed to a specific filename or bank name. Layout is learned from the document's own structure. *(See `memory: transaction-extraction-must-generalize`.)*
2. **Cheapest-correct extraction.** Use a free deterministic parser whenever it provably works; escalate to an LLM only when a referee says the cheap parse failed.
3. **Privacy first.** The real account number, holder name and IFSC are never sent to an external text LLM — they are swapped for placeholders around every text-based LLM call.
4. **Auditability.** Every output number traces back to a file, a parser tier, and a reconciliation rate. Nothing is silently dropped — unparseable rows are *flagged*, not deleted.
5. **A single, stable output schema** the Analysis Phase can depend on.

### 1.4 Expected outputs
Per run, written under `outputs/extractions/<session_id>/`:

| Output | File | Purpose |
|---|---|---|
| Clean transactions | `clean_transactions.csv` | Verified rows, unified schema, across all statements |
| Flagged transactions | `flagged_transactions.csv` | Rows that failed a check, each with a reason + diagnosis |
| Duplicates | `duplicates.csv` | Exact-duplicate rows pulled out of the clean set (full audit trail) |
| Run metadata | `metadata.json` | The run's "receipt": per-file identity + counts |
| Per-statement bundles | `statements/<holder>_<account>.json` | One structured JSON per statement (identity + its clean rows) |
| Summary report | `extraction_summary_report.json` / `.txt` | Totals, flag breakdown, reconciliation, metadata coverage, LLM usage |
| Per-file ledger | `extraction_ledger.json` | One auditable object per file (route, tier, fallback, recon, zero-row status, metadata + source) |
| Vector store *(optional)* | `storage/chromadb/` | Anonymised transactions ingested for semantic search |

---

## 2. Complete Extraction Pipeline

The orchestrator is [`extraction/extraction_pipeline.py`](extraction/extraction_pipeline.py) → `run_extraction_pipeline(files, session_id, …)`.

```
                        ┌─────────────────────────────────────────────────────┐
                        │                 INPUT: list of files                │
                        └─────────────────────────────────────────────────────┘
                                              │
                          PREFLIGHT: skip missing/empty files (FAILED, loudly)
                                              │
                 split by extension ──────────┴───────────────────────────────────
                /                                                                   \
   non-image files (PDF/Excel/CSV/TXT/DOCX)                         image files (JPG/PNG)
   per-file loop  ────────────────────────                         batched (cross-file) ──────────
        │                                                                  │
   route_file()  ───────────────────────────                       Stage 1: vision transcribe each image
        │                                                          Stage 2: group_images() into statements
   ┌────┴───────────────────────────────────────┐                 Stage 3: vision structured-extract per group
   │ excel_csv │ pdf_digital │ pdf_scanned │     │                        │
   │   docx    │    text     │   image     │     │                        │
   └────┬──────────┬──────────────┬────────┘                              │
        │          │              │                                       │
   read table   pull text     OCR → text                                  │
        │          │              │                                       │
        │   ┌──────┴──────────────┴───────┐                               │
        │   │  Metadata: regex first,     │                               │
        │   │  LLM fallback only if empty │                               │
        │   │  (identity swapped → vault) │                               │
        │   └──────────────┬──────────────┘                               │
        │                  │                                              │
        │   ┌──────────────┴───────────────────────────────────┐         │
        │   │  TIERED TRANSACTION EXTRACTION (text path)        │         │
        │   │   Tier 2  cheap deterministic parse               │         │
        │   │   Tier 3  grade_parse referee (reconcile)         │         │
        │   │   Tier 4  LLM schema discovery (30-line sample)   │         │
        │   │   Tier 5  LLM full read (last resort)             │         │
        │   │   + fallbacks: structured-table / fixed-width /   │         │
        │   │                coordinate-column repair           │         │
        │   └──────────────┬───────────────────────────────────┘         │
        │                  │                                              │
        └──────────┬───────┴──────────────────────────────────┬──────────┘
                   │                                           │
          standardise to unified schema             standardise to unified schema
                   │                                           │
                   └─────────────────────┬─────────────────────┘
                                         │
                   chronological normalise (flip newest-first → oldest-first)
                                         │
                     validate_and_clean()  →  clean_df  +  flagged_df
                                         │
                          mark_duplicates() across the run
                                         │
        ┌────────────────────────────────┴────────────────────────────────┐
        │  PERSIST: CSVs + metadata.json + per-statement JSON              │
        │  REPORT:  summary report (.json/.txt) + extraction_ledger.json   │
        │  INGEST:  anonymise → ChromaDB (optional)                        │
        └─────────────────────────────────────────────────────────────────┘
```

**Why this shape.** The expensive, non-deterministic LLM is gated behind a deterministic **referee** (`grade_parse`). A clean statement costs **zero** transaction-LLM calls; only genuinely hard layouts escalate. Tokens scale with *documents*, not *rows*.

---

## 3. Supported Input Formats

Routing is done by [`extraction/router.py`](extraction/router.py) → `route_file()`. `SUPPORTED_EXTENSIONS = [.pdf, .xlsx, .xls, .csv, .docx, .txt, .jpg, .jpeg, .png]`.

| Format | Route label | How it is processed |
|---|---|---|
| **Digital PDF** | `pdf_digital` | [`extractor_digital_pdf.py`](extraction/extractor_digital_pdf.py): table-aware text extraction with pdfplumber (cell-level table joins + metadata header). |
| **Scanned PDF** | `pdf_scanned` | [`extractor_ocr.py`](extraction/extractor_ocr.py): PyMuPDF rasterises each page @300 DPI → Tesseract OCR → Groq Vision fallback on low-confidence pages → same text path. |
| **Image (JPG/PNG)** | `image` | [`vision_extractor.py`](extraction/vision_extractor.py) + [`image_grouping.py`](extraction/image_grouping.py): vision transcription → group continuation pages → structured vision extraction. |
| **Excel (.xlsx/.xls)** | `excel_csv` | [`extractor_excel_csv.py`](extraction/extractor_excel_csv.py): read with pandas (openpyxl/xlrd), deterministic column map, LLM column-id only if headers are unrecognised. |
| **CSV** | `excel_csv` | Same path as Excel; encoding/delimiter sniffing. |
| **TXT** | `text` | [`extractor_docx.py`](extraction/extractor_docx.py)`extract_text_from_txt` → text path; fixed-width ledgers handled by a dedicated fallback. |
| **DOCX** | `docx` | [`extractor_docx.py`](extraction/extractor_docx.py)`extract_text_from_docx` → text path. |

**Digital vs scanned PDF decision.** `_classify_pdf()` measures **average embedded characters per page**. Above `DIGITAL_PDF_CHAR_THRESHOLD = 100` → `pdf_digital`; at/below → `pdf_scanned` (the page is essentially a picture and needs OCR). On any inspection failure it defaults to `pdf_scanned` (safe — OCR always produces *something*).

---

## 4. Routing Strategy

```
route_file(path)
  ├─ extension in {.xlsx,.xls,.csv}      → "excel_csv"
  ├─ extension == .docx                  → "docx"
  ├─ extension == .txt                   → "text"
  ├─ extension in {.jpg,.jpeg,.png}      → "image"
  ├─ extension == .pdf                   → _classify_pdf():
  │      avg chars/page > 100 ? "pdf_digital" : "pdf_scanned"
  └─ otherwise                           → "unsupported" (recorded as FAILED)
```

- Routing is **purely structural** (extension + embedded-text density) — never the filename's words.
- **Images are partitioned out** of the per-file loop *before* routing, because they need a cross-file grouping step (a single statement can arrive as several photos). Every non-image file is processed independently.
- The router is the single source of truth for the `route` label that the rest of the pipeline branches on.

---

## 5. Transaction Extraction

All text sources (digital PDF, OCR'd scanned PDF, DOCX, TXT) flow through **one unified tiered ladder**, `_extract_text_transactions()`. Excel/CSV and images have their own structured extractors but standardise to the *same* schema.

### 5.1 The validation-arbitrated tiered hybrid

| Tier | Method | LLM cost | When used |
|---|---|---|---|
| **2** | Cheap deterministic parse (`standardise_digital_pdf_transactions`) | 0 | Always tried first |
| **3** | `grade_parse` referee (balance reconciliation) | 0 | Always — decides whether to escalate |
| **4** | LLM **schema discovery** from a 30-line sample → deterministic re-parse | 1 call | Only if Tier 3 verdict ≠ PASS |
| **5** | LLM reads the **full** statement (last resort) | 1 call | Only if still not PASS |

At every step the parse with the **best coverage-aware score** is kept, so escalating can only improve, never worsen, the result.

**Coverage-aware score (phase-4):**
```
_parse_score(grade) = reconciliation_rate × completeness_ratio
```
This prevents a degenerate parse (e.g. a wrong LLM schema whose date format matches only one line) from "winning" by reconciling trivially while discarding hundreds of real rows. A candidate must be better on **both** reconciliation **and** coverage.

### 5.2 Deterministic fallbacks (text path)
Layered after the tier ladder, each fully generic and adopted only if it scores better:

1. **Structured-table fallback** (`extract_transaction_table_df`) — re-reads the PDF as a pdfplumber **table** and maps columns by the table's own header. Fires when the text parse is **incomplete** (`completeness_ratio < MIN_COMPLETENESS_RATIO`), not only on a true zero. Recovers ruled-table PDFs and PDFs whose narration wraps onto the line *above* the date.
2. **Fixed-width text fallback** (`standardise_fixed_width_text`) — for plain-text ledgers whose lines end in trailing user-id/channel columns that block the money-peeler. Strips trailing alpha tokens, peels amount+balance, derives direction from the balance delta.
3. **Coordinate-column repair** (`extract_coordinate_table_df`) — re-reads the page by **word x-coordinates** when >80% of rows show the `amount == 1.0` artifact (a FLEXCUBE-style forex layout where a `Trans.Rate ≈ 1.00` column was misread as the amount). Ignores rate/LCY columns; folds balance-only continuation lines into the preceding row.

### 5.3 Specific extraction concerns

- **Table detection.** pdfplumber `extract_tables()`; the widest table (`max_cols × rows`) meeting a min size is treated as the ledger. A clean date column + ≥2 money columns is required so a *metadata* table (e.g. "Date of Birth … Balance") is never mistaken for the ledger.
- **Column detection.** Three layers: (a) deterministic header vocabulary (`Withdrawal/Debit/Dr`, `Deposit/Credit/Cr`, `Balance`, `Date`, `Narration/Particulars`, etc.); (b) coordinate clustering by x-position; (c) LLM column-id (`identify_column_structure`) only when headers are unrecognisable.
- **Transaction boundary detection.** A new transaction begins at a line whose **leading token is a date** (`_DATE_START_RE`). Lines without a leading date are treated as continuation/narration, never as new rows.
- **Multi-line narration.** Table cells join multi-line narration correctly; for text, an **inline narration supplement map** recovers fragments pdfplumber emits out of y-order. Phase-4 money-token recovery rescues amounts polluted by wrapped fragments (e.g. a cell `"N\n9173.00"` → `9173.00`).
- **Multi-page statements.** Page 1 contributes its **metadata header + table**; pages 2+ contribute **table rows only** (a raw `extract_text()` prefix on later pages would duplicate transactions in y-scrambled order — the "first 7–8 rows of each new page are wrong" symptom).
- **Header removal / bleed.** The metadata header is taken from page 1 only and is composed of non-transaction lines; it is kept for identity but excluded from the transaction rows.
- **Footer removal / bleed.** Page footers/summaries are non-date lines and are not parsed as transactions; bloated summary rows are caught by the narration-bloat check (§7).
- **CR/DR & Debit/Credit handling.** Separate `Debit`/`Credit` columns are preserved as-is. A single signed `Amount` (+ optional `Dr/Cr` flag) is split into Debit/Credit. **Direction is verified against the running balance** (`_correct_direction_by_balance`), because a `Dr/Cr` suffix on a *balance* marks the balance sign, **not** the transaction direction — a subtle but important distinction. The validator independently tries both directions to detect newest-first statements.
- **Balance extraction.** The running `Balance` column is the engine's truth signal; balance-only continuation lines are folded into their transaction.
- **Date extraction.** `Date` holds the calendar date only (no fabricated `00:00:00`); `Time` is a separate column, populated only when the statement prints a time, else blank.
- **Reference & transaction IDs.** Four **distinct** identifier fields are preserved (additively, never merged): `Transaction_ID` (bank's own per-row id), `Reference_Number` (dedicated Ref/RRN/UTR column), `Transaction_Reference` (an id parsed *out of* the narration, e.g. `UPI/506.../NAME`), `Cheque_Number` (cheque/instrument number). Any subset may be present; absent ones stay blank.

---

## 6. Metadata Extraction

Account identity is read **only from the statement's own content** — never from the filename or any reference file. Implemented in [`account_extractor.py`](extraction/account_extractor.py) (`extract_account_details_from_text` + `reconcile_account_details`) and, for spreadsheets, [`extractor_excel_csv.py`](extraction/extractor_excel_csv.py).

### 6.1 Fields supported

| Field | Notes |
|---|---|
| `account_holder` | Labelled lines + unlabelled first-line/post-block name heuristics; rejects branch/address/bank lines |
| `account_number` | Labelled (`A/C No`, `Account Number`), shape-validated (6–20 digits); Excel: repeated `ACCOUNT NO.`/`ACCOUNT` column |
| `bank_name` | Labelled, or **derived from the IFSC prefix** (`IFSC_PREFIX_TO_BANK`) when absent |
| `branch`, `branch_address`, `branch_code`, `branch_phone`, `branch_email`, `branch_gstin` | Labelled, multi-line aware |
| `ifsc_code` | Multi-window labelled match (`IFSC`, `IFS Code`, `RTGS/NEFT IFSC`) preferred over bare shape `[A-Z]{4}0[A-Z0-9]{6}` |
| `customer_id` / `cif` | `CIF No`, `Customer ID`, `Cust ID` |
| `micr_code` | `MICR` labelled |
| `account_type` / `product_type` | `Scheme`, `Product`, `Account Type` |
| `statement_period` | "From … To …" range |
| `opening_balance` / `closing_balance` | Labelled balances |
| `currency`, `customer_address`, `joint_holder`, `nominee_name` | Additive identity context |

### 6.2 Strategy & validation
- **Code-first, LLM-fallback.** Local regex runs first (fully private). The text LLM (`extract_metadata_llm`, defined in `llm_structurer.py`) is consulted **only** when regex finds *no* identity at all — and even then, shape-validated regex values are not discarded for an LLM omission.
- **Shape validation.** IFSC must match `[A-Z]{4}0[A-Z0-9]{6}`; account number must be a 6–20 digit run; holder candidates rejected if numeric/date/branch-like. This prevents a **counterparty** IFSC or name (which appears inside NEFT/IMPS narration) from being mistaken for the *account's own* identity.
- **Excel multi-source** *(memory: `excel-metadata-multisource`)*: a `key:value` block above the table, **identity carried as repeated columns** (`_identity_from_columns` — the value repeated on every row is the account's; a many-valued column is a counterparty column and is rejected), and the shared text-heuristics reader over sheet titles. Header normalisation strips trailing punctuation so `ACCOUNT NO.` and `IFSC:` still match.
- **Reconciliation.** `reconcile_account_details` merges sources by reliability, fills `bank_name` from the IFSC prefix, and records a `metadata_source` (`regex` / `llm_fallback` / `excel_metadata_block` / `vision`) for the ledger.

---

## 7. Validation Pipeline

Implemented in [`validator.py`](extraction/validator.py).

### 7.1 The referee — `grade_parse`
Grades a candidate DataFrame **without modifying it** and returns:
`reconciliation_rate`, `completeness_ratio`, `ordering` (`oldest_first`/`newest_first`/`unknown`), `has_balance_column`, `failing_row_indices`, `rows_checked`, `verdict`.

- **Reconciliation** checks `previous_balance ± amount = current_balance` within `BALANCE_TOLERANCE = 1.0` rupee.
- Tries **both directions** and keeps the better — this is how a newest-first statement (chain runs bottom-to-top) is detected.
- **Completeness** = `parsed_rows / transaction_like_lines` — catches under-extraction even when the few parsed rows reconcile.
- **Verdict = PASS** only if `reconciliation_rate ≥ ACCEPT_RECONCILE_RATE (0.98)` **and** `completeness_ratio ≥ MIN_COMPLETENESS_RATIO (0.90)`. When there is no usable running balance, a weaker proxy (debit/credit exclusivity + valid dates) is used instead.

### 7.2 Row-level checks — `validate_and_clean`
Each row is tested; the **first** failing check sets `flag_reason`. Clean rows go to `clean_df`, failing rows to `flagged_df`.

| Check | `flag_reason` | Meaning |
|---|---|---|
| Date validity | `invalid_date` | Date unparseable |
| Balance arithmetic | `balance_mismatch` | `prev ± amount ≠ balance` (beyond tolerance) |
| Debit/Credit exclusivity | `both_debit_credit_filled` | Both filled → column misalignment |
| Narration bloat | `narration_contains_multiple_transactions` | >400 chars and ≥4 embedded dates → multiple txns merged |

**Balance-mismatch diagnosis** (`_classify_balance_mismatch`) explains *why*, so a flag is actionable rather than opaque:
- `missing_amount` — balance moved but both debit and credit are zero.
- `direction_inverted` — swapping debit↔credit makes the row reconcile.
- `missing_transaction` — this row is correct but a gap implies an un-captured transaction before it.
- `balance_jump_no_change` — balance changed with no corresponding amount and no movement.

### 7.3 Duplicate detection — `mark_duplicates`
Assigns a stable `txn_id` to every row and tags exact duplicates with `duplicate_of` (first occurrence = `None`). Duplicates are pulled into `duplicates.csv` — **nothing is lost**, the full audit trail is preserved.

### 7.4 Zero-row adjudicator
When a file yields zero rows, `_adjudicate_zero_row` distinguishes a **true zero** (positive evidence of no activity — "No transactions", withdrawal/deposit counts = 0) from a **functional failure** (we failed to parse). The ledger records `zero_row_status` + `zero_row_reason`, so a genuinely empty statement is never confused with a broken parse.

### 7.5 Metadata validation
Shape-checks (IFSC pattern, account-number digit run), label-preference over bare shape, counterparty rejection, and `metadata_source` provenance per field (§6.2).

---

## 8. Standardisation

[`standardiser.py`](extraction/standardiser.py) converts every source into the **one schema** the Analysis Phase depends on.

```
STANDARD_COLUMNS    = [Date, Time, Narration, Debit, Credit, Balance, Account_ID, Bank_Name]
REFERENCE_COLUMNS   = [Transaction_ID, Reference_Number, Transaction_Reference, Cheque_Number]   (additive)
RICH_COLUMNS        = STANDARD + REFERENCE + [Transaction_Type]                                  (full internal row)
```

What standardisation guarantees:
- **Amounts → floats.** `_clean_amount` / `_clean_amount_to_float` handle Indian commas (`1,50,000.00`), `₹`, `Dr/Cr` suffixes, dashes/nil → blank. Phase-4 `_recover_money_token` rescues a numeric value from a fragment-polluted cell (`"N\n9173.00"` → `9173.00`) **only** after a plain parse fails — it can never corrupt a clean cell.
- **One amount model.** A single signed/`Dr-Cr`-flagged amount column is split into `Debit`/`Credit`; direction is then **verified against the running balance**.
- **Dates normalised**, `Time` separated, blanks preserved (a blank cell stays blank — never a fabricated 0 or midnight).
- **Account_ID & Bank_Name stamped** on every row from the reconciled identity.
- **Chronological order:** newest-first statements are flipped to oldest-first (preserving within-day order) so reconciliation and analysis always see time moving forward.

The same `standardise_dataframe_direct` is reused by the Excel path *and* the PDF structured-table fallback — one column-mapper, many sources.

---

## 9. Database & Outputs

Persistence is in [`storage.py`](extraction/storage.py) (`persist_extraction_run`) and reporting in [`report_generator.py`](extraction/report_generator.py). All under `outputs/extractions/<session_id>/`.

| Output | Contents / purpose |
|---|---|
| **`clean_transactions.csv`** | All verified rows across every statement, in the unified schema. The primary input to the Analysis Phase. |
| **`flagged_transactions.csv`** | Rows that failed a check, each with `flag_reason` and (for balance mismatches) `mismatch_diagnosis`. Held for manual review — surfaced, not deleted. |
| **`duplicates.csv`** | Exact duplicates pulled out of the clean set, with `duplicate_of` pointing at the kept row. |
| **`metadata.json`** | Run receipt: per-file identity + row counts. |
| **`statements/<holder>_<account>.json`** | One bundle per statement — reconciled account details + that statement's clean rows. Lets the analysis/UI consume a single account at a time. |
| **`extraction_summary_report.json` / `.txt`** | Totals, flag breakdown, balance-mismatch diagnosis histogram, reconciliation average + sub-0.90 list, zero-row files, metadata coverage (holder/account/IFSC), LLM-call budget. |
| **`extraction_ledger.json`** | One object **per file**: route, parser tier, fallback used, raw-char count, transaction-like lines, row counts, reconciliation rate, zero-row status/reason, per-field metadata + source, LLM calls. The audit backbone — any number traces to a file without re-running. |
| **ChromaDB** (`storage/chromadb/`) | *Optional* (`ingest_to_chromadb=True`). Each row is rendered as a descriptive sentence, embedded with `all-MiniLM-L6-v2`, and stored in a **local, on-disk** vector collection (one per session). It is **local-only — no data leaves the machine** (no cloud egress); embeddings carry real values for accurate search. |

> **Note on "SQLite".** The Extraction Phase currently persists tabular data as **CSV + JSON** and the semantic store as **ChromaDB** (a vector database). There is no SQLite database produced by extraction today; if a relational store is desired it would be a thin adapter over `clean_transactions.csv` and is best owned by the Analysis Phase. This is documented honestly rather than implied.

---

## 10. Major Improvements

### 10.1 Architectural
- **Validation-arbitrated tiered hybrid** — a deterministic referee (`grade_parse`) gates all LLM use; cheap-correct by default, LLM only on proven failure.
- **Unified text path** for digital PDF / scanned-PDF OCR / DOCX / TXT — one ladder, one standardiser.
- **Per-file ledger + zero-row adjudicator** — full auditability; true-zero vs functional-failure separation.
- **Privacy vault** ([`identifier_vault.py`](extraction/identifier_vault.py)) — account number / holder / IFSC swapped for `ACC_TEMP` / `HOLDER_TEMP` / `IFSC_TEMP` around every text LLM call; restored after.
- **GROQ multi-key rotation** ([`key_pool.py`](extraction/key_pool.py)) — survives per-key TPM limits on large batches.

### 10.2 Parser
- **Coverage-aware `_parse_score`** (`reconciliation × completeness`) — a degenerate 1-row parse can no longer beat a fuller one. *Recovered ~5,761 rows that were previously collapsing to a single row.*
- **Completeness-triggered structured-table fallback** — fires on *incomplete* parses, not only true zeros; fixes digital PDFs whose narration wraps above the date.
- **Money-token recovery** — rescues amounts from fragment-polluted cells.
- **Coordinate-column repair** — fixes the forex `amount == 1.0` misread.
- **Fixed-width text parser** — plain-text ledgers with trailing channel columns.

### 10.3 Metadata
- **Excel account-number from a repeated identity column** (trailing-period header normalisation + bare `ACCOUNT` header) — recovered all previously-missing spreadsheet account numbers.
- **`_metadata_lines` keeps labelled identity emitted in y-order *after* the first transaction** — fixed a correctness bug where a dropped header let the LLM fallback hallucinate a counterparty as the account holder/number.
- **Multi-window IFSC** and **bank-from-IFSC-prefix** derivation.

### 10.4 Validation
- **Balance-mismatch diagnosis** (`missing_amount` / `direction_inverted` / `missing_transaction` / `balance_jump_no_change`).
- **Narration-bloat detection** for merged-transaction rows.
- **Both-direction reconciliation** for newest-first detection.

### 10.5 Performance / safety
- **OCR page cap** (`max_pages`) and 300-DPI rasterisation — bounded, accurate OCR.
- **LLM sees only samples** (30 lines for schema discovery), not whole statements, except the Tier-5 last resort.
- **Offline regression harness** (`tools/regression_harness.py`) proves zero regressions before/after any change *(memory: `regression-harness`)*.

---

## 11. Challenges Solved

| Challenge | Solution |
|---|---|
| **Header bleed** | Page-1-only metadata header, composed of non-transaction lines; excluded from rows. |
| **Footer bleed** | Footers/summaries are non-date lines (never parsed as txns); bloated summaries caught by the narration-bloat check. |
| **Multi-line narration** | Table-cell joins + inline narration supplement map + money-token recovery for wrapped fragments. |
| **Narration wrapping *above* the date** | Completeness-triggered structured-table fallback (re-reads as a table); this single class had collapsed several large PDFs to 1 row. |
| **Metadata hallucination** | Code-first metadata; shape validation + counterparty rejection; restored dropped post-date identity lines; vault placeholders. |
| **Balance mismatch** | Tolerance-aware reconciliation, both-direction check, per-row diagnosis; honest flagging instead of suppression. |
| **Excel/CSV parsing** | Deterministic header map + identity-from-repeated-columns + key:value block; LLM column-id only when unrecognised; xlrd for `.xls`. |
| **Duplicate handling** | Stable `txn_id` + `duplicate_of`; duplicates moved to `duplicates.csv`, never dropped. |
| **Transaction boundary detection** | Leading-date rule; continuation lines folded, not split into phantom rows. |
| **Forex `amount == 1.0` misread** | Coordinate-column repair ignoring rate/LCY columns. |
| **Newest-first statements** | Detected via reverse-order reconciliation; flipped to oldest-first in standardisation. |
| **Generalisation across banks** | No filename/bank-name branching anywhere; layout learned from structure. *(memory: `transaction-extraction-must-generalize`.)* |
| **Large-batch API limits** | Multi-key rotation; deterministic fallbacks absorb LLM 413/TPM failures. |

---

## 12. Current Capabilities

The extraction engine can:
- Ingest **PDF, scanned PDF, JPG/PNG, XLSX, XLS, CSV, TXT, DOCX** and route each correctly.
- Extract transactions deterministically, escalating to an LLM **only** when a balance-reconciliation referee proves it necessary.
- Recover transactions from **ruled tables, wrapped-narration layouts, fixed-width ledgers, forex layouts, multi-page statements, and photographed/scanned pages**.
- Detect and correct **debit/credit direction** against the running balance and **oldest/newest** ordering.
- Preserve **four distinct reference identifiers** plus cheque numbers, additively.
- Extract **~20+ metadata fields** from the document's own content, with shape validation, counterparty rejection, and IFSC-prefix bank derivation.
- Validate every row (date, balance arithmetic, debit/credit exclusivity, narration bloat) with **actionable diagnoses**.
- Distinguish **true-zero** statements from **functional failures**.
- De-duplicate across the whole run with a full audit trail.
- Protect identity via **placeholder swapping** around every text LLM call.
- Emit a **stable unified schema** plus a **per-file ledger** and **summary report** for full auditability.
- Optionally ingest **anonymised** transactions into a **vector store** for semantic search.

**Measured (162-file corpus):** 162/162 files processed, 0 failures, **206,458** rows (ground truth ≈ 205,455), account number on **159/162**, holder on **156/162**, **0** functional zero-rows, average reconciliation **0.984**.

---

## 13. Known Limitations

- **`balance_mismatch` is not zero, by design.** ~490 rows across the full corpus remain flagged, **93% concentrated in two pathological files** (one with heavily wrapped multi-line narration where amounts bleed across rows; one forex statement where a rate column leaks into Debit). Driving this to zero would require per-file suppression, which the project rules forbid — honest flags beat hidden rows. *(memory: `section4-gates-honest-limits`.)*
- **IFSC coverage caps at document-faithful values.** Some statements (notably pure Excel/CSV transaction tables) **do not contain a labelled IFSC at all** — only counterparty IFSCs inside narration. The engine will not fabricate one, so its IFSC count is lower than a ground truth that derived IFSCs externally.
- **Lineless-table PDFs** (no ruled borders) where pdfplumber finds no table and the text parser under-reads can lose a few rows (a small HDFC-style statement is the example).
- **Vision/OCR truncation.** Very long scanned/photographed statements can have the vision model return fewer rows than present; bounded by `max_pages` and not deterministically recoverable.
- **No SQLite/relational output** in the extraction phase (CSV/JSON/ChromaDB only) — see §9.

**Possible future improvements:** multi-line row reconstruction for the wrapped-narration class; a generic "metadata block detector" independent of y-order; OCR row-count cross-checks against a running-balance chain; an optional SQLite adapter for the Analysis Phase.

---

## 14. Preparing for the Analysis Phase

The Analysis Phase (25 fraud-detection cases) consumes the extraction outputs through their **stable contract**:

- **Primary feed: `clean_transactions.csv`** — guaranteed `STANDARD_COLUMNS` (`Date, Time, Narration, Debit, Credit, Balance, Account_ID, Bank_Name`) plus the additive `REFERENCE_COLUMNS`. *These exact names must not change without updating the analysis engine.*
- **Per-account bundles: `statements/<holder>_<account>.json`** — identity + clean rows for single-account analysis and UI display.
- **Identity & provenance: `metadata.json` + `extraction_ledger.json`** — let analysis trust (or down-weight) a statement based on its reconciliation rate, parser tier, and metadata source.
- **Review queue: `flagged_transactions.csv`** — rows analysis should treat cautiously, each with a reason + diagnosis; never silently merged into the clean feed.
- **Semantic layer (optional): ChromaDB** — local on-disk embeddings for similarity/clustering across narrations (no cloud egress).

Guarantees the Analysis Phase can rely on:
1. Every clean row reconciles against its running balance (within tolerance) or is otherwise validated.
2. Amounts are floats; debit/credit direction is balance-verified; rows are chronological (oldest-first).
3. Account number, holder and bank are stamped on every row.
4. Nothing is silently dropped — flagged and duplicate rows are preserved separately with full reasons.

---

*Source of truth: this document is a description of the code in [`extraction/`](extraction/). Where a number is quoted it comes from the verified 162-file run; where a limit is stated it is a deliberate, documented decision rather than an oversight.*
