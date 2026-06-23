# CHANGES_INSTRUCTIONS.md — Extraction Phase Re-Architecture

**Project:** Multi-Accused Cross-Account Investigation Engine
**Team:** Survey Corps · CIDECODE Hackathon 2026 (CID Karnataka / PES University)
**Repository:** Automated-Financial-Data-Analysis-System-Survey-Corps
**Scope of this task:** Phase 2 — Extraction & Data Cleaning **only**. Do **not** build or touch the analysis engine, report generator, RAG chatbot, or frontend.
**Status of the engine:** Already written by a previous model. It works, but it has drifted away from the intended design. This document tells you precisely what to change and why.

> Read this whole document once before writing anything. It is long on purpose — it contains the *why*, the *what*, worked examples, exact decision thresholds, and acceptance tests. The architecture here is a deliberate team decision, not a suggestion.

---

## Table of contents

1. [Why this document exists](#1)
2. [How you must work (operating rules)](#2)
3. [The goal in one sentence](#3)
4. [The core philosophy — Validation-Arbitrated Tiered Hybrid](#4)
5. [The Anti-Overfitting Law (non-negotiable)](#5)
6. [Audit of the current code — what is wrong and must change](#6)
7. [The target architecture (the tiers, in full)](#7)
8. [The Validator — the heart of the system (full spec + worked examples)](#8)
9. [The deterministic schema-driven parsing engine](#9)
10. [The escalation ladder (exact rules and thresholds)](#10)
11. [The LLM interface — provider-independent, local-model-ready](#11)
12. [Sampling strategy for schema discovery](#12)
13. [Metadata extraction (account details → JSON)](#13)
14. [The image / vision path](#14)
15. [Output format and storage](#15)
16. [Code to delete or consolidate](#16)
17. [Privacy decision](#17)
18. [Testing and the blind-set acceptance criteria](#18)
19. [Step-by-step build order](#19)
20. [What NOT to do](#20)

---

<a name="1"></a>
## 1. Why this document exists

The extraction engine currently contains **two contradictory philosophies bolted together**:

- For Excel/CSV and the "happy path" of digital PDFs, it follows the intended design: **the LLM only learns the layout from a small sample, and deterministic code parses every row.**
- For scanned PDFs, DOCX, and the digital-PDF fallback, it does the **opposite**: **the LLM reads every single transaction row, in 60-line chunks.**

This fork is the root cause of every problem the team has reported: slow processing, high API/token usage, the "fix bank A → break bank B" cycle, and the fear of overfitting. It also introduced a **silent data-loss bug** (a hard cap of 40 chunks ≈ 2,400 lines, after which transactions are dropped with no warning), and a **privacy inconsistency** (raw holder names and narrations are sent to the LLM on the text path, although a CID rule says to anonymise first).

Your job is to **converge the pipeline onto one coherent philosophy** — the Validation-Arbitrated Tiered Hybrid described in §4 and §7 — and to make accuracy provable on bank formats the code has never seen.

---

<a name="2"></a>
## 2. How you must work (operating rules)

1. **Read the codebase once, thoroughly, before changing anything.** Build a complete mental model first. This is a token-sensitive task; do not re-read the same files repeatedly.
2. **You make the engineering decisions.** This document tells you the required *outcome* and the *architecture*. It does not dictate every function name. Implement it the way a senior engineer would.
3. **Do not break what already works.** Excel/CSV extraction, the OCR two-tier engine, the storage layer, the validator's existing checks, the caching — keep them. Touch only what this document says to change.
4. **Every non-trivial step needs a plain-language comment** a non-technical CID judge could follow when walked through it.
5. **Keep all test runs small and bounded.** Never run an unbounded, hours-long, laptop-overheating process. Process a few files per format, confirm, report.
6. **Make all decisions yourself and proceed.** Do not ask questions mid-task.
7. **Preserve provider-independence.** All LLM access must go through one thin interface (§11). No `groq`-specific calls scattered through the pipeline.

---

<a name="3"></a>
## 3. The goal in one sentence

> Take **any** uploaded bank statement — digital PDF, scanned PDF, phone photo, Excel, CSV, or DOCX — from **any** bank, including banks never seen during development, and turn it into **one clean, standard, verifiable transaction table** that the analysis engine can trust in court.

The standard output schema, identical for every input, is:

```
Date | Time | Narration | Debit | Credit | Balance | Account_ID | Bank_Name | IFSC_Code
```

(`Time` and `IFSC_Code` are kept because real statements carry them and the analysis phase needs them. They are blank/`UNKNOWN` when the source does not provide them — never invented.)

---

<a name="4"></a>
## 4. The core philosophy — Validation-Arbitrated Tiered Hybrid

There are two naive options, both wrong for this project:

- **Full deterministic (no LLM):** fast and cheap, but silently fails on unseen layouts → wrong numbers in a court report. Also the overfitting trap.
- **Full LLM (reads every row):** generalises, but slow, expensive, provider-locked, non-reproducible, and makes it look like the AI did everything.

We choose a **hybrid where an objective validator — not the file type, not the bank name — decides, per statement, which path was good enough.** Three principles define it:

### Principle 1 — The LLM fills parameters; humans write the engine.
The LLM **never writes a parser or regex per statement.** It returns a small structured *schema* (which column is what, date format, debit/credit method, whether narrations wrap, etc.). A single, hand-written, well-tested deterministic engine *consumes* those parameters. The engine is stable across all banks; only the parameters change per statement.

- ❌ Wrong: "LLM generates a custom parser for HDFC." (new untested code every run — fragile, unauditable, overfitting in disguise)
- ✅ Right: "LLM tells our one engine: this statement uses layout X." (same trusted engine, just configured)

### Principle 2 — The balance math is the referee.
Every bank statement prints a **running balance** (the money left after each transaction). That gives a universal self-check:

```
balance(this row) == balance(previous row) + credit(this row) − debit(this row)
```

This is arithmetic, true for every bank on earth, so it never needs to know which bank produced the statement. If our column guesses or direction guesses are wrong, the chain breaks. The validator uses this to grade any parse — deterministic or LLM — and to decide whether to escalate. **The validator is provider-independent and is the part that makes the output court-defensible.**

### Principle 3 — Spend intelligence only where cheap methods provably fail.
Cheap deterministic parsing handles the easy majority. The LLM is an **insurance policy that activates only when validation fails** — and even then it is given only a small sample (for schema) or only the specific failing rows (for repair), never the whole 5,000-row document.

The result: tokens scale with **documents + a small tail of hard rows**, never with the number of transactions. A 5,000-row statement typically costs **0–3 LLM calls**.

---

<a name="5"></a>
## 5. The Anti-Overfitting Law (non-negotiable)

The mentoring/synthetic datasets are for **development only**. The system must work on statements it has never seen. Enforce these rules:

1. **No code path may branch on a specific bank's identity.** There must be zero logic of the form `if bank == "HDFC"`, `if "Kotak" in ...`, "IDBI serial-number hack", "Kotak (Dr) suffix special case", etc. Bank-specific behaviour must be expressed only as **general, parameterised patterns** that the schema selects.
2. **Add a build/test guard** that greps the `extraction/` source for hard-coded bank names used in control flow and **fails** if any are found in code paths (comments that merely give examples are allowed, but prefer to remove even those to avoid confusion).
3. **Parsing parameters come from the schema (LLM or header), never hard-coded per bank.**
4. **Escalation is decided by measured correctness** (the validator), never by file type or bank.
5. **Sampling for schema discovery is content-based** (longest lines, failing rows, spread across the document), never bank-based.
6. **Prefer `UNKNOWN` or a flagged row over a guess.** Never invent an account number, IFSC, or direction.
7. **Correctness must be proven on a blind set** of statements not used during development (§18).

---

<a name="6"></a>
## 6. Audit of the current code — what is wrong and must change

| # | Problem | Where | Required change |
|---|---|---|---|
| A | **Two contradictory philosophies.** Scanned/DOCX/digital-fallback use `structure_statement()` which sends every row to the LLM in chunks. | `llm_structurer.py`, `extraction_pipeline.py` | Replace "LLM reads every row" with the tiered hybrid (§7). The LLM full-row reader is retained ONLY as the last-resort row-repair tool, scoped to failing rows. |
| B | **Silent truncation / data loss.** `MAX_CHUNKS = 40` (≈2,400 lines) — longer statements lose rows with no warning. | `llm_structurer.py` | Eliminate the silent cap from the main path. If any bound is needed for heat/quota, the un-processed tail must be **flagged loudly**, never dropped. |
| C | **The schema is decorative.** `discover_transaction_schema()` returns a rich schema, but the deterministic parser only reads two booleans (`has_reference_number`, `has_cheque_number`). | `standardiser.py` | The deterministic engine must be genuinely **driven by the full schema** (date format, column order, dr/cr method, wrap flag, balance position). |
| D | **Brittle digital-PDF parser** assumes date-at-line-start, balance=last-token, 2-decimal amounts; fails silently on unseen layouts. | `standardiser.py` (`_parse_single_transaction_line`) | Keep it as the cheap Tier-2 attempt, but gate acceptance on the **validator** (§8/§10), and make its assumptions schema-driven, not hard-coded. |
| E | **Dead / duplicate identity system.** `account_extractor.extract_account_details_from_text()` (with `BANK_KEYWORDS`, `IFSC_PREFIX_TO_BANK`) is **not called by the pipeline** — the pipeline uses the LLM metadata call instead. Two identity systems; one is dead. | `account_extractor.py`, `extraction_pipeline.py` | Pick **one** metadata strategy (§13). Recommended: code-first regex (`extract_account_details_from_text`) with an LLM fallback. Delete or wire whichever you do not keep — no dead code. |
| F | **Privacy inconsistency.** Excel/CSV path anonymises before the LLM; the text path sends raw names/narrations. | `llm_structurer.py` vs `column_identifier.py` | Make the privacy behaviour **consistent and documented** (§17). |
| G | **Crash-on-empty risk for missing balance column.** Validator assumes a balance exists. | `validator.py` | Add the no-balance-column fallback path (§8). |

---

<a name="7"></a>
## 7. The target architecture (the tiers, in full)

Every file flows through the same ladder. The validator decides when to stop.

```
Uploaded file
   │
   ▼
[ Tier 0 — Router + Raw Extract ]            (code, no LLM)
   ├─ digital PDF  → embedded text
   ├─ scanned PDF  → OCR text (Tesseract; Vision fallback < 80% confidence)
   ├─ image        → Vision (pixels can't be parsed deterministically)
   ├─ DOCX         → text
   └─ Excel / CSV  → table (DataFrame)
   │
   ▼
[ Tier 1 — Metadata Extraction ]             (code-first regex; LLM only if regex empty)
   account holder, number, IFSC, branch, period, opening/closing balance → stored in JSON
   │
   ▼
[ Tier 2 — Cheap Deterministic Parse ]       (code, no LLM)
   • Excel/CSV: match column headers by keyword.
   • Text: shape+position parse using DEFAULT schema, OR the discovered schema if present.
   │
   ▼
[ Tier 3 — VALIDATE ]                         (code, no LLM — the referee, §8)
   balance reconciliation + debit/credit exclusivity + date validity + completeness
   │
   ├─ PASS (e.g. ≥ 98% rows reconcile AND completeness OK) ─────────► accept; flag the few failures
   │
   └─ FAIL ▼
[ Tier 4 — Schema Discovery (LLM on a SAMPLE) ]   (1 LLM call, §11/§12)
   LLM returns a structured schema → re-parse ALL rows deterministically with it → VALIDATE again
   │
   ├─ PASS ─────────► accept; flag the few failures
   │
   └─ still failing on specific rows ▼
[ Tier 5 — Row Repair (LLM on FAILING ROWS only) ]  (1 small LLM call, §11)
   send only the unreconciled rows → re-validate
   │
   ▼
[ Remaining failures → FLAGGED for manual review ]   (never dropped, never guessed)
   │
   ▼
[ Normalise · dedupe · reversal-detect · stamp identity · store ]   (code)
   │
   ▼
   ONE clean unified table  +  flagged table  +  per-run metadata receipt
```

**Worked cost example — Bank A, digital PDF, 5,000 transactions:**
- Tier 1 metadata: 0 LLM (regex succeeds) or 1 LLM (weird header).
- Tier 2 cheap parse: 0 LLM.
- Tier 3 validate: 4,800/5,000 reconcile → FAIL (below 98%).
- Tier 4 schema discovery: **1 LLM call** on a smart sample → re-parse → 4,990 reconcile.
- Tier 5 row repair: **1 LLM call** on the 10 failures → 8 fixed.
- 2 rows flagged.
- **Total: ~2–3 LLM calls for 5,000 rows.**

**Excel, clean headers:** Tier 2 matches headers → Tier 3 passes → **0 LLM calls.**

**Image (phone photo):** Tier 0 vision reads the picture (unavoidable — pixels) → Tier 3 still validates the result → failures flagged.

---

<a name="8"></a>
## 8. The Validator — the heart of the system (full spec + worked examples)

Build this **first and make it airtight.** It is deterministic, provider-independent, and is what every other tier leans on. It must return, for any candidate parsed DataFrame, a structured result:

```
{
  "reconciled_rows": int,
  "total_rows": int,
  "reconciliation_rate": float,        # reconciled / total (rows with a usable balance)
  "exclusivity_ok_rows": int,
  "date_valid_rows": int,
  "completeness_ratio": float,         # parsed_rows / transaction_like_lines
  "ordering": "oldest_first" | "newest_first" | "unknown",
  "has_balance_column": bool,
  "failing_row_indices": [int, ...],   # rows to escalate / flag
  "verdict": "PASS" | "FAIL"
}
```

### 8.1 The four checks

1. **Balance reconciliation (primary).** For each row after the anchor: `abs(balance_prev + credit − debit − balance_curr) <= BALANCE_TOLERANCE` (use the existing `BALANCE_TOLERANCE = 1.0`). Count how many rows pass. A row that breaks the chain is a `failing_row_index`.
2. **Debit/Credit exclusivity.** Exactly one of debit/credit is non-zero per row. Both non-zero ⇒ column misalignment ⇒ failing row.
3. **Date validity.** Every Date parses to a real date in a sane range (e.g. year 2000–2035). A non-date in the date slot ⇒ failing row.
4. **Completeness.** `parsed_rows / transaction_like_lines` (reuse `count_transaction_like_lines`). A large shortfall (e.g. < 0.9) ⇒ under-extraction ⇒ the whole parse is suspect even if the few parsed rows reconcile.

### 8.2 Worked example — a CORRECT parse (chain holds)

| Row | Debit | Credit | Balance | Check |
|---|---|---|---|---|
| 1 | | 5,000 | 25,300 | anchor |
| 2 | 12,000 | | 13,300 | 25,300 − 12,000 = 13,300 ✓ |
| 3 | | 40,000 | 53,300 | 13,300 + 40,000 = 53,300 ✓ |

`reconciliation_rate = 1.0` → PASS.

### 8.3 Worked example — a WRONG column guess (chain breaks)

Parser mistakenly used the cheque-number column as "Balance":

| Row | Debit | Credit | "Balance" | Check |
|---|---|---|---|---|
| 1 | | 5,000 | 4471 | anchor |
| 2 | 12,000 | | 8832 | 4471 − 12,000 = −7,529 ≠ 8832 ✗ |

`reconciliation_rate ≈ 0` → FAIL → escalate to Tier 4. **This is how the validator detects a bad parse with no answer key.**

### 8.4 Tricky case — NEWEST-FIRST statements

Some banks list the most recent transaction first, so the balance chain runs **upward**, not downward. The validator must **try both directions** and keep the better one:

- Compute `reconciliation_rate` parsing top→bottom (assume oldest-first).
- Compute it bottom→top (assume newest-first).
- Set `ordering` to whichever direction reconciles better, and report that rate.
- If the statement is newest-first, the downstream table must be **re-sorted to oldest-first chronological order** before storage, so the analysis phase always sees time moving forward.

Example (newest-first):

| Row as printed | Debit | Credit | Balance |
|---|---|---|---|
| 1 (latest) | | 40,000 | 53,300 |
| 2 | 12,000 | | 13,300 |
| 3 (earliest) | | 5,000 | 25,300 |

Top→bottom: 53,300 + 40,000? No. Bottom→top: 25,300 − 12,000 = 13,300 ✓, 13,300 + 40,000 = 53,300 ✓ → newest-first detected.

### 8.5 Tricky case — NO opening balance printed (first row has nothing to chain from)

The anchor problem: row 1's balance is printed, but there is no "previous" balance to test it against.

- **Preferred:** read the explicit opening balance from the header/text ("Opening Balance", "B/F", "Balance as on …") and use it as the anchor; then row 1 itself is validated.
- **If absent:** treat row 1's *printed balance* as the anchor and validate from **row 2 onward**. Row 1's direction is then inferred from the chain (row 2 tells you whether row 1's amount went in or out) or from its narration keyword. Do **not** drop row 1.
- Either way, `reconciliation_rate` is computed over the rows that *have* an anchor; do not penalise the unavoidable first-row gap.

### 8.6 Tricky case — NO balance column at all

Some Excel exports and minimal statements have only Date/Narration/Debit/Credit and no running balance.

- `has_balance_column = false`. Balance reconciliation cannot run.
- Fall back to the weaker checks: exclusivity + date validity + completeness.
- Mark the statement's confidence as **lower** and **escalate to Tier 4 more readily** (because the strongest check is unavailable). Surface this in the metadata receipt so the team knows which statements lacked the strongest verification.

### 8.7 Tricky case — MULTI-LINE wrapped narration

A long narration wraps onto continuation lines. If the parser splits one transaction into two, the chain breaks exactly at those rows. Those rows become `failing_row_indices`. After Tier 4 schema discovery sets `narration_wraps = true`, the engine stitches continuation lines and re-validates. This is the most common reason the cheap Tier-2 parse fails on real PDFs — handle it explicitly.

---

<a name="9"></a>
## 9. The deterministic schema-driven parsing engine

One engine, parameterised by a **schema object**. No bank names anywhere.

The schema (returned by the header for Excel, or by the LLM for hard text) must include at least:

```
{
  "source_kind": "text" | "table",
  "date_format": "DD/MM/YYYY" | "DD-MMM-YYYY" | ... ,
  "has_time": bool, "time_format": "",
  "column_order": ["date", "narration", "reference", "debit", "credit", "balance"],
  "debit_credit_method": "two_columns" | "single_amount_balance_inferred" | "drcr_marker",
  "balance_position": "last_money_token" | "named_column" | "none",
  "narration_wraps": bool,
  "has_reference_number": bool,
  "has_cheque_number": bool,
  "ordering_hint": "oldest_first" | "newest_first" | "unknown"
}
```

The engine must:
- For **table** sources (Excel/CSV): rename columns per `column_order`/header match; coerce types; done.
- For **text** sources: locate the date by `date_format`, isolate the money tokens, assign amount/balance per `balance_position` and `debit_credit_method`, stitch continuation lines if `narration_wraps`, and produce the standard columns.
- **Always** run `_correct_direction_by_balance` (keep this — it is a genuine accuracy win): after a first assignment, recompute debit vs credit from the running-balance delta, except where the bank marked direction explicitly (Dr/Cr) — keep that "locked" behaviour.
- Re-sort to oldest-first chronological order before returning if `ordering_hint`/validator says newest-first.

**Example — a `single_amount_balance_inferred` line:**
```
02/04/2023  NEFT RENT APR REF883201  12,000.00  13,300.00
```
- `date_format = DD/MM/YYYY` → Date = 02/04/2023
- money tokens at end = [12,000.00, 13,300.00]; `balance_position = last_money_token` → Balance = 13,300.00, amount = 12,000.00
- `REF883201` has letters → not money → stays in narration; `has_reference_number = true` → extract REF883201 into Reference_Number
- direction: balance went 25,300 → 13,300 (down) → **Debit** 12,000.00

---

<a name="10"></a>
## 10. The escalation ladder (exact rules and thresholds)

Put every threshold in `config/settings.py` as a named constant so they are tunable and visible.

```
ACCEPT_RECONCILE_RATE   = 0.98   # parse is accepted if this fraction of balance-bearing rows reconcile
MIN_COMPLETENESS_RATIO  = 0.90   # parsed_rows / transaction_like_lines must be at least this
BALANCE_TOLERANCE       = 1.00   # rupee tolerance per row (already exists)
```

Decision logic per statement:

1. Run Tier 2 cheap parse → Tier 3 validate.
2. **Accept (no LLM for transactions) if:** `verdict == PASS`, i.e. `has_balance_column` and `reconciliation_rate >= ACCEPT_RECONCILE_RATE` and `completeness_ratio >= MIN_COMPLETENESS_RATIO`. Flag the few non-reconciling rows.
3. **Else → Tier 4:** schema discovery (LLM on a sample), re-parse, re-validate. Accept on the same criteria.
4. **Else → Tier 5:** send only `failing_row_indices` to the LLM row-repair; re-validate those rows.
5. **Remaining failures → flagged.** Never dropped.
6. **No-balance-column statements:** since the primary check is unavailable, require exclusivity + completeness to pass; escalate to Tier 4 if exclusivity is poor; mark lower confidence.

The metadata receipt must record, per file: which tier it stopped at, the reconciliation rate at each tier, the LLM call count, and the final flagged count. This is the team's proof of correctness.

---

<a name="11"></a>
## 11. The LLM interface — provider-independent, local-model-ready

All LLM access goes through **one module** exposing exactly these functions. The rest of the pipeline must not import `groq` directly.

```
discover_schema(sample_text, hints) -> schema_dict          # Tier 4
repair_rows(failing_rows_text, schema) -> [row_dict, ...]   # Tier 5
extract_metadata_llm(header_text) -> metadata_dict          # Tier 1 fallback only
read_image(image_bytes) -> {account_details, transactions}  # vision, Tier 0 for images
```

Rules:
- The provider (Groq today) lives behind this module only. Swapping to a **local model** later (e.g. a small instruct model serving JSON) must require changing **only this one module** — no pipeline edits. State this in a comment so judges see the migration path.
- Keep the existing three-key strategy (`GROQ1` text, `GROQ2` vision, `GROQ3` reserved for analysis). Extraction uses `GROQ1`/`GROQ2` only.
- Keep **disk caching** keyed on input hash, so re-runs cost zero calls. Never cache an empty/failed response.
- All calls use `temperature = 0` and forced JSON output.

---

<a name="12"></a>
## 12. Sampling strategy for schema discovery

The sample sent to `discover_schema` must be **content-based and representative**, never "the first N rows" (the first rows are the least representative — opening balance, simple entries):

- Take a handful of transaction-like lines from the **start, middle, and end** of the document.
- Always include the **longest lines** (most likely to contain wrapped narrations / reference numbers).
- Always include the specific lines the Tier-2 parse **failed** to reconcile (they carry the structural variation that broke the cheap parse).
- Cap the sample at a small, bounded size (e.g. 20–40 lines) so the call is cheap and fast.

---

<a name="13"></a>
## 13. Metadata extraction (account details → JSON)

This runs **before** transaction parsing and is independent of it.

- **Strategy: code-first.** Use `account_extractor.extract_account_details_from_text()` (regex, fully local, no data leaves the machine) for digital PDF / DOCX / scanned text. It already handles many label spellings, IFSC-by-shape, bank-by-IFSC-prefix and bank-by-keyword. **Wire it into the pipeline** (today it is dead — the pipeline calls the LLM metadata path instead).
- **LLM fallback only when regex returns mostly empty.** Call `extract_metadata_llm(header_text)` on the first ~40 lines only, and merge — document value wins when well-formed; otherwise the LLM value fills the gap.
- Store the final identity in the per-statement JSON: `account_holder, account_number, ifsc_code, bank_name, branch, account_type, statement_period, opening_balance, closing_balance`.
- **Never** invent fields. Missing → `""` / `UNKNOWN`. Account number absent ⇒ `UNKNOWN-<filestem>` so two unknown statements never merge.
- **Decide one metadata path and delete the other** (see §16). Do not keep both the regex and the LLM metadata systems live and divergent.

---

<a name="14"></a>
## 14. The image / vision path

Images are the one justified exception to "code parses the rows", because pixels cannot be parsed deterministically.

- Keep: Tesseract first → if confidence < 80% → vision LLM reads the image and returns structured rows + account details.
- **But still validate** the vision output with the same balance referee (§8). Vision is not exempt from verification. Rows that do not reconcile are flagged, not trusted blindly.
- For clean, high-confidence images, Tesseract text can be fed into the normal text Tier-2 → Tier-3 path; only low-confidence images go to vision. Keep images local except for the unavoidable low-confidence vision call.

---

<a name="15"></a>
## 15. Output format and storage

Unchanged in spirit from the current `storage.py` (keep it), but ensure:

- **`clean_transactions.csv`** — reconciled rows in the standard schema, oldest-first, with the real account number/IFSC stamped per row.
- **`flagged_transactions.csv`** — every row that failed validation, **with a `flag_reason`** ("balance mismatch", "unreadable date", "both debit and credit present", "un-processed tail"). Never drop.
- **`statements/<holder>_<account>.json`** — one per statement: the metadata JSON + that statement's clean rows.
- **`metadata.json` (the run receipt)** — per file: route, OCR engine + confidence, the schema used and its source (header / cheap-default / LLM), the tier it stopped at, reconciliation rate, LLM call count, clean/flagged counts. This receipt is the courtroom-grade audit trail.
- Nothing leaves the local machine except the anonymised/again-sample LLM calls and the unavoidable low-confidence vision images.

---

<a name="16"></a>
## 16. Code to delete or consolidate

- **Remove the silent chunk cap** (`MAX_CHUNKS`) from the main flow; repurpose `structure_statement` into the scoped Tier-5 `repair_rows` (failing rows only) or delete it if `repair_rows` is cleaner from scratch.
- **Collapse the two identity systems** into one (§13). Delete the unused one. No dead code.
- **Make `discover_transaction_schema` non-decorative** — either feed its full output into the engine, or fold it into the single `discover_schema` interface (§11).
- **Remove unused standardiser paths** if they are genuinely dead after the refactor (e.g. `_detect_csv_format`/`_parse_csv_text` if no longer reached). Verify with a grep before deleting.
- **Scrub bank names from code logic** and add the build guard (§5).

---

<a name="17"></a>
## 17. Privacy decision

The CID rule was "anonymise before any Groq text call." The current code honours it on the Excel/CSV path but not the text path. **Make it consistent.** Choose and document one of:

- **(Recommended) Anonymise the SAMPLE before schema discovery and the failing rows before repair.** Schema discovery only needs the *shape* of the data, not real names — so anonymising the sample costs nothing and satisfies CID. Real identities are restored locally afterwards.
- If full anonymisation of repair rows harms accuracy (names sometimes carry direction cues), document the exception explicitly, limit raw data to the *minimum* failing rows, and surface it in the receipt.

Whatever you choose, the behaviour must be **identical across all text routes** and clearly commented, so a judge can see exactly what leaves the machine.

---

<a name="18"></a>
## 18. Testing and the blind-set acceptance criteria

This is how overfitting is *proved* absent.

1. **Development set:** `synthetic_dataset_full_mentoring/` (by format). Use freely while building.
2. **Blind set:** `original bank statements/` — treat these as **never-seen** statements. Do **not** tune code to them. Additionally, if any brand-new statement is available, add it.
3. **Per-statement metric:** report the **reconciliation rate** and flagged count per file, for both sets. Success = high reconciliation on the **blind** set, not just the development set.
4. **Ground-truth tests** (keep/extend existing): compare extracted output to `transactions_master.csv` (row counts, amounts) and confirm the `ground_truth.json` accounts survive extraction.
5. **Regression guard:** the build guard from §5 (no bank names in logic).
6. **Bounded runs only:** a few files per format; report numbers; never grind the whole dataset unattended.

**Acceptance criteria — you are done when all are true:**
- a) The pipeline uses ONE philosophy (the tiered hybrid); no path sends all rows to the LLM as the default.
- b) The validator exists, is airtight, handles newest-first / missing-opening-balance / no-balance-column / wrapped rows, and decides escalation.
- c) Tokens scale with documents, not rows; a clean statement costs 0–1 transaction LLM calls; caching makes re-runs free.
- d) No silent truncation; the un-processed tail (if any) is flagged.
- e) One metadata system, wired and tested; no dead identity code.
- f) Privacy behaviour is consistent and documented.
- g) Provider access is behind the single interface; a local-model swap touches only that module.
- h) Blind-set reconciliation is high and reported; bank names absent from logic (guard passes).
- i) Plain-language comments throughout.

---

<a name="19"></a>
## 19. Step-by-step build order

Do it in this order so each step rests on a proven foundation:

1. **Build/strengthen the Validator (§8) first.** Including both-direction (newest-first) reconciliation, the no-opening-balance anchor logic, and the no-balance-column fallback. Unit-test it on hand-made rows.
2. **Make the deterministic engine schema-driven (§9).** Feed it the schema object; keep `_correct_direction_by_balance`.
3. **Wire the escalation ladder (§10)** with the config thresholds.
4. **Define the single LLM interface (§11)** and move all provider calls behind it. Repurpose row-reading into scoped `repair_rows`. Remove `MAX_CHUNKS` from the main path.
5. **Content-based sampling (§12)** for schema discovery.
6. **Consolidate metadata (§13)** — wire code-first regex, LLM fallback; delete the dead system.
7. **Keep the image/vision path but add validation (§14).**
8. **Privacy consistency (§17)** and the **anti-overfitting build guard (§5).**
9. **Blind-set tests + receipt reporting (§18).** One small bounded end-to-end run per format; report reconciliation numbers; confirm every acceptance criterion.

---

<a name="20"></a>
## 20. What NOT to do

- Do **not** keep the "LLM reads every row" approach as a default path.
- Do **not** let the LLM write per-statement parsers or regex — it fills parameters only.
- Do **not** drop any row silently; flag it.
- Do **not** branch on a bank's name anywhere in logic.
- Do **not** scatter provider-specific (`groq`) calls through the pipeline.
- Do **not** tune the code to the blind set or the mentoring set; generalisation is the goal.
- Do **not** build the analysis engine, reports, chatbot, or frontend in this task.
- Do **not** run unbounded, laptop-overheating processes.
- Do **not** ask questions — decide as a senior engineer and proceed.

---

*End of CHANGES_INSTRUCTIONS.md — Survey Corps · CIDECODE Hackathon 2026.*
