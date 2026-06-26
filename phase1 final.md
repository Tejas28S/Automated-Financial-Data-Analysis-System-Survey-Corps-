# Phase 1 — Extraction Engine: FINAL Work Log & Architecture Reference — updated by vinayak and tejas

> **STATUS: PHASE 1 (EXTRACTION) — COMPLETE.** ✅
> The extraction engine is finished and validated against the full `original bank statements`
> dataset (103 files: digital PDFs, Excel `.xls`/`.xlsx`, CSV, TXT). The core engine parses the
> overwhelming majority of statements deterministically (0 LLM calls); the validator-arbitrated
> LLM tiers exist only as a backstop for genuinely unusual layouts and never corrupt a good
> deterministic parse. The project is ready to move on to **Phase 2 (Analysis & Reporting)**.

**Project:** Automated Financial Data Analysis System (Survey Corps · CIDECODE Hackathon 2026 · CID Karnataka)
**Scope:** Everything done in the extraction phase — architecture, all code changes across the four working sessions, file explanations, known issues, and what comes next.
**Provider note:** The system uses **Groq** (the fast LLM inference API, `from groq import Groq`) running open models — not xAI's "Grok". Wherever "Groq" appears, it means the Groq inference API.

---

## Table of Contents

1. [Design Philosophy — The Validation-Arbitrated Tiered Hybrid](#philosophy)
2. [Complete File-by-File Inventory (theory, no code)](#inventory)
3. [API Keys and AI Usage Map](#api-map)
4. [Session 1 Changes — Generalization Fix](#session1)
5. [Session 2 Changes — Architecture Overhaul](#session2)
6. [Session 3 Changes — Regression Stability Fix + Full-Dataset Validation](#session3)
7. [Session 4 Changes — Parser Hardening + 103-File Final Validation](#session4)
8. [Current Architecture Status](#status)
9. [How to Move to the Analysis Phase](#next-phase)
10. [Known Drawbacks and Pending Issues](#drawbacks)

---

<a name="philosophy"></a>
## 1. Design Philosophy — The Validation-Arbitrated Tiered Hybrid

The central idea is simple: **let cheap deterministic code do the work for every file it can handle correctly, and let the LLM handle only the cases that deterministic code genuinely cannot**. The referee between the two is the balance-reconciliation validator — a format-agnostic arithmetic check that works for every bank on earth because all banks print a running balance column.

The tiers from cheapest to most expensive:

```
Tier 0  Router + raw extract          (always runs — no LLM)
Tier 1  Metadata from text header     (local regex first, LLM only if regex finds nothing)
Tier 2  Cheap deterministic parse     (no LLM — fixed schema assumptions)
Tier 3  VALIDATE (the referee)        → PASS → accept the cheap parse
                                      → FAIL ↓
Tier 4  LLM schema discovery on       → re-parse all rows with the discovered schema
        a small SAMPLE                → VALIDATE again → PASS → accept
                                      → FAIL ↓
Tier 5  LLM reads the full statement  → remaining failures → FLAGGED (never dropped)
        (absolute last resort)
```

Every file exits at the cheapest tier where the validator says PASS. A clean, normal digital PDF or Excel file costs 0 transaction LLM calls. Tokens scale with the number of genuinely hard documents, not with the number of rows.

**Three locked rules:**
1. No code path ever branches on a bank's name. Bank identity is only ever data (a label in output), never a condition.
2. Nothing is ever silently dropped. Unparseable rows go to `flagged_transactions.csv` with a `flag_reason`.
3. Raw bank data never leaves the machine unmasked. Every text LLM call is preceded by `anonymise_text()` which replaces account numbers, names, and IFSC codes with placeholders.

---

<a name="inventory"></a>
## 2. Complete File-by-File Inventory (theory)

### 2.1 Directory Structure

```
SURVEY CORPS/
├── config/
│   ├── __init__.py
│   └── settings.py
├── extraction/
│   ├── __init__.py
│   ├── router.py
│   ├── extractor_digital_pdf.py
│   ├── extractor_ocr.py
│   ├── extractor_docx.py
│   ├── extractor_excel_csv.py          ← rewritten in Session 2
│   ├── vision_extractor.py             ← extended in Session 2
│   ├── account_extractor.py
│   ├── anonymiser.py
│   ├── identifier_vault.py
│   ├── column_identifier.py
│   ├── llm_structurer.py               ← patched in Session 2
│   ├── llm_interface.py                ← NEW in Session 2
│   ├── standardiser.py                 ← extended in all sessions
│   ├── validator.py                    ← extended in Session 2
│   ├── storage.py
│   ├── chromadb_ingestor.py
│   └── extraction_pipeline.py          ← rewritten in Session 2
├── tests/
│   └── test_extraction.py
├── run_all.py                          (batch runner for all 103 statements)
├── app.py                              (local Streamlit tester, not committed)
├── requirements.txt
├── .env.example
├── .gitignore
├── AUDIT.md
├── INSTRUCTIONS (1).md
└── phase1 final.md                     (this document)
```

---

### 2.2 `config/settings.py` — Central Configuration

Single source of truth for all constants. Nothing in the system hardcodes a value that might need changing; it all lives here.

**What it contains:**
- **Directory paths:** `BASE_DIR`, `UPLOAD_DIR`, `OUTPUT_DIR`, `EXTRACTIONS_DIR`, `LLM_CACHE_DIR`, `CHROMADB_DIR`, etc. All paths are created on import so the system can always write to them.
- **API keys:** `GROQ1_KEY = os.getenv("GROQ1")` and `GROQ2_KEY = os.getenv("GROQ2")`. GROQ3 is deliberately NOT loaded here — it belongs to the analysis phase and having it absent in the extraction code makes the separation explicit. `require_extraction_keys()` fails fast with a readable error if either key is missing.
- **Model names:** `GROQ_MODEL = "llama-3.3-70b-versatile"` (text, 100k tokens per day on the free tier) and `GROQ_VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"` (vision).
- **Extraction thresholds:** `TESSERACT_CONFIDENCE_THRESHOLD = 80.0` (below this Groq Vision is the fallback), `DIGITAL_PDF_CHAR_THRESHOLD = 100` (fewer characters per page → classify as scanned), `BALANCE_TOLERANCE = 1.0` (1 rupee tolerance in running balance checks).
- **Tiered-hybrid thresholds (new in Session 2):** `ACCEPT_RECONCILE_RATE = 0.98` (a parse is trusted if 98% of balance-chain rows hold), `MIN_COMPLETENESS_RATIO = 0.90` (at least 90% of transaction-like lines must be captured), `SCHEMA_SAMPLE_LINES = 30` (how many lines are sent to the LLM for schema discovery), `REPAIR_BATCH_ROWS = 60` (batch size if row-by-row repair is ever added).
- **Output schema:** `STANDARD_COLUMNS = ["Date","Time","Narration","Debit","Credit","Balance","Account_ID","Bank_Name"]` and `REFERENCE_COLUMNS = ["IFSC_Code","account_holder","account_number","ifsc_code"]`. This is the fixed contract the analysis phase depends on.

---

### 2.3 `router.py` — File-Type Detection (Station 1)

Every uploaded file enters the system through the router. `route_file(path)` returns one label: `excel_csv`, `docx`, `image`, `pdf_digital`, or `pdf_scanned`. Extension determines the route for everything except PDFs. For PDFs, `_classify_pdf()` opens the file with pdfplumber, reads up to 3 pages, and measures average characters per page. Above `DIGITAL_PDF_CHAR_THRESHOLD` (100 chars) it is digital; below it is scanned. No Groq. No bank names.

---

### 2.4 `extractor_digital_pdf.py` — Text from Digital PDFs (Station 2)

`extract_text_from_digital_pdf(path)` uses pdfplumber to read every page into text and joins them into one string. Each page is wrapped in its own try/except so a single bad page cannot crash the file. `_clean_pdf_text()` strips `(cid:NN)` glyph artifacts — a common pdfplumber quirk where decorative font glyphs appear as `(cid:9)` instead of a real character. No Groq.

**Session 4 addition — collapsed-rows guard:** pdfplumber's table extractor sometimes stores all transaction rows as a single table cell with embedded `\n` separators, producing just 1 "line" that the parser sees as 1 row. The guard compares date-line counts from table extraction vs `extract_text()`. If the plain text has ≥1.5× more date lines AND ≥3 absolute date lines, the page falls back to `extract_text()`. This fixed ISHA STAT NW (6 rows recovered from the collapsed 1-cell output).

---

### 2.5 `extractor_ocr.py` — Scanned PDF and Image OCR (Station 2)

The two-tier local OCR engine. For images, it runs Tesseract (after OpenCV preprocessing: grayscale, Otsu thresholding, deskew, denoise, sharpen). If Tesseract's confidence is below 80%, it falls back to Groq Vision (`run_groq_vision_on_image` — uses GROQ2). For scanned PDFs, it rasterises each page at 300 DPI using PyMuPDF, then runs the two-tier OCR on each page image.

**429 TPD fail-fast (added Session 2):** `_is_nonretryable(err)` detects two categories of non-retryable errors — 413 (payload too large) and 429 with "per day"/"tokens per day"/"tpd" in the message. When daily quota is exhausted, retrying wastes the remaining tokens and hangs for up to 10 minutes. The fail-fast breaks the retry loop immediately.

---

### 2.6 `extractor_docx.py` — Word Document Text (Station 2)

`extract_text_from_docx(path)` reads all paragraphs and all table cells via python-docx into one text string, which is then treated exactly like digital-PDF text. No Groq.

---

### 2.7 `extractor_excel_csv.py` — Structured Table Reading (Station 2, rewritten Session 2)

Excel and CSV files are already structured tables, but they have two challenges the original code did not handle:

**Challenge 1 — Metadata block above the table.** Some bank exports print account identity (Account Number, IFSC, Holder Name, Branch, Period) in key:value rows ABOVE the actual transaction table. The original code ignored these rows. The rewrite adds `_parse_metadata_block(rows)` which detects and extracts this block. The metadata becomes `df.attrs["statement_metadata"]`, a dict carried alongside the DataFrame without corrupting the data.

**Challenge 2 — Variable header row position.** Some files have the column header on row 1, others have it on row 9 after the metadata block. The original "first all-text row" rule picked the wrong row on files where metadata labels like "Account Type | Savings" appeared before the real header. The rewrite adds `_detect_header_index(rows)`: it measures the modal column width of actual data rows, then requires the header row to be at least as wide as the data table. A narrow key:value metadata row cannot be mistaken for a wide transaction header.

**Deterministic column mapping.** `_COL_KEYWORDS` is a priority-ordered list of column header spellings mapped to standard field names (`date`, `narration`, `debit`, `credit`, `balance`, `cheque_number`, `reference_number`). This is applied in `_infer_column_map(columns)`. The result is attached as `df.attrs["inferred_column_map"]`. When the pipeline finds this map is complete (has date, balance, and at least one of debit/credit), it skips the Groq column identifier entirely — 0 LLM calls for a normal Excel/CSV file.

`_read_csv_file` tries `utf-8 → latin-1 → cp1252` encodings (Indian bank systems often export in Windows encoding). `_read_excel_file` uses `dtype=str` throughout to preserve leading zeros in account numbers.

---

### 2.8 `vision_extractor.py` — Image and Scanned PDF Vision (Station 2/3, extended Session 2)

`extract_statement_from_image(path)` sends an image to Groq Vision (GROQ2) with a strict JSON prompt and gets back `{account_details, transactions}` directly. Results are cached on disk by a hash of the image bytes — re-running on the same image costs 0 tokens.

**`extract_structured_from_scanned_pdf(file_path, max_pages)` (added Session 2):** Renders each page of a scanned PDF to a PNG image using PyMuPDF at `OCR_RENDER_DPI` (300 DPI), saves each to a temp directory, and calls `extract_statement_from_image` per page. Account details are taken from the first page that provides them. Transactions are accumulated across all pages. The temp directory is cleaned up in a `finally` block. This eliminates the old path where Tesseract converted scanned pages to raw text that was then chunked and sent to GROQ1 — which consumed the entire 100k daily token quota on a 6-page statement. Now only GROQ2 is used, on a separate daily quota, and each page is cached.

---

### 2.9 `account_extractor.py` — Identity from Text (Station 3 helper, local)

`extract_account_details_from_text(text)` uses regex to find account holder, account number, IFSC code, branch, account type, statement period, and opening/closing balances from the raw text of a digital PDF or OCR result. It handles many label spellings and falls back to a bare "MR/MRS NAME" line for the holder. Bank name is inferred from the IFSC prefix using `IFSC_PREFIX_TO_BANK` (a reference dict — not a branching condition). No Groq.

`reconcile_account_details(content, account_id, bank_hint)` merges document-extracted values with the investigator's hints (account_id label and bank name passed at upload time). The pipeline never passes the `master_row` argument, so no reference dataset is ever loaded at runtime.

---

### 2.10 `anonymiser.py` — PII Masking Before Any Text → Groq Call

`anonymise_text(text)` replaces IFSC codes, UPI IDs, Indian mobile numbers, account numbers (long digit strings), and common Indian names with numbered placeholders (`ACCT_1`, `NAME_1`, etc.). The mapping is kept in memory and returned alongside the anonymised text; it is never logged or written to disk. Narration transaction keywords (UPI/NEFT/SALARY/RTGS/etc.) are preserved because the LLM needs them to understand transaction structure. Privacy rule: raw bank data never goes to any cloud API unmasked.

---

### 2.11 `identifier_vault.py` — Temporary Identity Swap

`IdentifierVault` provides `redact(text)` and `restore(result)` to swap real account identifiers with short fixed placeholders (`ACC_TEMP`, `HOLDER_TEMP`, `IFSC_TEMP`) around a single LLM call. It is used in the Excel/CSV column-ID path where the vault redacts before sending to Groq and restores placeholders in the returned column map. Works recursively over strings, dicts, and lists.

---

### 2.12 `column_identifier.py` — Groq Column Mapping for Excel/CSV (Station 3)

`identify_column_structure(text, path)` sends the first 40 anonymised lines to Groq (GROQ1) and receives back a JSON column map telling the pipeline which column header holds the date, narration, debit, credit, and balance. Results are cached on disk by an MD5 hash. Falls back to `DEFAULT_COLUMN_MAP` if Groq returns an unusable result (surfaced as source `fallback`, never hidden). After Session 2, this function is only reached if the deterministic `_infer_column_map` in `extractor_excel_csv.py` could not produce a complete map from the header names alone.

---

### 2.13 `llm_structurer.py` — The Groq Text Brain (Station 3, patched Session 2)

Holds all GROQ1 text calls, all via `_call_json()` (forced JSON mode, retry with backoff):

- **`extract_account_metadata(header_text, path)`** — sends the first ~40 lines of a statement to Groq and gets back account identity as JSON. Used as a fallback in `llm_interface.extract_metadata_llm` only when the local regex reader found nothing.
- **`discover_transaction_schema(sample_text, path)`** — sends a small anonymised sample of transaction lines and gets back a parsing schema describing the layout (date format, column order, debit/credit method, balance position, whether narrations wrap across lines). Used in Tier 4 of the escalation ladder.
- **`structure_statement(raw_text, path)`** — reads the full anonymised statement text in chunks and returns structured transaction rows as JSON. Used only in Tier 5 (last resort). Previously this was the primary path for scanned PDFs, consuming 40+ chunked Groq calls per file.

**429 TPD fail-fast (added Session 2):** `_call_json`'s retry loop now detects non-retryable 429 (daily quota) errors and breaks immediately instead of sleeping and retrying.

---

### 2.14 `llm_interface.py` — The Single LLM Facade (NEW in Session 2)

This is the most important architectural addition. The extraction pipeline imports ALL its LLM calls from here and nowhere else. Today it wraps the Groq-backed modules; tomorrow a local model swap touches only this file.

**Four functions:**
- `discover_schema(sample_text, file_path)` — anonymises the sample, calls `discover_transaction_schema`. Used in Tier 4.
- `read_statement_rows(raw_text, file_path)` — anonymises the full text, calls `structure_statement`. Used in Tier 5.
- `extract_metadata_llm(header_text, file_path)` — calls `extract_account_metadata`. Used as Tier 1 fallback when regex finds nothing (documented privacy exception: reading real holder name is the whole point here).
- `read_image(image_path)` — calls `extract_statement_from_image`. Used for image files (unavoidable raw-data exception — a vision model must see the pixels).
- `read_scanned_pdf(file_path, max_pages)` — calls `extract_structured_from_scanned_pdf`. Used for scanned PDF files.

---

### 2.15 `standardiser.py` — Reshape to Unified Schema (Station 4, extended all sessions)

Pure pandas/regex, no Groq. Converts whatever raw form the extractor produced into the standard `Date | Narration | Debit | Credit | Balance | Account_ID | Bank_Name` DataFrame.

**Key functions:**
- `standardise_digital_pdf_transactions(raw_text, account_id, bank, opening, schema)` — the date-anchored line parser. Reads `schema` (now actually consumed — in Session 1 it was "decorative") to adjust its assumptions about date format, column order, and debit/credit method.
- `standardise_llm_transactions(transactions, account_id, bank, opening)` — builds a DataFrame from the LLM's JSON list of transaction dicts, then corrects debit/credit direction from the running balance.
- `standardise_transaction_records(records, account_id, bank)` — for the vision reader's already-split structured rows.
- `standardise_dataframe_direct(raw_df, column_map, account_id, bank)` — for Excel/CSV using the column map from either the deterministic header-reader or Groq.
- `_correct_direction_by_balance()` — infers debit vs credit from the running balance direction when the narration does not explicitly say DR/CR. A genuine accuracy win kept across all routes.
- **`count_transaction_like_lines(raw_text)` (added Session 1)** — estimates how many lines contain a date AND a money amount. Used as the `expected_rows` baseline for the completeness check in `grade_parse`.
- **`build_schema_sample(raw_text, failing_lines)` (added Session 2)** — builds a smart representative sample (start/middle/end of document + longest lines + any lines that failed the parse) capped at `SCHEMA_SAMPLE_LINES`. Sent to the LLM for schema discovery in Tier 4.
- **`_peel_trailing_amounts()` (hardened Sessions 3 & 4)** — peels trailing monetary tokens from the right of a transaction line. Session 3 added trailing non-money channel-code stripping. Session 4 added interleaved watermark skip (Fix B) and leading-dot decimal normalisation (Fix C).
- **`_strip_noise_lines()` (hardened Session 4)** — filters boilerplate/footer lines. Fix D extended the URL filter (`https?://` in `_FOOTER_NOISE`) to fire even on lines that appear to start with a date, as long as they carry zero money tokens — catching browser-printed netbanking PDF timestamps.

---

### 2.16 `validator.py` — Quality Control and Escalation Referee (Station 5, extended Session 2)

**`validate_and_clean(df)`** — the final quality gate. Three checks: date validity (must be a real date, year 2000–2035), balance arithmetic (prev + credit − debit ≈ balance within 1 rupee tolerance), debit/credit exclusivity (both filled = column alignment error). Splits rows into clean and flagged. Adds `mark_duplicates` (tags, never deletes duplicates) and `_mark_reversals` (marks reversed/failed transactions with `is_reversed`).

**`grade_parse(df, expected_rows)` (NEW in Session 2)** — the heart of the tiered hybrid. Does NOT clean data; it grades a candidate parse so the pipeline can decide whether to escalate. Key mechanisms:
- Computes the reconciliation rate by walking the running-balance chain row by row.
- Tests BOTH forward and reverse order and takes whichever rate is higher. This is how a newest-first statement (chain runs bottom-to-top) is detected automatically — the `ordering` field in the result tells the pipeline to flip the DataFrame before final validation.
- Completeness ratio: `parsed_rows / expected_rows` (from `count_transaction_like_lines`).
- `PASS` verdict requires: `reconciliation_rate ≥ 0.98 AND completeness_ratio ≥ 0.90` (for statements with a balance column). For statements with no balance column (some Excel exports that omit it), falls back to a weaker proxy (debit/credit exclusivity + valid dates).
- Returns `failing_row_indices` — the list of rows that failed the chain. In a future Tier 5 row-repair extension, these are the only rows that need to go to the LLM.

---

### 2.17 `storage.py` — Persistence (writes only, no Groq)

`persist_extraction_run(session_id, clean_df, flagged_df, per_file_records, summary, statements)` writes one folder per session under `outputs/extractions/<session_id>/`:
- `clean_transactions.csv` — all clean rows with real `account_number`, `account_holder`, `ifsc_code` columns.
- `flagged_transactions.csv` — all flagged rows with a `flag_reason` column.
- `metadata.json` — the audit receipt: files processed, row counts, LLM call counts, reconciliation rate per file, tier each file resolved at, OCR engine used.
- `statements/<holder>_<account>.json` — one structured JSON per statement (identity + that statement's transactions).

Nothing in the system ever reads these files back at runtime. They are a pure output sink for the investigator and for the analysis phase.

---

### 2.18 `chromadb_ingestor.py` — Optional RAG Vector Store

`ingest_transactions_to_chromadb(df, session_id)` turns each clean transaction row into a sentence and upserts it into a ChromaDB collection using a local `all-MiniLM-L6-v2` embedding model. Imported lazily (only when `ingest_to_chromadb=True` is passed to the pipeline) so a normal extraction run never loads the heavy embedding stack. This is OFF by default in extraction and is the RAG chatbot's store for the analysis phase.

---

### 2.19 `extraction_pipeline.py` — The Orchestrator (rewritten Session 2)

`run_extraction_pipeline(files, session_id, ingest_to_chromadb, max_ocr_pages, persist)` is the single entry point for the whole extraction phase. It loops files sequentially (one bad file can fail without stopping the rest), calls `_process_single_file` for each, concatenates all per-file DataFrames, tags cross-file duplicates, optionally ingests to ChromaDB, and persists.

**`_process_single_file(file_path, account_id, bank_name, max_ocr_pages)`** routes each file and runs it through the appropriate tiers:

- **Image / Scanned PDF:** calls `read_image` or `read_scanned_pdf` via `llm_interface`, gets structured JSON directly from the vision model, standardises, runs `grade_parse` for a reconciliation report.
- **Excel / CSV:** calls `extract_dataframe_from_excel_csv`, reads metadata and column map from `df.attrs`, attempts deterministic column mapping first (0 LLM calls if successful), falls back to `identify_column_structure` only if the deterministic map is incomplete, standardises, grades.
- **Digital PDF / DOCX / Scanned-PDF-OCR-text:** feeds into `_extract_text_transactions` which implements the full tiered escalation ladder (Tier 2 → Tier 3 grade → Tier 4 schema discovery → Tier 5 full read).

**`_extract_text_transactions(raw_text, file_record, account_id, bank_name, details)`** is the unified text escalation ladder:
1. Tier 2: cheap default parse with `standardise_digital_pdf_transactions`.
2. `grade_parse` → if PASS, done.
3. Tier 4: `build_schema_sample` → `discover_schema` → reparse → `grade_parse` → if better, adopt.
4. Tier 5: `read_statement_rows` (full read) → `grade_parse` → if better, adopt.
5. Each tier records itself in `file_record` (tier name, reconciliation rate, LLM call count).

**Newest-first sort:** if `grade_parse` reports `ordering == "newest_first"`, the DataFrame is reversed before `validate_and_clean` so the analysis phase always sees time moving forward.

**Metadata code-first (Tier 1):** `extract_account_details_from_text` (local regex) is tried first. The LLM metadata fallback is called only if regex returns nothing useful. This keeps private identity data off the wire in 95%+ of cases.

---

### 2.20 `tests/test_extraction.py`

pytest suite covering router classification, standardiser types and schema, validator checks (balance arithmetic, dates, duplicates, reversals, exclusivity), anonymiser masking, and identifier vault. References `synthetic_dataset_full_mentoring/statements/` as the test dataset — this is the only correct place to depend on the dataset. The engine itself never reads the dataset. **43 tests, all passing as of Session 4.**

---

<a name="api-map"></a>
## 3. API Keys and AI Usage Map

Three Groq keys, two used in extraction, one reserved for analysis.

| Key | Model | Where used | What for |
|---|---|---|---|
| **GROQ1** | `llama-3.3-70b-versatile` (text) | `llm_structurer.py` via `llm_interface.py` | Schema discovery (Tier 4), full statement read (Tier 5), metadata fallback (Tier 1) |
| **GROQ1** | same | `column_identifier.py` | Excel/CSV column mapping when deterministic headers fail |
| **GROQ2** | `meta-llama/llama-4-scout-17b-16e-instruct` (vision) | `vision_extractor.py` via `llm_interface.py` | Image files and scanned PDF pages |
| **GROQ2** | same | `extractor_ocr.py` | Low-confidence Tesseract fallback (rarely needed) |
| **GROQ3** | (not loaded in extraction) | — | Reserved for analysis phase |
| **Tesseract** | local, no API | `extractor_ocr.py` | Primary OCR for scanned PDFs (before Groq Vision fallback) |

**Local model swap:** All pipeline LLM imports go through `llm_interface.py`. Swapping to a local model (Ollama with `llama3.2`/`mistral` for text; Ollama with `llava` for vision) requires editing `llm_interface.py` and the three underlying modules (`llm_structurer.py`, `column_identifier.py`, `vision_extractor.py`) — 4 files, not 1, because the Groq client is still created in 3 places.

---

<a name="session1"></a>
## 4. Session 1 Changes — Generalization Fix

### Problem
The digital-PDF transaction parser was hardcoded to specific layout assumptions: date must be at the start of the line, amounts must have exactly 2 decimal places, balance must be the last money token on the line. One statement (`356927262-Bank-Statement.pdf`) had 44 transaction lines that ended in a branch code (`101`), so the parser found 0 rows. This was the "fix one bank, break another" coupling problem.

### Fix — Format-Agnostic Generalization Guard
- Added `count_transaction_like_lines(raw_text)` to `standardiser.py` — counts lines that have a date anywhere AND at least one money token. This estimates how many transactions the statement actually contains without parsing them.
- Added a guard in `extraction_pipeline.py`: if `parsed_rows < 0.60 × expected_rows`, fall back to `structure_statement` (the LLM chunked reader). Only adopt the LLM result if it actually recovers more rows.

### Verification
The guard fired on 1 of 9 sample statements (the one with 0/44 rows). The 8 working statements (ratio 0.94–1.00) stayed on the cheap deterministic path with no regression and no added LLM cost.

---

<a name="session2"></a>
## 5. Session 2 Changes — Architecture Overhaul

Session 2 replaced most of the original architecture. The changes are listed in order of impact.

### 5.1 Scanned PDF → GROQ2 Vision (biggest impact)

**Before:** Scanned PDF → Tesseract → raw text → GROQ1 in 40+ chunks → structured rows. On a 6-page statement this consumed the entire 100k daily GROQ1 quota.

**After:** Scanned PDF → PyMuPDF renders pages → GROQ2 vision per page → structured JSON directly. Each page result is cached on disk. Re-running the same PDF costs 0 tokens. GROQ1 quota is not touched.

Files changed: `vision_extractor.py` (added `extract_structured_from_scanned_pdf`), `llm_interface.py` (added `read_scanned_pdf`), `extraction_pipeline.py` (scanned PDF branch now calls `read_scanned_pdf`).

### 5.2 429 TPD Fail-Fast

**Before:** A daily-quota 429 error triggered the normal retry loop (sleep + retry 3 times), wasting remaining tokens and hanging for 5–10 minutes.

**After:** `_is_nonretryable(err)` detects 429 with "per day"/"tokens per day"/"tpd" in the message and breaks the retry loop immediately. Same fix applied in both `extractor_ocr.py` and `llm_structurer.py`.

### 5.3 `grade_parse()` — The Validator Referee

Added `grade_parse(df, expected_rows)` to `validator.py`. This is the decision point that determines whether a parse is good enough or must escalate. It tests both forward and reverse order (to detect newest-first statements), reports a reconciliation rate, a completeness ratio, the detected ordering, and a `PASS/FAIL` verdict. The pipeline uses this verdict to decide which tier to stop at.

### 5.4 Tiered Escalation Ladder in `extraction_pipeline.py`

`_extract_text_transactions` implements the full Tier 2 → 3 → 4 → 5 ladder for all text-based sources (digital PDF, DOCX, scanned-PDF OCR text). Previously the pipeline had separate branches per file type with different logic in each. Now all text sources follow one path; the validator referee decides how far to escalate.

### 5.5 `llm_interface.py` — Single Provider Facade

New file. All LLM imports in the pipeline go through here. Today it wraps the existing Groq modules; a future local-model swap touches only this file (and the 3 modules it wraps, which still create Groq clients directly).

### 5.6 Excel/CSV Deterministic Column Mapping

**Before:** Every Excel/CSV file sent its first 40 lines to GROQ1 for column identification. This burned quota even for files with perfectly readable headers like "Date | Narration | Debit | Credit | Balance".

**After:** `_COL_KEYWORDS` in `extractor_excel_csv.py` maps standard header spellings to standard fields. `_infer_column_map(columns)` runs this deterministically. When the map is complete (has date, balance, and at least one of debit/credit), the pipeline skips `identify_column_structure` entirely — 0 LLM calls.

### 5.7 Metadata Block Parsing for Excel/CSV

**Before:** Key:value rows printed above the transaction table (Account Number, IFSC, Holder Name, Branch, Period) were ignored. The output showed no account identity for Excel files.

**After:** `_parse_metadata_block(rows)` reads both `Label | Value` cell-pairs and `# Label: Value` comment-style lines above the table. The extracted identity is attached as `df.attrs["statement_metadata"]` and flows through the pipeline into the output's `account_number`, `ifsc_code`, `account_holder` columns.

### 5.8 Width-Aware Header Detection

**Before:** `_detect_header_index` used "first all-text row" which picked a metadata label row (e.g. "Account Type | Savings") instead of the real transaction header on files where metadata appeared before the table.

**After:** The rewrite measures the modal column width of data rows, then requires the header row to be at least as wide as the data table. A 2-column metadata row cannot be mistaken for a 6-column transaction header.

### 5.9 Metadata Code-First in Text Path (Tier 1)

**Before:** Every digital PDF and scanned PDF called `extract_account_metadata` (GROQ1) to read account identity.

**After:** `extract_account_details_from_text` (local regex, no Groq) is tried first. The LLM is called only if the regex returns nothing for all three key fields (holder, number, IFSC). In practice the regex succeeds for most statements where the header is printed as text, so GROQ1 quota is not spent on metadata for most files.

### 5.10 `build_schema_sample()` in `standardiser.py`

Added `build_schema_sample(raw_text, failing_lines)` which builds a content-aware representative sample: first N lines, last N lines, middle N lines, the longest lines (likely the most complex rows), and any lines that failed the initial parse. Capped at `SCHEMA_SAMPLE_LINES = 30`. This is what gets sent to the LLM in Tier 4 schema discovery.

---

<a name="session3"></a>
## 6. Session 3 Changes — Regression Stability Fix + Full-Dataset Validation

Session 3 was triggered by a regression report: after Session 2's metadata/LLM changes, several
statements that used to extract correctly — most visibly **every Axis Bank statement** — began
returning **0 or 1 rows** while burning **2 LLM calls** per file. The work was strictly: find the
true root cause by tracing the pipeline, fix it generically (no per-bank patches, no overfitting),
and validate against the **whole** `original bank statements/` dataset.

### 6.1 Root-cause analysis (traced end to end, not guessed)

**Root cause A — the deterministic parser silently dropped whole statements (the real cause).**
`_parse_single_transaction_line` assumed *the balance is the last token on the line* and peeled
money tokens from the right. Axis (and several other) statements print a trailing **"Init. Br"
branch-code column** (`2535`, `248`, `100`) *after* the balance. A bare branch code is not a money
token, so the right-end peel found nothing → every row was flagged "amounts pending" → **all rows
discarded**. Instrumented proof: 80 of 80 Axis date-lines produced 0 kept rows. This layout only
ever "worked" before Session 2 because the old pipeline read every row with the LLM.

**Root cause B — a degenerate parse then blocked the LLM's correct output.**
When Tier 2 collapsed to 1 row, `grade_parse` had no real balance chain to test and reported
`reconciliation_rate = 1.0` — a meaningless perfect score. The escalation ladder compared parses by
**bare rate** (`llm_rate >= cheap_rate`), so a correct LLM parse reconciling at 0.99 *lost* to the
bogus 1.0 (`0.99 >= 1.0` is false) and was **discarded**, keeping the 1-row garbage.

### 6.2 The fixes

- **Fix 1 — trailing non-money column (`standardiser.py`).** New `_peel_trailing_amounts()` skips up to two short trailing code tokens — a bare-numeric branch/posting code or a standard transaction-channel code (UPI/NEFT/IMPS/RTGS/ATM/POS/CLG/…) — **only when doing so exposes a proper amount+balance pair**. A statement whose last token is already the balance never enters this path. → All Axis statements parse at **Tier 2 with 0 LLM calls**.

- **Fix 2 — score-based, non-regressing escalation (`validator.py` + `extraction_pipeline.py`).** `grade_parse` now also returns `score = reconciliation_rate × completeness_ratio`. The ladder compares candidate parses by **score** and adopts a challenger only if it is **strictly better**. A degenerate/sparse parse can no longer block a fuller parse.

- **Fix 3 — narration-LAST layouts (`standardiser.py`).** Mirror helper `_peel_leading_amounts()` handles statements that print `date → amounts → narration`. When the right-end peel finds no amounts, it peels the amount run from the front and treats the remainder as narration. → Narration-last PDFs now parse deterministically with **0 LLM calls**.

- **Fix 4 — bank name from the footer legend (`account_extractor.py`).** A Pass-3 scan reads the canonical bank name from the "REGISTERED OFFICE — … BANK LTD" boilerplate window only (never from a narration, so a counterparty bank inside a UPI/NEFT line is never mistaken for the account's own bank).

### 6.3 Full-dataset validation (61 files as of Session 3)

All 43 unit tests passing. 52 of 61 files parsed deterministically with 0 LLM calls. 5 files needed LLM fallback. No regressions on previously-working files.

---

<a name="session4"></a>
## 7. Session 4 Changes — Parser Hardening + 103-File Final Validation

Session 4 expanded the dataset to **103 files** (42 new statements added) and fixed 4 new edge cases discovered during full-dataset validation. All fixes are generalised on token shape and layout structure — **no bank name used anywhere in any conditional**.

### 7.1 Fix A — Collapsed-Rows Detection (`extractor_digital_pdf.py`)

**Problem:** pdfplumber's table extractor stored all 6 transaction rows of ISHA STAT NW as a single table cell containing embedded `\n`. The pipeline saw 1 "row" and reported rows=1, recon=1.0.

**Fix:** After building `table_lines` in `_extract_page_as_text`, count date-starting lines from both the table extraction and `extract_text()`. If `txt_date_count > tbl_date_count × 1.5` AND `txt_date_count >= 3`, fall back to `extract_text()`.

**Result:** ISHA STAT NW: 6 rows, recon=1.000. ✓

### 7.2 Fix B — Interleaved Watermark Skip (`standardiser.py` — `_peel_trailing_amounts`)

**Problem:** UCO Bank prints a "Account Name: HARI SH R AM" page watermark whose fragments ("Na", ":", "SH", "R", "AM") bleed into transaction text between the amount and running balance. `_peel_trailing_amounts` peeled the balance, hit a watermark fragment, and stopped — returning only 1 money token → row dropped. AccountStmt_1228XXXXXX3352.pdf had 34 rows silently dropped.

**Fix:** In phase 2 of the peel loop, allow exactly one skip of a `_is_trailing_code(tok)` or `:` token, but only when exactly 1 money token (the balance) has been peeled and the skip has not already been used (`not _interleaved_skipped`). Guard prevents the skip from firing when 2+ tokens already found (narration words like "BANK" stay in narration).

**Result:** 218/218 rows, recon=1.000, comp=1.000. ✓

### 7.3 Fix C — Leading-Dot Decimal Normalisation (`standardiser.py` — `_peel_trailing_amounts`)

**Problem:** Some statements print a zero balance as `.00` instead of `0.00`. `_is_money_token(".00")` returned False (regex requires ≥1 digit before the decimal). `_peel_trailing_amounts` returned 0 money tokens → partial record → row dropped. NITIN statement.pdf had 1 row missing.

**Fix:** Before all peeling, scan the last 4 token positions and normalize any token matching `^\.\d{1,2}$` to `0` + token (e.g. `.00` → `0.00`, `.50` → `0.50`).

**Result:** 49/49 rows, recon=1.000, comp=1.000. ✓

### 7.4 Fix D — Browser-Printed Netbanking PDF URL Filter (`standardiser.py` — `_strip_noise_lines`)

**Problem:** AU Bank's netbanking portal, when printed to PDF from a browser, embeds the page URL and a print timestamp on every page: e.g. `11/28/25, 3:40 PM blob:https://cbsupgrade.aubank.in/...`. The timestamp `11/28/25` matched `DATE_AT_LINE_START_PATTERN`, causing the line to be treated as a new transaction date. This flushed the current FLEXCUBE transaction record (which had amounts but not yet its balance continuation line) before the actual balance (`2,147.00` on the next line) could be captured — giving 42 rows with `Balance=NaN`. soa_0167042251865512.pdf reported recon=0.960.

**Fix:** The `_FOOTER_NOISE` regex already contained `https?://`. The guard that prevented filtering date-like lines was: `not is_date_start`. Changed to: `not is_date_start OR money_tok_count == 0`. Real transactions always carry ≥2 money tokens, so they are safe. A browser URL line carrying zero money tokens is now dropped even if its timestamp accidentally matches a date pattern.

**Result:** 1928/1928 rows, recon=1.000, comp=1.000, 0 NaN balance rows. ✓

### 7.5 Full-Dataset Validation — Final Numbers (103 files, batch_20260626_193018)

```
Session     : batch_20260626_193018
Duration    : 4453.2s (~74 minutes)
Files OK    : 103 / 103
Total rows  : 114,449   clean=114,367   flagged=82
LLM calls   : 42
Tier mix    : cheap_parse=76  excel_deterministic=23  excel_columnmap=1  llm_full_read=3

Pass (recon ≥ 0.90) : 92 / 103
Perfect (recon=1.00) : 76 / 103
All 43 unit tests    : PASS
```

**Files below 0.90 (10 files — all known, unfixable at parser level):**

| File | Recon | Reason |
|---|---|---|
| 9 × `.xls` files | 0.000 | HTML/XML-masquerading-as-XLS (corrupt format); all have working PDF counterparts at 1.000 |
| `CASA_STATEMENT_8442098066767557.pdf` | ~0.23 | Integer-only amounts with no decimal on early pages; `.xlsx` counterpart extracts 100% |
| `211566492688.pdf` | 0.891 | Groq API 413 token-limit error on this specific file; not a parser bug |

**Near-misses (recon 0.90–0.999):**

| File | Recon | Note |
|---|---|---|
| `statement (2).pdf` | 0.980 | Bank of Baroda, LLM full-read path — 1 flagged row due to unusual multi-line entry |
| `Statement.pdf` | 0.979 | GST invoice detail table appended after transactions is misread as rows (known layout) |

---

<a name="status"></a>
## 8. Current Architecture Status

### What works reliably

| Source type | Tier reached | LLM calls (typical) | Notes |
|---|---|---|---|
| Digital PDF (clean layout) | Tier 2 (cheap parse) | 0–1 (metadata regex succeeds) | Validated on 76+ statements |
| Excel / CSV (standard headers) | Deterministic | 0 | `_COL_KEYWORDS` maps all common spellings |
| Excel with metadata block | Deterministic | 0 | `_parse_metadata_block` reads identity above table |
| Scanned PDF | Vision per page | 1 per page (cached) | GROQ2 only, GROQ1 not touched |
| Image | Vision | 1 (cached) | GROQ2 only |
| DOCX | Tier 2–5 text path | 0–2 | Same as digital PDF |
| Digital PDF (unusual layout) | Tier 4–5 | 1–2 | Schema discovery or full read as needed |

### Known working constraints
- 429 TPD fail-fast prevents retry storms on quota exhaustion.
- Newest-first statements are detected and flipped automatically by `grade_parse`.
- No row is ever silently dropped — everything unparseable goes to `flagged_transactions.csv` with a reason.
- No code path branches on a bank name (verified by reading all extraction/*.py).
- Raw bank data is anonymised before every text LLM call.

---

<a name="next-phase"></a>
## 9. How to Move to the Analysis Phase

The extraction phase outputs two things: a **returned Python dict** (the fast in-process path) and a **disk output** under `outputs/extractions/<session_id>/`. The analysis phase can consume either.

### What the analysis phase receives

From the returned dict:
```python
result = {
    "clean_df": pd.DataFrame,         # unified clean transactions, STANDARD_COLUMNS schema
    "flagged_df": pd.DataFrame,        # rows that failed a check, with flag_reason
    "session_id": str,
    "clean_rows": int,
    "flagged_rows": int,
    "files_processed": int,
    "files_failed": [str],
    "per_file": [dict],                # per-file audit records (tier, reconciliation rate, LLM calls)
    "storage_paths": dict,             # paths to the CSVs and JSONs written to disk
}
```

From disk (`outputs/extractions/<session_id>/`):
- `clean_transactions.csv` — the main table (oldest-first, all accounts combined)
- `flagged_transactions.csv` — rows for manual investigator review
- `metadata.json` — audit receipt
- `statements/*.json` — one JSON per account with its own identity and transactions

From ChromaDB (if `ingest_to_chromadb=True` was passed): a per-session collection of transaction embeddings for the RAG chatbot.

### What the analysis phase needs to do

The analysis phase (Phase 2) is responsible for:
1. **Pattern analysis:** unusual narrations, round-number transactions, large-value clustering, velocity patterns (many transactions in a short window).
2. **Network analysis:** counterparty mapping (who sends/receives money with whom), using the `Narration` column for UPI IDs and account references.
3. **Suspect flagging:** applying investigator-defined thresholds to produce a shortlist.
4. **Report generation:** a human-readable PDF/HTML report using `reportlab`/`matplotlib`.
5. **RAG chatbot:** natural-language Q&A over the extracted transactions using ChromaDB + an LLM.
6. **Reversed-transaction exclusion:** the `is_reversed` flag on clean_df must be respected — reversed transactions should not count toward cumulative totals.
7. **Balance-mismatch interpretation:** `flagged_df` rows with `flag_reason == "balance_mismatch"` may indicate either an extraction gap OR a fraudulent alteration. The analysis phase is the right place to investigate which.

### Integration point

```python
from extraction.extraction_pipeline import run_extraction_pipeline

result = run_extraction_pipeline(
    files=[{"file_path": "/path/to/statement.pdf", "account_id": "ACC001", "bank_name": "SBI"}],
    session_id="investigation_20260623",
    ingest_to_chromadb=True,   # turn on for RAG chatbot
    persist=True,
)
clean_df = result["clean_df"]
# analysis code uses clean_df directly or reads from result["storage_paths"]["clean_csv"]
```

GROQ3 (the third API key) is for the analysis and reporting phase. It is intentionally not loaded in `config/settings.py` during extraction so there is no accidental cross-contamination of quota.

---

<a name="drawbacks"></a>
## 10. Known Drawbacks and Pending Issues

### 10.1 Metadata Extraction Incomplete for Many Files (HIGH PRIORITY for next session)

**This is a significant gap.** The `account_extractor.py` regex extracts account holder name, account number, and IFSC code correctly for statements where this information is printed in a standard `Label: Value` format (e.g. `Account No. : 123456789`). However, a large number of the 103 statements in the dataset return empty or "Unknown" for one or more of these fields. Observed failure modes:

- **Name not extracted:** Many PDFs print the holder name in a formatted header box without a keyword label (e.g. the name appears in a bold font on its own line with no prefix). The regex requires a label like "Account Holder:" before the name.
- **Account number not extracted:** Some statements hide the account number in a masked format (`XXXX1234`) or present it mid-page in a table rather than in the header. The regex reads only the first ~60 lines of text.
- **Bank name shown as "Unknown Bank":** IFSC-to-bank mapping covers major banks but not all cooperative banks or smaller RRBs. If the IFSC prefix is not in the lookup table, bank name stays "Unknown".
- **Impact on analysis phase:** The per-statement JSON files under `statements/` are keyed by `<holder>_<account>`. When holder and account are empty, all files collapse to the same key and overwrite each other. The analysis phase cannot link transactions to a named suspect when the identity fields are blank.

**Recommended fix:** Expand `account_extractor.py` with:
1. A wider scan window (first 100 lines, not 60) with a secondary pass on the last 20 lines for statements that print identity in the footer.
2. Additional name-detection patterns: line that is all-caps or title-case, between 2 and 6 words, no digits, appearing within the first 20 lines — likely the account holder name.
3. A masked-account-number pattern: `[Xx*]{4,}\d{4}` or partial reveal formats common in Indian bank exports.
4. Expand `IFSC_PREFIX_TO_BANK` to cover cooperative banks, small finance banks, RRBs using the RBI's published IFSC directory.
5. LLM metadata fallback (Tier 1) is already in place — ensure it is triggered whenever any key field (holder, number, IFSC) is missing, not only when all three are missing.

### 10.2 LLM Client Created in 3 Places (not a true one-file local-model swap)

`llm_interface.py` is the intended facade for a future local model swap, but the actual Groq client is still instantiated in `llm_structurer.py`, `column_identifier.py`, and `vision_extractor.py`. Swapping to Ollama or LM Studio requires editing 4 files, not 1.

### 10.3 Digital PDF Parser Layout Assumptions (substantially narrowed in Session 3 & 4)

`standardise_digital_pdf_transactions` anchors on a date at the line start. The remaining genuinely-hard cases are (a) amounts printed without decimals (`100000`, `300`), which collide in shape with reference/instrument numbers, and (b) a date that is not at the line start. Both correctly route to Tier 4/5.

### 10.4 Image Grouping / Multi-Page Screenshot Problem (not implemented)

When an investigator uploads multiple screenshots of the same statement (e.g. scrolling through a mobile banking app), the system treats each image as a separate statement. The correct behavior is to group them as one statement using balance-chain continuity as the grouping signal.

### 10.5 Sequential File Loop (no parallelism)

`run_extraction_pipeline` processes files in a plain `for` loop. For a large batch with scanned PDFs, a bounded `ThreadPoolExecutor` around `_process_single_file` would be the straightforward fix.

### 10.6 No Row-Level Repair (Tier 5 repairs the whole statement)

The current Tier 5 sends the full statement to the LLM. `grade_parse` already returns `failing_row_indices`; a targeted repair loop sending only failing rows would cost a fraction of the tokens.

### 10.7 Tesseract OCR for Scanned PDFs Is Now Unused in the Main Path

After Session 2, scanned PDFs go directly to GROQ2 vision. Tesseract's preprocessing improvements (deskew, denoise, sharpen) are not applied to scanned PDF pages before they go to vision — if GROQ2 struggles with a blurry or rotated page, a hybrid path would help.

### 10.8 Outputs Folder Contains Test Session Artifacts

`outputs/extractions/` holds test session folders from development runs. The content is git-ignored and safe (no real PII), but the folder should be cleared before a production demo.

### 10.9 ChromaDB and Sentence Transformer Not Tested End-to-End

The `chromadb_ingestor.py` is imported lazily and disabled by default. It has not been exercised in a full extraction run. When `ingest_to_chromadb=True` is first passed, there may be dependency or model-download issues.

### 10.10 9 `.xls` Files Permanently at recon=0.000

These files are HTML/XML content saved with a `.xls` extension. `openpyxl` and `xlrd` cannot read them as spreadsheets. Each has a working PDF counterpart that extracts at recon=1.000. The correct fix is to route the HTML content through BeautifulSoup or warn the user at upload time that the file format is corrupt. For the hackathon, the PDF counterparts cover the data.

---

## Summary Table of All Changes

| What changed | File(s) | Session |
|---|---|---|
| `count_transaction_like_lines` + format-agnostic guard | `standardiser.py`, `extraction_pipeline.py` | 1 |
| 429 TPD fail-fast in vision path | `extractor_ocr.py` | 2 |
| 429 TPD fail-fast in text path | `llm_structurer.py` | 2 |
| Scanned PDF → GROQ2 vision per page | `vision_extractor.py` | 2 |
| `llm_interface.py` single LLM facade | `llm_interface.py` (new) | 2 |
| `grade_parse()` validator referee | `validator.py` | 2 |
| `build_schema_sample()` | `standardiser.py` | 2 |
| Tiered escalation ladder (`_extract_text_transactions`) | `extraction_pipeline.py` | 2 |
| `read_scanned_pdf` branch in pipeline | `extraction_pipeline.py` | 2 |
| Metadata code-first (Tier 1 regex before LLM) | `extraction_pipeline.py` | 2 |
| Newest-first sort from `grade_parse` ordering | `extraction_pipeline.py` | 2 |
| Deterministic column mapping (`_COL_KEYWORDS`, `_infer_column_map`) | `extractor_excel_csv.py` | 2 |
| Metadata block parsing (`_parse_metadata_block`, `_META_LABELS`) | `extractor_excel_csv.py` | 2 |
| Width-aware header detection (`_detect_header_index`) | `extractor_excel_csv.py` | 2 |
| Excel/CSV 0-LLM-call path in pipeline | `extraction_pipeline.py` | 2 |
| Tiered threshold constants | `config/settings.py` | 2 |
| Trailing non-money column peel (`_peel_trailing_amounts` Fix 1) | `standardiser.py` | 3 |
| Score-based non-regressing escalation (Fix 2) | `validator.py`, `extraction_pipeline.py` | 3 |
| Narration-LAST layout support (`_peel_leading_amounts` Fix 3) | `standardiser.py` | 3 |
| Bank name from footer legend (Fix 4) | `account_extractor.py` | 3 |
| Collapsed-rows detection guard (Fix A) | `extractor_digital_pdf.py` | 4 |
| Interleaved watermark skip in peel loop (Fix B) | `standardiser.py` | 4 |
| Leading-dot decimal normalisation `.00` → `0.00` (Fix C) | `standardiser.py` | 4 |
| Browser URL timestamp filter in `_strip_noise_lines` (Fix D) | `standardiser.py` | 4 |
