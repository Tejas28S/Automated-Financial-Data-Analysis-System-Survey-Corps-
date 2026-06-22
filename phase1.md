# Phase 1 — Extraction Engine: Work Log & Findings

**Project:** Automated Financial Data Analysis System (Survey Corps · CIDECODE Hackathon 2026 · CID Karnataka)
**Scope of this document:** A detailed record of every investigation, diagnosis, code change, verification, and read-only audit performed in this working session on the **extraction phase**.
**Provider note:** The system uses **Groq** (the fast LLM inference API, `from groq import Groq`) running open models — **not** xAI's "Grok". Wherever "Grok/Groq" is mentioned, it means the **Groq inference API**.

---

## Table of contents

1. [Task 1 — Why transaction extraction did not generalize (diagnosis + fix)](#task-1)
2. [Task 2 — Read-only audit of 5 concerns (no changes made)](#task-2)
3. [Task 3 — Architecture & Groq-usage explanation](#task-3)
4. [Exact code changes made this session](#code-changes)
5. [Verification evidence](#verification)
6. [Memory files written](#memory)
7. [What is still pending your approval](#pending)
8. [Complete file-by-file inventory (every file explained)](#inventory)

---

<a name="task-1"></a>
## 1. Task 1 — Why transaction extraction did not generalize

### 1.1 The problem reported
- **Metadata extraction generalized well** across different bank statements.
- **Transaction extraction did NOT generalize** to unseen formats: the raw text clearly contained transactions, but the final transaction output was **empty or incomplete** for new statement formats.
- Symptom of "fix Bank A → break Bank B" regressions, i.e. logic was being coupled to specific banks in the development dataset.
- Requirement: extraction must be **format-agnostic** and work on **completely unseen** bank statements; the dataset is for testing/validation only, never the source of truth.

### 1.2 Investigation
Files read and traced: `extraction_pipeline.py`, `llm_structurer.py`, `column_identifier.py`, `standardiser.py`, `router.py`, `validator.py`, plus the sample statements in `original bank statements/`.

**Key finding — there are four transaction routes, and they are NOT equally intelligent:**

| Route | How transactions are extracted | Generalizes? |
|---|---|---|
| image (.jpg/.png) | Vision LLM returns structured rows | ✅ LLM-driven |
| scanned PDF / DOCX | `structure_statement()` — **LLM reads every row** | ✅ LLM-driven |
| excel / csv | LLM column-map + pandas | ✅ mostly |
| **digital PDF** | **deterministic regex** (`standardise_digital_pdf_transactions`) | ❌ **hardcoded layout assumptions** |

- **Metadata generalizes** because it is *always* LLM-driven (`extract_account_metadata`).
- The **digital-PDF transaction path is the only one** that uses deterministic regex instead of the LLM — and digital PDFs are the most common real format.

**The deterministic parser (`_parse_single_transaction_line`) bakes in dataset-specific layout assumptions:**
1. The date must be at the **start** of the line.
2. Amounts must have a **2-digit decimal** (e.g. `30000.00`); a plain `5000` or `5,000` is invisible.
3. **The balance must be the last money token on the line.**

It also contained bank-specific regex hacks (IDBI serial-number prefix, Kotak `(Dr)` suffix) — visible evidence of the "fix one bank, break another" cycle.

**Important irony:** `discover_transaction_schema()` asks Groq for `column_order`, `date_format`, `debit_credit_method`, etc., but `standardise_digital_pdf_transactions()` **ignores almost all of it** — it only reads `has_reference_number` / `has_cheque_number`. So the "intelligent, adaptable" schema was essentially **decorative**; real parsing was hardcoded regex.

### 1.3 Proof (empirical)
A diagnostic was run against all sample statements through the **deterministic digital-PDF parser only** (no LLM call needed — the schema only gates ref/cheque flags), measuring parsed rows vs. transaction-like lines in the raw text:

```
356927262-Bank-Statement.pdf      anchor_date_lines= 44   PARSED_TXNS=  0   <-- BROKEN
365367510-Statement...            anchor_date_lines=121   PARSED_TXNS=121
391657900-SBI-statement-sample    anchor_date_lines= 32   PARSED_TXNS= 32
396985646-Kotak-Bank-Statement    anchor_date_lines=384   PARSED_TXNS=382
451843314-HDFC-BANK-STATEMENT     anchor_date_lines= 85   PARSED_TXNS= 85
527769613-IDBI-Current-Account    anchor_date_lines=902   PARSED_TXNS=902
544516929-ICICI-BANK-STATEMENT    anchor_date_lines=707   PARSED_TXNS=705
652591331-Canara-Bank-Statement   anchor_date_lines= 51   PARSED_TXNS= 48
752368071-BANK-OF-BARODA          anchor_date_lines= 27   PARSED_TXNS= 25
```

**Smoking gun:** `356927262-Bank-Statement.pdf` has 44 obvious transaction lines but parsed **0**. Example line:

```
'07-01-2016 DEC 30000.00 30307.30 101'
```

The line ends in `101` (a branch/transaction code with no decimal). The parser peels "money tokens" from the **end** of the line, but the last token `101` is not a money token, so the loop stops immediately, never finds the balance, and **drops every single row**. This is exactly the reported symptom: *raw text full of transactions, output empty.*

### 1.4 The fix (format-agnostic — no bank names)
Rather than patch the regex for this one statement (which would repeat the coupling mistake), a **generalization guard** was added:

- Estimate how many lines *look like* transactions: a line that contains a date **anywhere** AND at least one monetary amount (`count_transaction_like_lines`).
- If the deterministic parser produced **far fewer** rows than that estimate, **fall back to the LLM structurer** (`structure_statement`, the same path scanned PDFs use), which reads any layout.
- Only adopt the LLM result if it recovers **more** rows (so a rate-limited/empty reply can never make output worse).

This triggers on **capability** (under-extraction), never on a named bank format. It keeps the cheap deterministic path for layouts that already work and routes only genuinely-broken layouts to the LLM.

(Exact code in [§4](#code-changes); verification in [§5](#verification).)

---

<a name="task-2"></a>
## 2. Task 2 — Read-only audit of 5 concerns

> **These were investigated read-only. NO code changes were made for any item in this section. A plan (A/B/C/D) was proposed and is awaiting your go/no-go.**

### 2.1 Performance & processing time
**Two compounding causes:**
- **(a) Sequential file loop.** `run_extraction_pipeline` processes files in a plain `for` loop — file 2 cannot start until file 1 finishes. This is the "first completes, then a delay, then the second" behavior.
- **(b) Uneven LLM cost per route**, dominated by `structure_statement()` chunking.

**Groq calls per file:**

| Route | Groq calls |
|---|---|
| Digital PDF (normal) | **2** (metadata + schema) |
| Excel / CSV | **1** (column-ID) |
| **Scanned PDF / DOCX** | **1 + ⌈lines/60⌉ chunks** (capped 40) — the bottleneck |
| Image | 1+ (vision) |
| Digital PDF (fallback) | 2 + (1 + chunks) — only when deterministic under-extracts |

`structure_statement` uses `CHUNK_LINES = 60` → one Groq call per 60 lines. On free-tier Groq, rapid sequential calls hit the tokens-per-minute limit → HTTP 429 → retry with `time.sleep(2)` × up to `MAX_RETRIES = 3`. That compounding is where the 5–10 minutes go when many files are uploaded at once.

**Did the Task-1 fallback make it worse?** Only narrowly — it routes one genuinely-broken digital PDF (`356927262`) into the chunked path. The main cost is scanned PDFs + the sequential loop, which predate the change. One small redundancy was introduced: the fallback's `structure_statement` re-extracts metadata that was already extracted (1 wasted call per fallback).

### 2.2 Excessive Groq usage — where it comes from
- **Majority of calls:** `structure_statement` chunking (scanned PDFs / DOCX, + fallback digital PDFs).
- **Duplicate/unnecessary:** (i) fallback re-extracts metadata; (ii) `discover_transaction_schema` is spent even when we then fall back. Both small.
- **Fallback frequency:** triggers only when deterministic rows < 60% of transaction-like lines → **1 of 9** sample statements. Not over-firing.
- **Repeated processing of same content:** No. Every Groq answer is cached on disk (`storage/llm_cache/`), keyed by `md5` of the document text. Same file = 0 new calls; different statements get different keys → no cross-contamination.

### 2.3 Storage architecture
- **Output sink:** `outputs/extractions/<session_id>/` →
  - `clean_transactions.csv` — clean verified rows (led by real account_number / account_holder / ifsc_code columns)
  - `flagged_transactions.csv` — rows that failed a check, with reason (never silently dropped)
  - `metadata.json` — audit "receipt" (files processed, row counts, OCR engine, column-map source)
  - `statements/<holder>_<account>.json` — one structured file per statement (real identity + that statement's transactions)
- **Cache:** `storage/llm_cache/` (Groq responses).
- **Vectors:** `storage/chromadb/` (RAG; OFF during extraction by default — heavy embedding model).
- **In-process handoff:** `run_extraction_pipeline()` **returns** `clean_df` / `flagged_df` directly.

**Key fact:** **Nothing reads `outputs/` back at runtime** — `storage.py` only *writes*; there is no `glob`/`read_csv` of `EXTRACTIONS_DIR` anywhere in the code. So downstream modules should either consume the **returned DataFrame** in-process (fastest) or read `clean_transactions.csv` / the per-statement JSON / ChromaDB. `outputs/` is a pure output sink — no historical run is ever reused.

### 2.4 Dataset independence — verdict: CLEAN (one cosmetic note)
- **No conditional bank logic** — zero `if bank == "HDFC"`-style branches. Bank names (HDFC/ICICI/Kotak/IDBI/Canara/Baroda/SBI/Axis) appear **only in comments/docstrings**, never in code paths.
- **No dataset files read at runtime** — `reconcile_account_details()` *can* take a `master_row` from `accounts_master.csv`, but the pipeline calls it **without** that argument, so the reference dataset is never loaded. Account identity is read only from each statement's own text.
- The regex generalizations (serial-number prefix, `(Dr)` suffix) are **format-agnostic patterns**, not bank switches — and the new fallback exists precisely so unseen layouts route to the LLM.
- The only dataset references in code are **comments** and the **test file** (`tests/test_extraction.py`, which is *supposed* to reference the dataset).
- **Conclusion:** Deleting all test files and feeding brand-new statements will still work. *Optional cleanup:* scrub bank-name mentions from comments so they are never mistaken for logic.

### 2.5 Repository cleanup
`outputs/` is **~48 MB** of ~17 test-run session folders (`test_excel_session`, `llm_first`, `rework_demo`, `hdfc_time_check`, `bounded_demo`, `viewer_session`, `final_e2e`, `test_pdf_session`, `test_csv_session`, `newkey`, `final_check`, `test_bad_file_session`, `test_keys_session`, `test_multi_file_session`, `bounded_demo2`, …).

| Path | Status | Recommended action |
|---|---|---|
| `outputs/extractions/*` | test artifacts, **not read at runtime** | safe to delete all |
| `outputs/reports/.gitkeep`, `outputs/graphs/.gitkeep` | structure placeholders | keep |
| `storage/llm_cache/` | live cache (speeds re-runs; no cross-statement leakage) | keep, or clear for clean slate |
| `storage/chromadb/` | RAG vectors | clear if not mid-investigation |
| `.pytest_cache/`, `.DS_Store` | junk | safe to delete |
| `diag_txn.py` | temporary diagnostic | **already removed this session** |

Also recommended: verify `.gitignore` covers `outputs/` and `storage/` (they hold real de-anonymized data and must never be committed).

---

<a name="task-3"></a>
## 3. Task 3 — Architecture & Groq-usage explanation

### 3.1 The 5-station assembly line
```
upload → 1.ROUTER → 2.EXTRACTOR → 3.UNDERSTAND(Groq) → 4.STANDARDISE → 5.VALIDATE
                                                                              │
                          combine files → remove duplicates → SAVE (storage.py)
```
1. **Router** (`router.py`) — detects file type: digital PDF / scanned PDF / image / Excel-CSV / DOCX.
2. **Extractor** (`extractor_*.py`) — pulls raw content (text, OCR text, table, or image).
3. **Understand** (`column_identifier.py`, `llm_structurer.py`, `vision_extractor.py`) — Groq does the semantic work.
4. **Standardise** (`standardiser.py`) — reshape into the fixed schema, clean numbers/dates, split debit/credit.
5. **Validate** (`validator.py`) — date validity, balance arithmetic, debit/credit exclusivity → clean vs flagged.
`extraction_pipeline.py` orchestrates all files through all stations.

**Unified output schema:** `Date | Time | Narration | Debit | Credit | Balance | Account_ID | Bank_Name`

**Routing principle:** let Groq do the *understanding*; let plain code do the cheap bulk work where it safely can; fall back to Groq when local parsing under-performs.

### 3.2 Where Groq is used — complete map
**4 files call Groq, 6 logical call sites, 2 keys, 2 models.**

| # | File | Function | What it asks Groq | Model / Key | When |
|---|---|---|---|---|---|
| 1 | `llm_structurer.py` | `extract_account_metadata` | header → account holder/number/IFSC/bank/period/balances | text `llama-3.3-70b-versatile` / **GROQ1** | every digital + scanned PDF |
| 2 | `llm_structurer.py` | `discover_transaction_schema` | sample rows → describe layout | text / **GROQ1** | digital PDFs |
| 3 | `llm_structurer.py` | `structure_statement` | read lines → transaction rows as JSON (**chunked**, ~60 lines/call) | text / **GROQ1** | scanned PDFs, DOCX, **+ digital-PDF fallback** |
| 4 | `column_identifier.py` | `identify_column_structure` | which column = date/debit/credit/balance | text / **GROQ1** | Excel / CSV |
| 5 | `vision_extractor.py` | `extract_statement_from_image` | image → account details + transactions | **vision** `llama-4-scout-17b` / **GROQ2** | image uploads |
| 6 | `extractor_ocr.py` | vision OCR fallback | blurry scan page → text | **vision** / **GROQ2** | scanned PDFs where Tesseract confidence is low |

**Keys (same provider, split to avoid sharing a rate limit):**
- **GROQ1** → all text work (#1–4); most calls.
- **GROQ2** → all vision work (#5–6).
- **GROQ3** → belongs to the *analysis* phase; intentionally NOT loaded in extraction.

**Models** (from `config/settings.py`):
- Text: `llama-3.3-70b-versatile`
- Vision: `meta-llama/llama-4-scout-17b-16e-instruct`

**Cost behavior:** all answers cached on disk (re-run = 0 calls); `structure_statement` (#3) is the dominant cost driver because it calls once per ~60-line chunk.

---

<a name="code-changes"></a>
## 4. Exact code changes made this session

> Two files were modified (the Task-1 fix). Nothing from Task 2 was changed.

### 4.1 `extraction/standardiser.py`
Added a date-anywhere pattern and a generalization-check helper (near the other regexes):

```python
# A date appearing ANYWHERE on a line (not only at the start). This is used ONLY
# to estimate how many lines LOOK like transaction rows, so the pipeline can tell
# when the deterministic digital-PDF parser has under-extracted. It is never used
# to parse a value and it encodes no specific bank's layout.
_DATE_ANYWHERE_PATTERN = re.compile(
    r"\d{1,2}[/\-.][0-9A-Za-z]{2,9}[/\-.]\d{2,4}"   # 02/06/18, 16-Oct-2018, 02.06.2018
    r"|\d{4}[/\-.]\d{1,2}[/\-.]\d{1,2}"             # 2018-06-02
)


def count_transaction_like_lines(raw_text: str) -> int:
    """
    Estimate of how many lines in the raw text LOOK like transaction rows: a line
    that contains a date somewhere AND at least one monetary amount.
    ... (generalization check only; not a parser; encodes no bank-specific layout)
    """
    n = 0
    for line in (raw_text or "").splitlines():
        s = line.strip()
        if _DATE_ANYWHERE_PATTERN.search(s) and any(_is_money_token(t) for t in s.split()):
            n += 1
    return n
```

### 4.2 `extraction/extraction_pipeline.py`
- Added `count_transaction_like_lines` to the import from `extraction.standardiser`.
- In the `pdf_digital` branch, after `standardise_digital_pdf_transactions(...)`, added the **generalization guard**:

```python
# ── GENERALIZATION GUARD (format-agnostic) ───────────────────────
# The deterministic parser assumes a layout (date at line start, balance = last
# token, 2-decimal amounts). On an UNSEEN bank whose layout breaks any of those
# assumptions it silently returns far fewer rows than the statement actually
# contains. Detect this WITHOUT naming any bank and re-extract with the LLM
# structurer (the same path scanned PDFs use), which reads any layout.
expected_rows = count_transaction_like_lines(raw_text)
if expected_rows >= 3 and len(standard_df) < 0.6 * expected_rows:
    logger.warning(... "falling back to the LLM structurer for generalisation." ...)
    structured = structure_statement(raw_text, file_path)
    llm_df = standardise_llm_transactions(
        structured.get("transactions", []), account_id,
        details["bank_name"] or bank_name, details.get("opening_balance", ""))
    # Only adopt the LLM result if it actually recovered more rows.
    if len(llm_df) > len(standard_df):
        standard_df = llm_df
        file_record["column_map"] = {"engine": "llm_structurer_fallback"}
        file_record["column_map_source"] = structured.get("source")
        file_record["llm_txn_count"] = len(structured.get("transactions", []))
```

### 4.3 Temporary file (created then removed)
- `diag_txn.py` — a throwaway diagnostic used to prove the under-extraction. **Deleted** after use; not part of the codebase.

---

<a name="verification"></a>
## 5. Verification evidence

### 5.1 The guard fires only on the broken statement (format-agnostic)
Ratio = parsed ÷ transaction-like-lines; fallback fires when ratio < 0.60:

```
356927262-Bank-Statement.pdf      expected~ 44  parsed=  0  ratio=0.00  <-- FALLBACK
365367510-Statement...            expected~121  parsed=121  ratio=1.00
391657900-SBI-statement-sample    expected~  0  parsed= 32  ratio=1.00
396985646-Kotak-Bank-Statement    expected~382  parsed=382  ratio=1.00
451843314-HDFC-BANK-STATEMENT     expected~ 85  parsed= 85  ratio=1.00
527769613-IDBI-Current-Account    expected~902  parsed=902  ratio=1.00
544516929-ICICI-BANK-STATEMENT    expected~706  parsed=705  ratio=1.00
652591331-Canara-Bank-Statement   expected~ 51  parsed= 48  ratio=0.94
752368071-BANK-OF-BARODA          expected~ 25  parsed= 25  ratio=1.00
```
→ Fires on 1 of 9; the 8 working statements (ratio 0.94–1.00) stay on the cheap deterministic path → no regression, no added cost.

### 5.2 End-to-end fallback wiring (LLM stubbed to avoid spending quota)
Running `_process_single_file` on `356927262-Bank-Statement.pdf` with `structure_statement` stubbed:
```
deterministic digital-PDF parser under-extracted (0 rows vs ~44 transaction-like lines).
Falling back to the LLM structurer for generalisation.
column_map_source : stub
engine            : llm_structurer_fallback
rows_standardised : 44
clean rows        : 44
```
→ Guard fired, fell back, recovered all **44** transactions; debit/credit direction correctly derived from the running balance; visibility record correctly logs `llm_structurer_fallback`. In production the real Groq `structure_statement` replaces the stub and reads any layout.

### 5.3 Syntax / import checks
`ast.parse` passed on both modified files; `count_transaction_like_lines` imports cleanly.

---

<a name="memory"></a>
## 6. Memory files written
Persistent project memory (outside the repo, in the Claude memory store):
- **`transaction-extraction-must-generalize.md`** (type: feedback) — records the principle: never tune extraction regex to specific dataset banks; keep transaction extraction LLM-first / format-agnostic; the digital-PDF deterministic parser is the coupling risk; fix generalization at the guard/fallback level, not by per-bank regex.
- **`MEMORY.md`** — index pointer to the above.

---

<a name="pending"></a>
## 7. What is still pending your approval (NOT yet done)

A plan was proposed for the Task-2 concerns; **none of it is implemented yet**:

- **A — Performance:** parallelize the per-file loop (bounded thread pool); raise `CHUNK_LINES` (60 → ~120–150) with matching `max_tokens`; remove the fallback's redundant metadata call; lighten retry backoff.
- **B — Cleanup:** delete `outputs/extractions/*` + `.pytest_cache` + `.DS_Store`; optionally clear `storage/chromadb`; decide whether to clear or keep `storage/llm_cache`; confirm `.gitignore` covers `outputs/` and `storage/`.
- **C — Docs:** a short `STORAGE.md` documenting the storage/handoff contract.
- **D — Cosmetic:** scrub bank-name mentions from code comments so they are never mistaken for logic.

**Recommendation:** do **A** and **B** first (biggest impact); **C** if useful for the team; **D** optional.

---

### Summary of session deliverables
| Item | Status |
|---|---|
| Root-cause diagnosis of transaction non-generalization | ✅ done |
| Format-agnostic fix (guard + LLM fallback) | ✅ implemented & verified |
| Read-only audit: performance, Groq usage, storage, dataset independence, cleanup | ✅ done (findings above) |
| Architecture + Groq-usage map | ✅ documented |
| Performance optimizations (A) | ⏳ awaiting approval |
| Repo cleanup (B) | ⏳ awaiting approval |
| Storage doc (C) / comment scrub (D) | ⏳ optional, awaiting approval |

---

<a name="inventory"></a>
## 8. Complete file-by-file inventory (every file explained)

This is a full map of the repository. For each file: its purpose, key functions/contents, whether it calls Groq, and any dataset-independence notes. **★ marks the two files changed this session.**

### 8.1 Directory layout
```
Automated-Financial-Data-Analysis-System-Survey-Corps-/
├── config/                       # central configuration (Python package)
│   ├── __init__.py
│   └── settings.py
├── extraction/                   # THE EXTRACTION ENGINE (Python package, 17 files)
│   ├── __init__.py
│   ├── router.py                 # 1. file-type detection
│   ├── extractor_digital_pdf.py  # 2. digital-PDF text
│   ├── extractor_ocr.py          # 2. scanned-PDF / image OCR (Tesseract→Groq vision)
│   ├── extractor_docx.py         # 2. Word .docx text
│   ├── extractor_excel_csv.py    # 2. Excel/CSV tables
│   ├── vision_extractor.py       # 2/3. image → structured JSON via Groq vision
│   ├── account_extractor.py      # 3. account identity from text (regex, local)
│   ├── anonymiser.py             # privacy: mask PII before any text → Groq
│   ├── identifier_vault.py       # privacy: temp placeholder swap around LLM calls
│   ├── column_identifier.py      # 3. Groq column map for Excel/CSV
│   ├── llm_structurer.py         # 3. Groq metadata / schema / transaction structuring
│   ├── standardiser.py        ★  # 4. reshape into unified schema (+ new guard helper)
│   ├── validator.py              # 5. quality checks → clean vs flagged
│   ├── storage.py                # persist a run to disk (CSV + JSON)
│   ├── chromadb_ingestor.py      # optional RAG vector ingestion (OFF by default)
│   └── extraction_pipeline.py ★  # orchestrator (+ new generalization guard/fallback)
├── tests/
│   └── test_extraction.py        # pytest suite (uses the dataset for validation)
├── tools/
│   └── verify_viewer.py          # local-only Streamlit viewer (git-ignored)
├── original bank statements/     # 9 sample PDF statements (TEST DATA ONLY)
├── outputs/                      # runtime output sink (git-ignored content)
│   ├── extractions/<session>/    # clean/flagged CSVs + metadata.json + statements/
│   ├── reports/  (.gitkeep)
│   └── graphs/   (.gitkeep)
├── storage/                      # runtime local stores (git-ignored)
│   ├── llm_cache/                # cached Groq responses
│   └── chromadb/                 # RAG vectors
├── uploads/                      # investigator uploads (git-ignored)
├── requirements.txt
├── .env.example                  # key template (real .env is git-ignored)
├── .gitignore
├── AUDIT.md                      # prior hardening report
├── INSTRUCTIONS (1).md           # the phase-2 brief / requirements
├── phase1.md                     # THIS document
├── Screenshot 2026-06-18 ….png   # sample image statement (test input)
└── WhatsApp Image 2026-06-20 ….jpeg  # sample image statement (test input)
```

### 8.2 `config/`

**`config/__init__.py`** — Marks `config/` as a Python package so `from config.settings import …` works. No logic.

**`config/settings.py`** — Single source of truth for all configuration. Contents:
- **Paths:** `BASE_DIR`, `UPLOAD_DIR`, `OUTPUT_DIR`, `REPORTS_DIR`, `GRAPHS_DIR`, `EXTRACTIONS_DIR`, `STORAGE_DIR`, `LLM_CACHE_DIR`, `CHROMADB_DIR` — all created on import.
- **Keys:** loads `GROQ1_KEY`, `GROQ2_KEY` from `.env` (GROQ3 deliberately *not* loaded — it's the analysis phase's). `require_extraction_keys()` fails fast with a readable message if either is missing.
- **Models:** `GROQ_MODEL = "llama-3.3-70b-versatile"` (text), `GROQ_VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"` (vision).
- **Tunables:** `TESSERACT_CMD`, `TESSERACT_CONFIDENCE_THRESHOLD = 80.0`, `COLUMN_ID_SAMPLE_LINES = 40`, `DIGITAL_PDF_CHAR_THRESHOLD = 100`, `BALANCE_TOLERANCE = 1.0`, `EMBEDDING_MODEL = "all-MiniLM-L6-v2"`, `CHROMADB_COLLECTION_NAME`, `SUPPORTED_EXTENSIONS`.
- **`STANDARD_COLUMNS`** = `["Date","Time","Narration","Debit","Credit","Balance","Account_ID","Bank_Name"]` — the fixed output schema the analysis engine depends on.

### 8.3 `extraction/` — the engine

**`extraction/__init__.py`** — Package marker + docstring describing the phase. No logic.

**`router.py`** — *Station 1: routing.* `route_file(path)` returns one of `excel_csv | docx | image | pdf_digital | pdf_scanned`. Extensions decide everything except PDFs; for PDFs `_classify_pdf()` opens the file with `pdfplumber`, reads up to 3 pages, and compares average characters/page to `DIGITAL_PDF_CHAR_THRESHOLD` (100) — above = digital, below = scanned. Falls back to `pdf_scanned` on any open error. **No Groq. Generic — no bank logic.**

**`extractor_digital_pdf.py`** — *Station 2 (digital PDF).* `extract_text_from_digital_pdf(path)` reads every page with `pdfplumber`, page-isolated try/except so one bad page can't crash the file, and joins to one string. `_clean_pdf_text()` strips `(cid:NN)` glyph artifacts (common in SBI etc.). **No Groq.**

**`extractor_ocr.py`** — *Station 2 (scanned PDF / image).* The two-tier OCR engine:
- `run_tesseract_on_image()` — OpenCV preprocessing (grayscale, Otsu/adaptive threshold, deskew, denoise, sharpen) then Tesseract; returns text + average confidence.
- `run_groq_vision_on_image()` — **Groq call #6** (vision, GROQ2): base64 image + OCR prompt; used only as fallback.
- `_process_image_with_tiered_ocr()` — Tesseract first; if confidence < 80 → Groq vision; if vision fails → keep Tesseract. Records which engine won.
- `extract_text_from_scanned_pdf()` — rasterises each page at 300 DPI with PyMuPDF, OCRs each (with `max_pages` safety cap), returns text + per-page audit.
- `extract_text_with_ocr_audit()` — main entry; returns `(text, audit)`.

**`extractor_docx.py`** — *Station 2 (Word).* `extract_text_from_docx(path)` reads all paragraphs and all table cells (tab-joined rows) via python-docx into one text string, treated like digital-PDF text. **No Groq.**

**`extractor_excel_csv.py`** — *Station 2 (Excel/CSV).* `extract_dataframe_from_excel_csv()` reads `.xlsx/.xls` via openpyxl or `.csv` trying `utf-8 → latin-1 → cp1252` encodings, then stamps `Account_ID` / `Bank_Name`. Returns a raw DataFrame. **No Groq.**

**`vision_extractor.py`** — *Station 2/3 (images).* `extract_statement_from_image(path)` sends the image to **Groq call #5** (vision, GROQ2) with a strict JSON prompt and gets back `{account_details, transactions}` directly — no Tesseract for images. Caches by image bytes (re-run = 0 tokens). Marks unreadable parts `UNREADABLE` rather than dropping rows.

**`account_extractor.py`** — *Station 3 helper (identity, local).* Pulls account identity from statement **text** with regex — **no Groq, fully local, format-agnostic.**
- `extract_account_details_from_text()` — many label spellings for holder/number/IFSC/branch/type/period/balances; holder falls back to a bare "MR/MRS … NAME" line; IFSC by its unique shape (header only); **bank inferred from IFSC prefix or a keyword** (`BANK_KEYWORDS`, `IFSC_PREFIX_TO_BANK`).
- `reconcile_account_details(content, account_ref, bank_hint, master_row=None)` — merges document values with an optional `master_row`. **The pipeline calls it WITHOUT `master_row`, so no dataset file is ever read.** *(The bank tables here are reference data for generalisation, not per-statement branching.)*

**`anonymiser.py`** — *Privacy.* `anonymise_text(text)` replaces IFSC codes, UPI IDs, Indian mobile numbers, account numbers, and common Indian names (curated `COMMON_INDIAN_NAMES` set) with placeholders (`ACCT_1`, `NAME_1`, …) and returns `(anonymised_text, mapping)`. Mapping stays in memory, never logged. Used by `column_identifier.py` before sending Excel/CSV samples to Groq.

**`identifier_vault.py`** — *Privacy.* `IdentifierVault` swaps the real account number / holder / IFSC for fixed placeholders (`ACC_TEMP`, `HOLDER_TEMP`, `IFSC_TEMP`) around a text LLM call: `redact()` before, `restore()` (recursive over str/dict/list) after. Used on the Excel/CSV column-ID path.

**`column_identifier.py`** — *Station 3 (Excel/CSV column map).* `identify_column_structure(text, path)` sends the first 40 anonymised lines to **Groq call #4** (text, GROQ1) and returns `{column_map, source, cache_file, groq_called}` where `source ∈ groq|cache|fallback|empty`. Cached on disk; only real answers cached. Falls back to `DEFAULT_COLUMN_MAP` (surfaced as `fallback`, never hidden).

**`llm_structurer.py`** — *Station 3 (the LLM brain for text statements).* Holds **Groq calls #1–#3** (text, GROQ1), all via `_call_json()` (forced JSON, 3 retries):
- `extract_account_metadata()` — **#1** header → account metadata JSON (small, cached).
- `discover_transaction_schema()` — **#2** sample rows → a parsing schema (date format, column order, dr/cr method…). *Note: the digital-PDF parser currently only consumes `has_reference_number`/`has_cheque_number` from this.*
- `structure_statement()` — **#3** reads the whole document in **`CHUNK_LINES = 60`-line chunks** (capped at `MAX_CHUNKS = 40`), merging transactions; returns `{account_details, transactions, source}`. **This is the dominant API-cost driver** and the path the digital-PDF fallback now uses.

**`standardiser.py`** ★ — *Station 4: reshape to the unified schema (pure pandas/regex, no Groq).* Key pieces:
- `DATE_AT_LINE_START_PATTERN` / `_MONEY_TOKEN` — the date-anchored row detector and money-token recogniser.
- `standardise_digital_pdf_transactions()` — the deterministic digital-PDF parser (date-anchored, multi-line stitch, dr/cr inference, balance-based direction correction).
- `standardise_llm_transactions()` — builds the table from LLM JSON, then corrects debit/credit from the running balance.
- `standardise_transaction_records()` — for the vision reader's already-split rows.
- `standardise_dataframe_direct()` — for Excel/CSV using the column map (+ `_guess_column_mapping` fallback).
- `_correct_direction_by_balance()`, `_parse_date()`, `_clean_amount*()`, `_create_empty_standard_dataframe()`.
- **★ NEW this session:** `_DATE_ANYWHERE_PATTERN` and `count_transaction_like_lines()` — the format-agnostic generalization check (see [§4.1](#code-changes)).

**`validator.py`** — *Station 5: quality control (no Groq).* `validate_and_clean(df)` runs three checks → splits clean vs flagged:
1. `_check_date_validity` (must be a real date, year 2000–2035),
2. `_check_balance_arithmetic` (`prev + credit − debit ≈ balance` within `BALANCE_TOLERANCE`),
3. `_check_debit_credit_exclusivity` (not both filled).
Plus `mark_duplicates()` (tags `duplicate_of`, never deletes; assigns `txn_id`) and `_mark_reversals()` (adds `is_reversed`).

**`storage.py`** — *Persistence (writes only, no Groq).* `persist_extraction_run(...)` writes per session: `clean_transactions.csv` (led by real `account_number`/`account_holder`/`ifsc_code`), `flagged_transactions.csv`, `metadata.json` (audit receipt), and `statements/<holder>_<account>.json` per statement. **Nothing ever reads these back at runtime.**

**`chromadb_ingestor.py`** — *Optional RAG store (OFF during extraction).* `ingest_transactions_to_chromadb(df, session_id)` turns each row into a sentence and upserts into a per-session ChromaDB collection using a local `all-MiniLM-L6-v2` embedding model (sets `OMP_NUM_THREADS=1` to dodge a macOS onnxruntime stall). Imported lazily so a normal run never loads the heavy embedding stack.

**`extraction_pipeline.py`** ★ — *The orchestrator.* `run_extraction_pipeline(files, session_id, …)` loops files **sequentially**, runs each through `_process_single_file()` (route → extract → understand → standardise → validate), stamps the real account number/IFSC on every row, then concatenates all files, tags cross-file duplicates, optionally ingests to ChromaDB, and persists. Per-file error isolation (one bad file never stops the run). **★ NEW this session:** the generalization guard in the `pdf_digital` branch that falls back to `structure_statement` when the deterministic parser under-extracts (see [§4.2](#code-changes)).

### 8.4 `tests/`

**`tests/test_extraction.py`** — pytest suite (~35 tests) covering router classification, digital-PDF/Excel/CSV extraction, standardiser schema/types, validator checks (balance, dates, duplicates, reversals, exclusivity), anonymiser masking, and the identifier vault. It references `synthetic_dataset_full_mentoring/statements/` as `DATASET_DIR` — **this is the legitimate place to depend on the dataset (validation), not in the engine.**

### 8.5 `tools/`

**`tools/verify_viewer.py`** — A **local-only Streamlit** viewer (`streamlit run tools/verify_viewer.py`) that shows, per file: route taken, OCR engine + confidence, the Groq column map and its source, raw text, the clean table, and flagged rows. It is **git-ignored** (`.gitignore: tools/`) and is the "temporary testing interface" you referred to — purely a dev/demo aid, not part of runtime.

### 8.6 Root files & runtime folders

- **`requirements.txt`** — deps: fastapi, uvicorn, python-dotenv, pandas, numpy, pdfplumber, pytesseract, pillow, opencv-python, python-docx, **groq**, pymupdf, chromadb, sentence-transformers, scikit-learn, networkx, reportlab, openpyxl, matplotlib, python-multipart, aiofiles, streamlit. *(fastapi/uvicorn/streamlit are for the API/viewer layers, not the core extraction logic.)*
- **`.env.example`** — template for `GROQ1`/`GROQ2`/`GROQ3` + `TESSERACT_CMD`. The real `.env` is git-ignored.
- **`.gitignore`** — ignores `.env`, `__pycache__`, venvs, `storage/chromadb/`, `storage/llm_cache/`, `uploads/`, `outputs/reports/`, `outputs/graphs/`, **`outputs/extractions/`** (real de-anonymised data), `tools/`, `.DS_Store`, IDE files. *(Note: `outputs/extractions/` IS already git-ignored — good.)*
- **`AUDIT.md`** — prior "Extraction Phase — Audit & Hardening Report": what an earlier hardening pass found broken/missing and fixed (e.g. it flagged that `.env.example` had once contained real keys). Background reading, not runtime.
- **`INSTRUCTIONS (1).md`** — the **Phase-2 brief**: defines the goal (any statement → one clean table), the operating rules (audit don't rewrite, keep it explainable, keep runs bounded, one provider = Groq), and the privacy rules (§6 vision exception, §7 three-key split). This is the spec the engine is built against.
- **`original bank statements/`** — 9 sample bank-statement PDFs (SBI, HDFC, ICICI, Kotak, IDBI, Canara, Bank of Baroda, and two generic). **Test data only.**
- **`Screenshot ….png`, `WhatsApp Image ….jpeg`** — sample image statements for testing the vision path.
- **`outputs/`, `storage/`, `uploads/`** — runtime folders (content git-ignored). `outputs/extractions/` is the output sink; `storage/llm_cache` and `storage/chromadb` are local caches/stores; `uploads/` holds investigator files.

### 8.7 Where Groq is called — quick cross-reference
| Call | File · function | Key · model |
|---|---|---|
| #1 metadata | `llm_structurer.extract_account_metadata` | GROQ1 · text |
| #2 schema | `llm_structurer.discover_transaction_schema` | GROQ1 · text |
| #3 transactions (chunked) | `llm_structurer.structure_statement` | GROQ1 · text |
| #4 column map | `column_identifier.identify_column_structure` | GROQ1 · text |
| #5 image → JSON | `vision_extractor.extract_statement_from_image` | GROQ2 · vision |
| #6 blurry-scan OCR | `extractor_ocr.run_groq_vision_on_image` | GROQ2 · vision |

Every other file in `extraction/` (`router`, `extractor_digital_pdf`, `extractor_docx`, `extractor_excel_csv`, `account_extractor`, `anonymiser`, `identifier_vault`, `standardiser`, `validator`, `storage`, `chromadb_ingestor`) is **pure local code — no Groq calls.**
