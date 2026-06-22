# INSTRUCTIONS.md — Extraction Phase Audit & Hardening

**Project:** Multi-Accused Cross-Account Investigation Engine
**Team:** Survey Corps · CIDECODE Hackathon 2026 (CID Karnataka / PES University)
**Repository:** Automated-Financial-Data-Analysis-System-Survey-Corps
**This task covers:** Phase 2 — Extraction & Data Cleaning **only**. Do not build the analysis engine, report generator, RAG chatbot, or frontend in this task.

---

## 1. WHO YOU ARE AND HOW TO WORK

You are a **senior Python backend engineer** joining a project where the extraction phase has **already been written** by a previous model. Your job is **not** to rewrite it from scratch. Your job is to **audit what exists, find what is broken or missing, decide the right fix yourself, and implement it.**

Read these operating rules carefully — they matter as much as the task itself:

1. **Read the codebase ONCE, thoroughly, before doing anything.** Do not re-read the same files repeatedly across the session. Build a complete mental model first, then act. This is a token-budget-sensitive task — wasted re-reads cost real credits. Be economical.
2. **You make the engineering decisions.** This document tells you *what outcome is required* and *why it matters*. It deliberately does **not** tell you how to structure functions, what variable names to use, or how to write the code. You are the engineer. Decide the implementation yourself, the way a senior developer would.
3. **Do not break what already works.** If a component is correct, leave it alone. Touch only what is broken, missing, or unverifiable.
4. **The code must be explainable to non-technical teachers and judges.** Every non-trivial step needs a short, plain-language comment saying *what it does and why* — not jargon. A judge from CID with no coding background should be able to follow the logic when you walk them through it.
5. **Do not run long, heavyweight, or unbounded processes.** A previous session ran a process for an entire day and overheated the laptop. Avoid that. Keep test runs small and bounded. If a step (model download, embedding, OCR over many files) is heavy, process a small sample first, confirm it works, and report — do not silently grind through the whole dataset.
6. **Ask no questions. Make all decisions yourself and proceed.** Everything you need is in this file and the repo.

---

## 2. WHAT THIS PHASE MUST ACHIEVE (THE GOAL)

Take **any** uploaded bank statement — digital PDF, scanned PDF, phone photo, Excel, CSV, or DOCX — and turn it into **one clean, standard, verifiable transaction table** that the analysis engine can trust.

The standard output schema, identical for every input format, is:

```
Date | Narration | Debit | Credit | Balance | Account_ID | Bank_Name
```

That is the entire purpose of this phase. Everything you do should serve that single goal: **any messy input → one clean, trustworthy, inspectable table.**

---

## 3. FIRST TASK — AUDIT, DON'T ASSUME (DO THIS BEFORE ANY CODE)

Before writing or changing a single line, do a complete read of the repository and produce a short written audit. The team has lost time because nobody could confirm whether the existing code actually works. Your first deliverable is **clarity**, not code.

Read the repo and determine, honestly:

- What extraction components already exist, and what each one currently does.
- Whether the existing code matches the architecture in Section 5 of this document.
- Where the existing code **deviates** from the intended design, where it is **simplified**, and where it is **missing entirely**.
- Whether anything is half-built, stubbed, or never wired into the pipeline.

Then write a brief audit summary (plain language, no code) covering:

- What works and is confirmed.
- What is broken.
- What is missing.
- What you plan to fix, in order.

This audit is for the team to read. Keep it short and honest. Do not pad it.

---

## 4. THE FOUR PROBLEMS YOU MUST SOLVE

These are the specific failures the team experienced. Solving them is the core of this task.

### Problem 1 — "Is Groq actually doing its job?"
The team cannot confirm whether the Groq LLM is genuinely identifying the column structure of each document, or whether the pipeline is silently falling back to guesses or hardcoded assumptions.

**Required outcome:** The Groq column-identification step must be **provably working and visible.** When a document is processed, it must be possible to see (a) that Groq was called or a cached answer was used, and (b) exactly what column map Groq returned for that document. If Groq fails or returns nonsense, that must be surfaced clearly, not hidden. Confirm with your own bounded test run that Groq returns a sensible column map for real documents in the dataset.

**Clarify Groq's role in your comments and audit, because the team is confused about it:** Groq does **two narrow jobs only** — (1) identify which column is Date/Narration/Debit/Credit/Balance from the first ~30–40 lines of a document, returned as a small JSON map, called once per document and cached; and (2) later (in a different phase, not this one) write explanations. Groq does **not** convert every transaction row into JSON. Groq does **not** see all the rows. The row-by-row conversion is done by deterministic pandas code. Make this separation obvious in how the code reads.

### Problem 2 — "OCR fails on blurry images; does the Groq Vision fallback work?"
Tesseract alone is not good enough for blurry phone photos of statements — the team tested this and confirmed it. The design decision is: **keep Tesseract for clean images, and fall back to Groq Vision for low-confidence ones.**

**Required outcome:** The OCR path must work as a two-tier system. Run Tesseract first and read its confidence. If confidence is **below 80%**, fall back to **Groq Vision** (`meta-llama/llama-4-scout-17b-16e-instruct`) to read the image directly. The handoff must be based on the confidence score the Tesseract pipeline already produces — not guesswork. It must be visible, for any given image, which path was taken (Tesseract or Groq Vision) and what text came out. Confirm the Groq Vision fallback actually returns readable transaction text on a blurry sample — this is the part the team trusts least, so prove it works.

**Note:** Groq Vision replaces the earlier Gemini idea. There should be **no Gemini** anywhere — one provider (Groq) for everything. If Gemini code or keys still exist from the previous session, remove them.

### Problem 3 — "Where is the extracted data? We can only see it in RAM, and we can't see it at all."
This is the biggest pain point. The clean DataFrame currently lives only in memory and vanishes when the run ends. Nobody on the team can open it, inspect it, or confirm the extraction was correct.

**Required outcome — and YOU decide the storage design:** Decide, like a senior engineer, the right way to persist the extraction output so it is **inspectable and survives the run**, while respecting the privacy rules in Section 6 and the hackathon's local-only constraint. At minimum, after every run, the following must be written to disk in a clearly organised, per-session location:
- the clean standardised transactions,
- the rows that failed validation ("flagged for manual review"),
- a small metadata record (which files were processed, counts, timestamps, which OCR path each file took, which column map Groq returned).

Choose the formats and folder layout yourself and **document your choice in a clear comment explaining what you stored, where, and why.** The guiding principle: a teammate must be able to open the output and read the extracted transactions without running any code. Nothing leaves the local machine.

### Problem 4 — "We cannot verify the output, and we don't trust the testing."
The team is not satisfied with the existing tests and cannot confirm correctness. Verification must become trivial and honest.

**Required outcome — two parts:**

**(a) A simple local verification viewer.** Build a minimal local-only interface (your choice of approach) where someone can point at a folder of statements, run the extraction, and **see on screen**: the raw extracted text, the column map Groq returned, the final clean table, and the flagged rows. This is a dummy internal tool for the team and the demo — **it must never be committed to GitHub** (add it to `.gitignore`). Its only job is to make the extraction visible so the team and the judges can trust it.

**(b) Honest testing against ground truth.** The dataset (see Section 7) ships with `transactions_master.csv` (the correct answer) and `ground_truth.json` (the planted fraud accounts). Tests must compare the extracted output **against this ground truth** and report real accuracy numbers — how many rows matched, how many were flagged, whether the known accounts all appear. A test that only checks "the code ran without crashing" is not acceptable. Tests must say *how correct* the output is, not just *that it produced output*. Keep test runs small and bounded so the laptop does not overheat.

---

## 5. ARCHITECTURE REFERENCE (LOCKED DECISIONS)

This is the intended design. Where the existing code disagrees with this, the code is wrong — fix it. This describes *what each stage is responsible for*, not how to implement it.

```
Uploaded file
   │
   ▼
[ Component 1 — Router ]  decide file type by extension + MIME
   ├─ Excel / CSV ───────────────► Component 4
   ├─ DOCX ──► extract text ──────► Component 3
   ├─ Digital PDF ────────────────► Component 2A
   └─ Scanned PDF / image ────────► Component 2B
                                         │
[ Component 2A — Digital PDF ]  pull embedded text → Component 3
                                         │
[ Component 2B — OCR ]  Tesseract first; read confidence
        confidence ≥ 80%  → use Tesseract text
        confidence < 80%  → Groq Vision reads the image
        → clean text → Component 3
                                         │
[ Component 3 — Column ID (Groq) ]
        anonymise the sample FIRST (Section 6)
        send ~30–40 lines → Groq returns column-map JSON
        called once per document, response cached on disk
                                         │
[ Component 4 — Standardise (pandas) ]
        parse every row using the column map
        normalise dates to one format
        strip commas / symbols from amounts
        attach Account_ID and Bank_Name (from accounts_master.csv)
                                         │
[ Component 5 — Validate & Clean (pandas) ]
        run the cleaning checks (Section 5.1)
        clean rows → kept; failing rows → flagged list (never dropped silently)
                                         │
[ Storage (you design) ]  persist clean + flagged + metadata to disk
                                         │
                          ▼
            clean unified table, ready for analysis
```

Locked technical choices:
- **Column ID + (later) explanations:** Groq `llama-3.3-70b-versatile`.
- **Blurry-image OCR fallback:** Groq Vision `meta-llama/llama-4-scout-17b-16e-instruct`.
- **OCR fallback trigger:** Tesseract confidence **< 80%**.
- **Account_ID & Bank_Name:** read from `accounts_master.csv`, not typed manually, not guessed by the LLM.
- **LLM calls scale with number of documents, never number of rows.** Cache every Groq column-map response on disk keyed to the document, so re-running tests costs zero new API calls.
- **No Gemini. One provider only (Groq).**
- **Everything local.** Nothing is stored in any cloud database.

### 5.1 Data cleaning — what "clean" must mean
Data cleaning is as important as extraction for this project. The validation/cleaning stage must, at minimum, enforce:

- **Date validity** — every Date must parse as a real date; a misread like a number in the date column fails the row.
- **Balance arithmetic** — previous balance ± debit/credit should equal the current balance; a mismatch flags the row as a probable misread.
- **Debit/Credit exclusivity** — exactly one of Debit or Credit should hold a value per row; both populated signals column misalignment.
- **Duplicate removal** — identical repeated transactions must be detected and removed (this is an explicit hackathon requirement).
- **Failed / reversed transactions** — detect transactions that are debited and then credited back (a reversal that nets to zero). These must be identified and handled, not treated as two genuine transactions (explicit hackathon requirement).

Rows that fail any structural check go to the **flagged-for-manual-review** list and are shown to the investigator — **never silently dropped or guessed.** Surfacing uncertainty honestly is part of what makes this tool defensible in court; treat it as a feature, not a weakness.

### 5.2 Fields the extractor must be smart enough to read
From the CID mentoring meeting, the system must reliably read and associate, per statement: **bank name, account holder, account number, branch, bank code / IFSC, transaction date, narration / particulars, debit, credit, balance.** The standard 7-column schema is the minimum; where the source provides account-identifying fields (account number, branch, IFSC), the extraction must capture them and attach them correctly so the analysis phase can link accounts. Do not lose this identifying information.

---

## 6. PRIVACY RULES (NON-NEGOTIABLE — FROM THE CID MEETING)

The CID mentors were explicit: **raw bank data must not be sent outside the system.** The investigators' real concern is that account numbers and personal data could leak to an external LLM API. Honour this in the design:

- **Anonymise before any Groq text call.** Before the ~30–40 line sample goes to Groq for column identification, replace account numbers and personal names with placeholders. Groq only needs to see the *shape* of the data (which column is which), never the real identities. Keep the real-value mapping on the local machine only.
- **Vision is the one unavoidable exception:** when an image must be read, Groq Vision has to see the image. Limit this to the low-confidence fallback cases only — clean images stay fully local via Tesseract and never leave the machine.
- **Everything else stays local** — the clean table, the stored files, the cache, the viewer. Nothing is written to any cloud service.
- **Keys live only in a local `.env`** that is git-ignored. Never hardcode keys in source. Never commit them. If the previous session left keys in any file, remove them.

When you write comments, make the privacy design visible — a judge should be able to see exactly what leaves the machine (anonymised column samples; low-confidence images only) and what never does (everything else).

---

## 7. API KEY STRATEGY — THREE GROQ KEYS, ONE PROVIDER

The team is using **three separate Groq API keys** assigned to three different team members. This is a deliberate rate-limit management decision: if all LLM calls share one key and everything runs at once during the demo, you risk hitting Groq's free-tier limits mid-presentation. Splitting by phase keeps each key's quota for its own job.

The split is:

| Key name in `.env` | Phase it serves | What it calls | Why separate |
|---|---|---|---|
| `GROQ1` | Extraction — column identification | `llama-3.3-70b-versatile` (text) | Lightest key; cached after first run, near-zero calls on reruns |
| `GROQ2` | Extraction — blurry image OCR fallback | `llama-4-scout-17b-16e-instruct` (Vision) | Vision model has its own rate limits; must not block text calls |
| `GROQ3` | Analysis phase + Report generation | `llama-3.3-70b-versatile` (text) | Heaviest key; fires in a burst during demo for thresholds, explanations, and executive summary |

**How to read the keys in code:**

The `.env` file (git-ignored, never committed) contains exactly these three variable names:

```
GROQ1=gsk_your_first_key_here
GROQ2=gsk_your_second_key_here
GROQ3=gsk_your_third_key_here
```

In `config/settings.py` (or equivalent), load them like this — and make sure the right module uses the right key:

- The column-identification module (`column_identifier.py` or equivalent) reads `GROQ1`.
- The OCR Vision fallback module reads `GROQ2`.
- The analysis engine and report generator (built in a later phase, not this task) will read `GROQ3`.

**For this extraction task specifically, you will use `GROQ1` and `GROQ2` only.** Do not reference `GROQ3` in any extraction-phase code — leave it for the analysis phase.

**If a key is missing from `.env`:** the module that needs it must raise a clear, readable error immediately at startup — something a non-technical teammate can understand — rather than crashing silently mid-run. Example: `"GROQ1 key not found in .env — add it before running extraction."` 

Add a comment in `config/settings.py` explaining why there are three keys and which phase each one serves. This is something judges will ask about and the team should be able to explain confidently.

---

## 8. THE DATASET (USE THIS FOR ALL TESTING)  

The real dataset folder is **`synthetic_dataset_full_mentoring/`**. It is organised **by file format**, not by case. Expect roughly this structure — confirm the exact layout when you audit the repo:

```
synthetic_dataset_full_mentoring/
├── statements/
│   ├── csv/
│   ├── digital_pdf/
│   ├── excel/
│   └── scanned_pdf/
├── accounts_master.csv        ← source of Account_ID and Bank_Name (and account-identifying fields)
├── transactions_master.csv    ← GROUND TRUTH: the correct extracted transactions
└── ground_truth.json          ← the deliberately planted fraud accounts/patterns
```

Use it like this:
- **`accounts_master.csv`** supplies Account_ID and Bank_Name (and any account/branch/IFSC identifiers) — the extractor reads these, it does not ask a human to type them.
- **`transactions_master.csv`** is the answer key — tests compare extracted output against it to measure real accuracy.
- **`ground_truth.json`** lists the known fraud accounts — tests confirm those accounts survive extraction and appear in the clean output.
- Test **each of the four format folders** (csv, excel, digital_pdf, scanned_pdf) and prove the unified output is consistent across all of them.

Keep every test run **small and bounded.** Validate on a few files per format first and report results before attempting the whole set.

---

## 9. WHAT "DONE" LOOKS LIKE (ACCEPTANCE CRITERIA)

You are finished only when all of the following are true and you have demonstrated each with a small, bounded run:

1. A clear written audit exists describing what was broken/missing and what you fixed.
2. Groq column identification is confirmed working and its returned column map is visible per document; caching works so reruns make no new API calls.
3. The OCR path works two-tier: Tesseract for clean images, Groq Vision fallback below 80% confidence; the chosen path and output text are visible per image; the Vision fallback is proven to read a blurry sample.
4. No Gemini code or keys remain anywhere.
5. The extracted clean table, flagged rows, and run metadata are **persisted to disk** in a layout you designed and documented; a teammate can open and read them with no code.
6. The local verification viewer works, shows raw text + column map + clean table + flagged rows, and is git-ignored (never pushed).
7. Data cleaning enforces date validity, balance arithmetic, debit/credit exclusivity, duplicate removal, and failed/reversed-transaction handling; flagged rows are surfaced, never dropped silently.
8. Tests compare extracted output against `transactions_master.csv` and `ground_truth.json` and report real accuracy numbers, not just "it ran."
9. Privacy rules hold: anonymisation before Groq text calls, images only via the low-confidence fallback, keys only in a git-ignored `.env`, nothing in the cloud. The three keys (`GROQ1`, `GROQ2`, `GROQ3`) are correctly wired — extraction uses only `GROQ1` and `GROQ2`; a clear error is raised at startup if either is missing.
10. The code carries plain-language comments a non-technical judge can follow.

---

## 10. WHAT NOT TO DO

- Do **not** rewrite working components from scratch.
- Do **not** build the analysis engine, reports, chatbot, or frontend in this task.
- Do **not** run unbounded, day-long, or laptop-overheating processes — keep runs small and bounded.
- Do **not** send raw account numbers or names to any LLM; anonymise first.
- Do **not** commit the verification viewer, the `.env`, the cached responses, or any stored case data to GitHub.
- Do **not** silently drop rows that fail validation — flag them.
- Do **not** ask the user questions. Decide and proceed.

---

## 11. SUGGESTED ORDER OF WORK

1. Audit the whole repo once; write the honest summary (Section 3).
2. Fix and **verify** Groq column identification (Problem 1).
3. Fix and **verify** the Tesseract → Groq Vision OCR path (Problem 2).
4. Design and implement **visible, persistent storage** (Problem 3).
5. Complete the **data cleaning** checks, including duplicates and reversals (Section 5.1).
6. Build the **local verification viewer** (Problem 4a).
7. Write **ground-truth tests** and report real accuracy (Problem 4b).
8. Do one small end-to-end bounded run, confirm every acceptance criterion, and report results.

Begin with the audit. Make your decisions like a senior engineer and proceed without asking questions.
