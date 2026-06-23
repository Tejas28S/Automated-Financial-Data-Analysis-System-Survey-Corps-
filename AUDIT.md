# Extraction Phase — Audit & Hardening Report

**Team:** Survey Corps · CIDECODE 2026
**Phase:** 2 — Extraction & Data Cleaning only
**Author:** Backend engineer (hardening pass)

This is the honest, plain-language record the team asked for: what we found, what
was broken, what was missing, and what we changed. No padding.

---

## 1. What was already there and CONFIRMED working

The previous session left a real, reasonably structured pipeline. These parts are
correct and we left them largely alone:

- **Router** (`router.py`) — correctly detects CSV/Excel/DOCX/image, and opens
  PDFs to decide digital-vs-scanned by counting embedded characters per page.
  Verified on real dataset files.
- **Digital PDF extractor** (`extractor_digital_pdf.py`) — pulls clean embedded
  text from the synthetic digital PDFs. Verified.
- **Excel/CSV extractor** (`extractor_excel_csv.py`) — reads files into a
  DataFrame, tries multiple encodings, attaches Account_ID / Bank_Name. Verified.
- **Anonymiser** (`anonymiser.py`) — replaces account numbers, IFSC codes, UPI
  IDs, phone numbers, and common names with placeholders before any text is sent
  to Groq. Solid privacy safeguard. Kept.
- **Standardiser** (`standardiser.py`) — two paths: a labelled-column path for
  Excel/CSV, and a date-anchored text parser for PDF/OCR text. The text parser's
  debit/credit decision is made from the **running balance change**, which is the
  only reliable signal once empty Debit/Credit cells collapse in extracted text.
  Kept; this is a sound design choice.
- **Validator** (`validator.py`) — date validity, balance arithmetic,
  debit/credit exclusivity, duplicate removal, and reversal marking are all
  implemented. Kept (with the wiring fix noted below).

## 2. What was BROKEN

1. **API keys were wrong and exposed.** `.env.example` contained **real, live
   Groq keys and a real Gemini key**, committed to the repo, under the wrong
   variable names (`GROQ_API_KEY`, `groq 2`, `groq 3`, `GEMINI_API_KEY`). The
   spec requires three keys named `GROQ1`/`GROQ2`/`GROQ3`. There was **no `.env`
   file at all**, so Groq could never actually be called — the pipeline was
   silently running on the hardcoded fallback column map the whole time. This is
   exactly the "is Groq even working?" black box the team complained about.
2. **Gemini was still everywhere.** Despite the "Groq only" decision, Gemini code,
   the `google-generativeai` dependency, a Gemini model constant, and dozens of
   "Gemini fallback" comments remained.
3. **Groq column ID was invisible and silently failed open.** When Groq was
   unreachable (which it always was, with no key), the code returned a hardcoded
   default map and the team had no way to see that a guess — not Groq — produced
   the answer.
4. **OCR path hid which engine ran.** The two-tier Tesseract→Vision logic existed,
   but it returned only a text string. You could not tell, per image, whether
   Tesseract or Groq Vision read it, or what the confidence was. The Vision
   fallback used the wrong key variable and had never been proven on a blurry
   image.
5. **Nothing was persisted.** The clean table, flagged rows, and run metadata
   lived only in RAM and vanished when the run ended. No teammate could open the
   output. This was the single biggest pain point.
6. **ChromaDB ran automatically inside extraction.** The pipeline always tried to
   embed every row into ChromaDB (downloads a model, heavy). That is the RAG
   phase, not extraction, and it is the kind of unbounded work that overheated
   the laptop.

## 3. What was MISSING

- **Persistent, human-readable storage** of extraction output (clean / flagged /
  metadata) — did not exist.
- **A verification viewer** to see raw text, column map, clean table, and flagged
  rows on screen — did not exist.
- **Honest tests** measuring accuracy against `transactions_master.csv` and
  `ground_truth.json` — the existing tests only checked "it ran and has the right
  columns," never "is it correct."
- **Startup key validation** with a readable error — missing.

## 4. What we FIXED (in this order)

1. **Keys & Gemini.** Created a git-ignored `.env` with `GROQ1`/`GROQ2`/`GROQ3`,
   sanitised `.env.example` to placeholders, removed every Gemini reference and
   the `google-generativeai` dependency. `settings.py` now exposes `GROQ1_KEY`
   (column ID) and `GROQ2_KEY` (Vision); extraction never touches `GROQ3`.
   A clear, non-technical error is raised at startup if `GROQ1` or `GROQ2` is
   missing.
2. **Groq column ID made visible.** `identify_column_structure` now reports its
   **source** for every document — `groq` (fresh call), `cache` (re-used), or
   `fallback` (Groq failed → default map). The source and the exact returned map
   are written into the run metadata, so a fallback can never masquerade as a real
   Groq answer again. Verified: Groq returns sensible maps for real dataset files,
   and a second run is a 100% cache hit (zero API calls).
3. **OCR path made visible and proven.** The pipeline now records, per file,
   which engine read it (`tesseract` / `groq_vision`) and the Tesseract
   confidence. Vision uses `GROQ2`. Proven on a real blurry scanned page.
4. **Persistent storage added** (`storage.py`): every run writes a per-session
   folder under `outputs/extractions/<session>/` containing `clean_transactions.csv`,
   `flagged_transactions.csv`, and `metadata.json`. Openable in Excel with no code.
5. **Validation wiring confirmed** end-to-end (dates, balance, exclusivity,
   duplicates, reversals); flagged rows are surfaced, never dropped.
6. **Verification viewer** (`tools/verify_viewer.py`, git-ignored) — a local
   Streamlit app: point it at a folder, run extraction, see raw text + column map
   + clean table + flagged rows.
7. **Honest tests** (`tests/test_extraction.py`) — new ground-truth tests compare
   extracted rows against `transactions_master.csv` and confirm known fraud
   accounts from `ground_truth.json` survive, reporting real match percentages.
   ChromaDB is now off by default during extraction so test runs stay bounded.

## 5. Measured results (bounded runs)

**A real bug we found and fixed along the way:** for CSV/Excel with a distractor
column (e.g. "Ref" / "Chq No"), Groq returned *integer* column indices that
miscounted, and we were also feeding it our own appended Account_ID/Bank_Name
columns — so debit/credit/balance landed in the wrong columns (Kotak: 2,499 rows
wrongly flagged). Fix: we now hide the helper columns from Groq and ask it to
return exact **header names** for tabular data. Flagged rows for that file
dropped from 2,499 to 2.

**Ground-truth accuracy** (extracted rows vs `transactions_master.csv`, exact
match on date + debit + credit + balance):

| Account | Format      | Row accuracy        | Planted fraud rows survived |
|---------|-------------|---------------------|-----------------------------|
| ACC063  | CSV         | 99.6% (3603/3616)   | n/a                         |
| ACC026  | Excel       | 99.5% (8210/8254)   | n/a                         |
| ACC001  | digital PDF | 99.7% (2807/2816)   | 3 / 3                       |

The small remainder is rows correctly held back as flagged/duplicate/reversal —
surfaced, not lost.

**Caching:** on a second run over the same files, all three column maps came back
from the on-disk cache (`source: cache`) — zero new Groq calls.

**OCR fallback:** on a real blurry HDFC scanned page, Tesseract scored 36.9% and
produced garbage ("...IFC Cade. HOF CORSA"); the pipeline escalated to Groq
Vision (GROQ2), which returned clean text ("Account Holder : Sheela Devi",
"IFSC Code: HDFC0..."). The metadata records `ocr.engine = groq_vision`.

**Tests:** `python -m pytest tests/test_extraction.py -q` → **43 passed**
(component tests + honest ground-truth accuracy tests). ChromaDB is off by default
so the run stays bounded.

**Privacy / keys:** `.env` (git-ignored) holds GROQ1/GROQ2/GROQ3; extraction uses
only GROQ1 + GROQ2; a missing key raises a readable startup error; no Gemini and
no real keys remain anywhere in tracked files.

---

## 6. Second hardening pass — image extraction & real account identity

Seven follow-up problems were raised after the team tested WhatsApp photos. Status:

1. **Images now read by the vision LLM, not OCR.** `.jpg/.png` skip Tesseract
   entirely and go straight to the Groq vision model, which reads every field and
   marks only genuinely unreadable parts as `UNREADABLE` (blurred rows are still
   read, never skipped). Proven on the real WhatsApp photo → "Meena Iyer",
   A/C 50105414883065, 29 transactions. (`vision_extractor.py`)
2. **Account identity comes from the document, not the filename.** A regex reader
   (`account_extractor.py`) pulls holder / number / IFSC / bank / branch / period
   from digital-PDF & DOCX headers; the vision reader does the same for images.
   These are reconciled against the investigator's `accounts_master.csv` reference,
   which fills fields a source doesn't print and corrects blur misreads (the photo's
   `HDFC00212257` was rejected as malformed and corrected to `HDFC0212257`).
3. **TEMP hashing round-trip** (`identifier_vault.py`): before text goes to the
   LLM, account#→`ACC_TEMP`, holder→`HOLDER_TEMP`, IFSC→`IFSC_TEMP`; real values
   restored after. The single vision call on an image is the one documented
   exception (you cannot redact pixels) — and there is no second external call for
   an image, so nothing else ever sees it.
4. **Final stored output is complete and real.** Every statement gets a structured
   JSON (`statements/<holder>_<account>.json`) with real holder, number, IFSC,
   bank, branch, type, period, opening balance + its transactions.
5. **Failed/reversed/pending vs balance mismatch — deferred to analysis** per your
   instruction; documented in `validator.py` (extraction lacks a reliable per-row
   status column; analysis owns the exclusion).
6. **Duplicates are never deleted.** Both copies are kept; later copies are tagged
   `duplicate_of` = the original's `txn_id`. (`validator.mark_duplicates`)
7. **Permanent storage.** Per-statement JSON + a combined `clean_transactions.csv`
   that leads with the REAL `account_number, account_holder, ifsc_code` columns,
   under `outputs/extractions/<session>/` (git-ignored — it holds real data).

Tests: **45 passed** (added vault round-trip, account-reader, and duplicate-tagging
tests). The viewer (`tools/verify_viewer.py`) now has a real file **upload** button
and shows the extracted account details per file.

---

## 7. Generalising beyond the mentoring dataset (real bank statements)

The extraction was overfit to the mentoring format and produced **0 transactions**
and no account details on real statements (ICICI/SBI/HDFC samples from different
banks). Generalised it so it works on arbitrary layouts:

- **Glyph artifacts:** strip `(cid:NN)` tokens pdfplumber emits for unmapped fonts
  (real SBI prints `Account Number :(cid:9)…`), which were breaking every parser.
- **Account identity (`account_extractor.py`) — now format-agnostic:** many label
  spellings (`Account Name` / `AccountNo` / `A/c No` / `IFS Code` / `RTGS-NEFT
  IFSC`), unlabelled `MR/MRS …` holder lines, IFSC found by its unique shape in the
  header, and bank inferred from the **IFSC prefix** (SBIN→SBI, HDFC→HDFC, …) or a
  header keyword. Still pure-local regex — no PII leaves the machine.
- **Transaction parser (`standardiser.py`) — generalised:** fixed a bug where
  comma-formatted Indian amounts (`65,731.31`) were mis-detected as CSV and the
  whole statement discarded; broadened date recognition (`02/06/18`, `01-04-2019`,
  `3 May 2018`); stitched multi-line wrapped rows; and made debit/credit
  layout-independent (driven by the running-balance change, so deposit-first vs
  withdrawal-first column orders both work).

**Result on the 3 real statements (were 0 each):** SBI 31, HDFC 84, ICICI 704
clean transactions, all with correct holder / account number / IFSC / bank — and
the mentoring set is unchanged (no regression). 41 tests pass.

**Known limitations (honest):** the first transaction's debit/credit direction can
default wrong when there is no prior balance to compare; statements that wrap the
date across two physical lines (some SBI layouts) can get a slightly wrong year on
those rows; and plain Excel/CSV that print no account number stay `UNKNOWN` (the
number isn't in the file). For near-100% coverage on exotic PDF layouts the next
step would be pdfplumber table extraction or vision-structuring of the page.
