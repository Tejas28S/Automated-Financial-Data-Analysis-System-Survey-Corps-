# Resolution Strategy For The Extraction Pipeline

## Purpose

This document converts the findings in `fix-issues.md`, the verified
`ground_truth.md`, and the current extraction architecture review into an
implementation roadmap.

It is intentionally not production code. It is the design blueprint for the
final extraction-hardening phase before the project relies on extracted data for
analysis.

## Executive Decision

The extraction engine should not be rewritten from scratch. The current
architecture is directionally correct: it has routing, digital PDF extraction,
OCR/vision support, Excel/CSV direct parsing, deterministic metadata extraction,
LLM fallback, validation, duplicate tagging, and persistence.

The gap is not a missing fraud-rule engine. The gap is extraction fidelity:
some supported statements are opened but functionally under-extracted, some
non-empty statements are reported as zero-row, metadata is under-captured, and
validation currently acts more as a detector than as a repair/escalation loop.

The core strategy is therefore:

1. Preserve the validation-arbitrated tiered hybrid.
2. Make every parser produce telemetry and source evidence.
3. Run multiple extraction candidates when quality is low.
4. Use validation to choose, repair, or escalate before finalizing output.
5. Keep all uncertain rows visible with reason codes and evidence.
6. Add regression fixtures from the known failures and from the full 162-file
   ground truth.

## Evidence Baseline

### Verified Ground Truth

The verified ground truth covers the real statement corpus under `primary/` and
`Secondary/`.

| Item | Verified value |
| --- | ---: |
| Real statement/source files analyzed | 162 |
| Raw transactions before dedupe | 205,455 |
| Deduplicated investigation transactions | 183,192 |
| Account profiles | 111 |
| Entities | 71,186 |
| Relationships | 56,490 |
| Behavioural/fraud pattern instances | 459 |
| Money-flow records | 5,114 |
| True zero-transaction real statements | 2 |
| Unsupported real statement files | 0 |

The 34 CSV files under `synthetic_data/` are a separate benchmark corpus and
must not be merged into the real-statement extraction metrics.

### Current Extraction Audit

`fix-issues.md` compares an older Secondary-only extraction summary against the
Secondary ground truth. Those numbers are useful for issue diagnosis, but they
are not the final full-corpus benchmark.

| Item | Ground truth / reference | Current extraction summary | Result |
| --- | ---: | ---: | --- |
| Secondary files processed | 144 | 144 | File coverage matched |
| Failed files | 0 expected hard failures | 0 | Technical open matched |
| Secondary raw transactions | 190,691 | 182,920 clean + flagged | Short by 7,771 |
| True zero-transaction Secondary files | 2 | 6 reported | 4 false zero files |
| Account number metadata present | 142 / 144 | 136 / 144 | Under-extracted |
| Holder metadata present | 139 / 144 | 136 / 144 | Under-extracted |
| IFSC metadata present | 106 / 144 | 85 / 144 | Under-extracted |
| Flagged rows | Not a GT concept | 926 | Mostly extraction defects |
| Duplicate rows tagged | About 22,263 raw-to-canonical gap | 41,078 | Definition too broad or not comparable |

The four known false zero-row files are:

| File | Ground truth transactions |
| --- | ---: |
| `BOM_Statement_FTP_02107_xxxxxxxx7596_20240812_20250801_20250801012337.pdf` | 570 |
| `BOM_Statement_FTP_02772_xxxxxxxx8123_20250514_20251127_20251127115931.pdf` | 468 |
| `shivlal statement.txt` | 362 |
| `stm REKHA.pdf` | 470 |

The two legitimate zero-row files are:

| File | Ground truth reason |
| --- | --- |
| `4513362998.pdf` | Statement summary reports zero withdrawal and deposit count |
| `BOM_Statement_FTP_02107_xxxxxxxx7596_20250812_20250811_20251112013551.pdf` | Text reports no transactions in period |

## Target Architecture

The final extraction architecture should be a candidate-based, evidence-led
pipeline:

1. Source manifest and preflight ledger
   - Record every input file before parsing.
   - Store type, size, extension, route, pages/sheets, text character count, and
     readable/non-readable status.
   - Separate technical failures from functional extraction failures.

2. Multi-window metadata extraction
   - Extract identity from page-one pre-table text, top N raw lines, coordinate
     grouped PDF header words, Excel/CSV metadata blocks, identity columns, and
     sheet names.
   - Emit field-level evidence for every value.

3. Candidate transaction extraction
   - For each file, produce one or more parse candidates: table-aware PDF,
     layout-text PDF, fixed-width TXT, direct spreadsheet, OCR/vision, LLM schema
     reparse, or LLM full read where needed.
   - Do not declare zero rows until all appropriate candidates have been tried or
     the document has positive zero-activity evidence.

4. Validation referee
   - Use running balance, completeness, row counts, opening/closing balance,
     date sanity, direction sanity, and duplicate evidence to grade candidates.
   - Select the best candidate, repair localized defects when possible, and
     escalate only when objective checks fail.

5. Evidence-preserving output
   - Every final row must retain source file, route, parser tier, page/sheet,
     raw-line/table-cell reference where available, confidence, and repair/source
     method.
   - No row should be silently dropped.

6. Per-file extraction report
   - The run summary must include a file-level ledger, not just aggregate totals.
   - The report must show which files are complete, weak, zero, repaired,
     escalated, duplicated, or still uncertain.

## Issue 1: Full-Corpus Scope And Benchmark Alignment

### Issue

The previous issue audit used a 144-file Secondary-only extraction summary, while
the verified ground truth now covers 162 real statement files across `primary/`
and `Secondary/`.

### Why It Happens

Extraction reports are generated per run, but the current summary does not embed
enough dataset-scope context to distinguish:

- Secondary-only runs
- full real-statement runs
- synthetic benchmark runs
- partial upload/session runs

This makes it easy to compare numbers from different populations.

### Why The Ground Truth Did Better

The ground truth explicitly records source folders, file type counts, unsupported
files, zero-transaction statements, synthetic benchmark separation, and final
validation totals.

### Recommended Generalized Approach

Add a source manifest stage before extraction:

- Assign each input file a stable source id.
- Store dataset scope: `primary`, `Secondary`, `synthetic`, or user upload.
- Store whether synthetic files are included or excluded from real-statement
  metrics.
- Emit `manifest_total_files`, `supported_files`, `unsupported_files`,
  `attempted_files`, `processed_files`, `technical_failures`, and
  `functional_failures`.
- Require any benchmark comparison to declare the manifest id and source folders.

### Expected Improvement

Row, account, and file counts will become comparable across runs. The team will
not mistake a 144-file Secondary result for full 162-file accuracy.

### Regression Prevention

Add a regression test that runs the manifest builder over the real corpus and
asserts the expected supported type counts:

- PDF: 103
- XLSX: 23
- CSV: 11
- XLS: 22
- TXT: 3
- total real statements: 162

## Issue 2: False Zero-Row Statements

### Issue

Four files with hundreds of ground-truth transactions are reported as zero-row in
the extraction summary.

### Why It Happens

The current pipeline treats successful file opening plus empty standardized rows
as a valid `status: ok` result. `files_failed: 0` therefore means "no technical
crash", not "every file was functionally extracted".

Likely parser weaknesses:

- Digital PDFs may have text but in a layout the default text parser cannot
  recognize.
- PDF table extraction may select the wrong table, miss column boundaries, or
  discard a valid table if it fails minimum shape rules.
- TXT fixed-width statements may need column-span parsing instead of date-first
  line parsing.
- Zero-row adjudication does not require positive evidence of no transactions.

### Why The Ground Truth Did Better

The ground truth inspected every source file and preserved file-level transaction
counts. It distinguished true zero-activity statements from parser failures. It
also separately documented the two real zero-transaction statements with source
evidence.

### Recommended Generalized Approach

Implement a zero-row adjudicator.

A supported statement may be finalized as zero-row only when one of these is true:

- The document contains explicit zero-activity text such as "No Transactions in
  this Period" or statement summary counts of zero withdrawals and zero deposits.
- The file is readable, parser telemetry shows no transaction-like lines, no
  transaction-like tables, and no money/date row structures.
- The file is unreadable or encrypted and is recorded as a technical failure,
  not as zero-row success.

If a parser returns zero rows but raw evidence shows date-plus-money lines,
tables, or repeated fixed-width row patterns, treat it as a functional extraction
failure and escalate.

Fallback order for zero-row candidates:

1. Re-run digital PDF with table-first extraction telemetry.
2. Re-run digital PDF with layout-preserving text extraction.
3. Run row-boundary parser over raw text, allowing leading reference tokens.
4. For TXT, run fixed-width column-span detection.
5. For suspected image/scanned pages, route to OCR/vision even if embedded text
   exists.
6. Use targeted LLM schema discovery on representative raw rows.
7. Use full LLM read only when cheaper candidates fail and the document is within
   quota limits.

### Expected Improvement

The four known false-zero files should produce transaction rows close to ground
truth counts. Future non-empty statements will be flagged as functional failures
instead of silently appearing as successful zero-row files.

### Regression Prevention

Add fixture tests:

| Fixture | Expected result |
| --- | --- |
| `4513362998.pdf` | Accepted zero-row with explicit reason |
| `BOM_Statement_FTP_02107_xxxxxxxx7596_20250812_20250811_20251112013551.pdf` | Accepted zero-row with explicit reason |
| `BOM_Statement_FTP_02107_xxxxxxxx7596_20240812_20250801_20250801012337.pdf` | Not zero; about 570 rows |
| `BOM_Statement_FTP_02772_xxxxxxxx8123_20250514_20251127_20251127115931.pdf` | Not zero; about 468 rows |
| `shivlal statement.txt` | Not zero; about 362 rows |
| `stm REKHA.pdf` | Not zero; about 470 rows |

## Issue 3: Missing 7,771 Secondary Rows

### Issue

The current Secondary extraction has 182,920 combined clean and flagged rows,
while the Secondary ground truth has 190,691 raw transaction occurrences.

### Why It Happens

The aggregate summary does not identify where rows went missing. Current
`extraction_summary_report.json` has no per-file row ledger, no expected-row
estimate per file, and no rejected-row samples.

Likely sources of row loss:

- Missed page-continuation rows.
- Multi-line transaction rows not stitched correctly.
- Header/footer blocks interrupting row assembly.
- Fixed-width TXT rows not parsed by column spans.
- PDF table rows dropped when table selection chooses the wrong candidate.
- Integer-only amounts missed by strict decimal money detection.
- Duplicate representations separated into duplicate output without a comparable
  raw/canonical definition.

### Why The Ground Truth Did Better

The ground truth preserved source-level row counts, deduplicated globally while
retaining source evidence, and made raw-before-dedupe counts visible.

### Recommended Generalized Approach

Create a file-level extraction ledger with these fields:

- file id and path
- route selected
- parser candidates attempted
- selected parser tier
- pages or sheets read
- raw text character count
- tables detected
- raw table rows detected
- transaction-like line count
- rows parsed before validation
- rows clean
- rows flagged
- rows duplicate-tagged
- rows rejected before validation
- reconciliation rate
- completeness ratio
- first and last transaction date
- metadata fields found and source evidence
- zero-row reason or functional-failure reason
- first 5 and last 5 rejected row samples

Use this ledger to compare extracted rows against ground-truth counts in
benchmark mode, and against internal expected-row estimates in ordinary uploads.

### Expected Improvement

Missing rows will be localizable to specific files and parser stages. This
enables targeted fixes instead of broad parser changes.

### Regression Prevention

Add a "no aggregate-only summary" test: every report must include one ledger row
per processed input file. Add full-corpus benchmark checks that compare row counts
per file against `source_inventory.json` within an allowed tolerance for duplicate
representations and known limitations.

## Issue 4: `missing_amount` Balance Mismatch Cluster

### Issue

The current run has 885 `missing_amount` diagnoses, mostly concentrated in
account `0167042251865512` from `soa_0167042251865512.pdf`, whose reconciliation
rate is only 0.156.

### Why It Happens

The parser emitted rows with dates and balances, but debit/credit amounts were
blank or zero even though the running balance changed. This points to extraction
misalignment, not suspicious banking behavior.

Likely causes:

- Amount columns split from narration/date columns.
- Non-standard amount header vocabulary.
- Balance captured but debit/credit column dropped.
- Amount present in a continuation line rather than the date-started line.
- Integer-looking or short decimal tokens rejected by money parsing.
- PDF coordinate/table extraction losing cells.

### Why The Ground Truth Did Better

The ground truth retained transaction evidence at source-file/page level and
accepted amount values when supported by the row context, not only by a generic
date-start text line.

### Recommended Generalized Approach

Add row-neighborhood repair driven by `grade_parse.failing_row_indices`.

For each `missing_amount` row:

1. Retrieve the raw source evidence around that row:
   - PDF table row cells and neighboring cells
   - layout text line before and after
   - page number and coordinates when available
   - original spreadsheet row when structured
2. Search only the row neighborhood for candidate debit/credit amount tokens.
3. Prefer source-present amount tokens over inferred values.
4. If exactly one source-supported candidate reconciles the balance chain, repair
   the row and mark `amount_recovered_by=source_row`.
5. If no source token is available but the balance delta uniquely identifies the
   amount, optionally infer it only when:
   - previous and current balances are reliable
   - no intervening transaction is missing
   - the row is a single transaction boundary
   - direction follows the balance delta
   Mark this as `amount_recovered_by=balance_delta_inference`.
6. If multiple candidates exist, keep the row flagged with a precise reason:
   `amount_ambiguous`.

### Expected Improvement

The major `missing_amount` cluster should shrink materially. Reconciliation for
`soa_0167042251865512.pdf` should move toward the pass threshold without hiding
unrepaired uncertainty.

### Regression Prevention

Use `soa_0167042251865512.pdf` as a regression fixture:

- `missing_amount` count must drop substantially from 885.
- The file must retain close to the ground-truth 1,928 transactions.
- Reconciliation must improve from 0.156.
- Any inferred amount must carry a repair method and source context.
- No repaired row may overwrite a source-present amount.

## Issue 5: Strict Money Token Handling

### Issue

The parser protects against reference-number confusion by requiring decimal money
tokens in free text, but some valid statements contain integer-looking amounts or
amounts whose decimal part is omitted.

### Why It Happens

One global money-token rule is doing two different jobs:

- In free text, it must avoid mistaking reference numbers for money.
- In confirmed money columns, it should accept values that are valid because the
  column position proves they are amounts.

### Why The Ground Truth Did Better

Ground truth parsing used source context: column headers, table positions,
balance changes, and spreadsheet types.

### Recommended Generalized Approach

Use context-sensitive money parsing.

- Free-text mode:
  - Keep strict decimal money detection.
  - Treat integer tokens as non-money unless backed by strong context.
- Structured column mode:
  - Accept integer values in confirmed debit, credit, amount, or balance columns.
  - Accept `Dr` or `Cr` suffix/prefix markers.
  - Normalize signs and parentheses.
- Balance-chain mode:
  - Accept an integer amount candidate if it is the only token that reconciles
    the row and is located in a plausible money span.
- OCR mode:
  - Use lower confidence and require corroboration from balance arithmetic or
    column position.

### Expected Improvement

Valid rows with integer-only amounts will be recovered without globally treating
all numeric strings as money.

### Regression Prevention

Add tests where reference numbers and integer amounts appear together. The parser
must accept integers only in money contexts and must not convert transaction
references, cheque numbers, phone numbers, or account numbers into amounts.

## Issue 6: Transaction Boundary Detection

### Issue

Some transaction rows are merged into narration or split across page boundaries.
The audit mentions `narration_contains_multiple_transactions`, `missing_transaction`,
page continuation issues, and row-start layouts where a reference or serial number
appears before the date.

### Why It Happens

The date-started line parser is strong for many layouts, but it still encodes a
layout assumption: a new transaction usually begins with a date. Real statements
may start rows with:

- serial number then date
- transaction id then date
- value date before transaction date
- date split across lines
- multi-line narration before amount columns finish
- repeated page headers between a row and its continuation

### Why The Ground Truth Did Better

The ground truth reconstructed rows from complete source evidence and preserved
duplicate/source relationships. It did not rely on one row-start rule alone.

### Recommended Generalized Approach

Build a row-boundary engine with multiple generic row-start signatures:

- date at line start
- short numeric serial/reference followed by date
- transaction id followed by date
- date within first N characters followed by at least one money token
- fixed-width date column at a stable x or character span
- spreadsheet row with valid date column

Before row assembly, remove page furniture:

- repeated column headers
- page numbers
- statement summary/footer blocks
- repeated identity headers
- legal/disclaimer blocks
- repeated lines that appear on many pages and do not contain transaction amounts

During row assembly:

- Carry pending rows across page breaks.
- Join continuation narration lines only until the next valid row-start signal.
- Rejoin page-split dates when a date prefix lacks a year and the next fragment
  begins with a plausible year.
- Keep raw continuation evidence so repairs are auditable.

After row assembly:

- Flag any narration above a generous length threshold with multiple date-like
  substrings as `narration_contains_multiple_transactions`.
- Attempt row re-splitting before final validation when the bloated narration
  contains complete date-plus-money row signatures.

### Expected Improvement

Fewer rows will be swallowed into narration, page transitions will lose fewer
transactions, and `missing_transaction` counts will become actionable rather
than generic balance mismatches.

### Regression Prevention

Test layouts with:

- date-first rows
- serial-before-date rows
- transaction-id-before-date rows
- page-split dates
- repeated headers and footers
- multi-line narrations
- fixed-width TXT rows

Assert that row count, longest narration length, and reconciliation do not
regress on known working formats.

## Issue 7: Header And Footer Removal

### Issue

Past runs showed HDFC/DCB-style footer bleed, page header bleed, statement summary
rows, and repeated page furniture entering narration or rows.

### Why It Happens

PDF text extraction emits visual page furniture in reading order. If the parser
does not strip it before transaction assembly, page headers and footers become
continuation text.

### Why The Ground Truth Did Better

The investigation preserved transaction evidence and documented limitations
instead of treating page furniture as transaction content.

### Recommended Generalized Approach

Keep and expand the current generic page-furniture filters, but make them
telemetry-driven:

- Count stripped header lines, footer lines, summary blocks, and repeated
  furniture lines per page.
- Keep a small sample of stripped lines for audit review.
- Never strip a line that starts with a valid transaction row signature.
- Use structural signals, not bank-name branches:
  - repeated column vocabulary
  - legal/disclaimer phrases
  - page summary labels
  - registered office/contact/footer labels
  - page number patterns
  - repeated non-transaction lines across pages

### Expected Improvement

Narration cleanliness will improve without dropping valid transaction rows.

### Regression Prevention

For known footer-bleed fixtures, verify longest narration length drops while row
count and reconciliation stay stable. For clean fixtures, verify no new rows are
removed and no reconciliation drop occurs.

## Issue 8: CR/DR Interpretation And Debit/Credit Direction

### Issue

The audit reports only one `direction_inverted` row, so direction inference is
mostly working. However, future layouts may use single amount columns, negative
debit signs, `Dr/Cr` suffixes, or reversed statement order.

### Why It Happens

Debit/credit direction can be encoded in several places:

- separate debit and credit columns
- one amount column plus Dr/Cr flag
- sign of amount
- suffix such as `(Dr)` or `(Cr)`
- narration tokens
- running balance delta

Any one rule is insufficient.

### Why The Ground Truth Did Better

The ground truth used observed transaction direction, balance behavior, and
statement context.

### Recommended Generalized Approach

Use a hierarchy of direction evidence:

1. Explicit debit/credit column.
2. Explicit Dr/Cr flag or suffix.
3. Sign convention in a confirmed money column.
4. Running balance delta.
5. Narration keyword only as weak fallback.

For newest-first statements, determine ordering before applying balance deltas.
For the first row with no previous balance, keep direction from explicit column
or flag; otherwise mark direction confidence lower.

### Expected Improvement

Direction accuracy stays high across both current and unseen formats.

### Regression Prevention

Add tests for:

- separate debit/credit columns
- single amount plus Dr/Cr flag
- negative debit values
- newest-first statements
- first-row ambiguity

## Issue 9: Metadata Under-Extraction

### Issue

The current Secondary summary under-extracts metadata:

- account number: 136 / 144 vs GT 142 / 144
- holder: 136 / 144 vs GT 139 / 144
- IFSC: 85 / 144 vs GT 106 / 144

IFSC is the largest gap.

### Why It Happens

Current metadata extraction is document-first and generally sound, but it still
misses values when:

- header scoping ends before the metadata line
- metadata appears in a table rather than linear text
- labels and values sit in multi-column PDF headers
- spreadsheets carry identity as repeated columns rather than key-value blocks
- TXT statements use fixed-width metadata
- account numbers are masked but holder and IFSC are still readable
- LLM fallback is triggered only when all key fields are absent, not when one
  important field is missing

### Why The Ground Truth Did Better

The ground truth used multiple sources of identity evidence and retained Unknown
only where the source did not support a reliable value. It also did not discard
transactions when account identity was masked.

### Recommended Generalized Approach

Implement field-level metadata extraction with multiple windows and evidence.

For each field, attempt:

1. PDF coordinate header extraction:
   - Group words by y-coordinate.
   - Detect label-value pairs in the same visual row.
   - Support side-by-side label/value panels.
2. PDF text metadata region:
   - first page pre-table text
   - top N lines
   - first 3 pages and last page for long statements
3. Table metadata extraction:
   - page-one key-value tables
   - spreadsheet rows above the transaction table
   - non-transaction sheets
4. Identity columns:
   - repeated account number, holder, IFSC, CIF, branch, bank columns
5. Shape-based extraction:
   - IFSC by exact shape in metadata region
   - account number by plausible length and label proximity
6. Filename-derived candidates:
   - low-confidence candidate only
   - never overwrite document-sourced metadata
   - report source as `filename_candidate`
7. Targeted LLM fallback:
   - call when important fields remain missing, not only when all metadata is
     missing
   - pass only metadata-region text
   - validate LLM output with shape and label-fragment filters

Every metadata field should emit:

- value
- confidence
- source method
- source file/page/sheet/line/cell when available
- alternatives and conflicts
- whether the value is masked or unknown

### Expected Improvement

Holder, account number, and IFSC coverage should approach the ground-truth
levels while reducing false positives such as branch/city labels captured as
holders.

### Regression Prevention

Use the audit metadata cases:

- `AccountStmt_0882XXXXXX5304 (1).pdf`: holder and IFSC should be extracted;
  account may remain Unknown.
- `AccountStmt_1228XXXXXX3352.pdf`: holder and IFSC should be extracted;
  account may remain Unknown.
- `shivlal statement.txt`: holder and account should be extracted.
- `statement-33500513952.pdf`: holder, account, and IFSC should be extracted.
- `statement-38347344323*.pdf`: holder/account/IFSC should be extracted.
- `statement-42935093151.pdf`: holder/account/IFSC should be extracted.
- `statement-49952935790.pdf`: holder/account/IFSC should be extracted.

## Issue 10: IFSC Extraction Gap

### Issue

The extraction summary finds IFSC in only 85 of 144 Secondary files, while ground
truth finds IFSC in 106.

### Why It Happens

IFSC values are globally shaped but can appear:

- in header text
- in transaction narrations as counterparty IFSCs
- in branch detail blocks
- in metadata tables
- in spreadsheet columns

Scanning too narrowly misses own-account IFSC. Scanning too broadly risks
capturing counterparty IFSC.

### Why The Ground Truth Did Better

Ground truth retained all IFSC entities but interpreted own-account metadata
conservatively.

### Recommended Generalized Approach

Separate IFSC roles:

- `account_ifsc`: the statement account's own IFSC.
- `counterparty_ifsc`: IFSCs found in transaction narrations.
- `all_ifsc_observed`: every IFSC-like token with source evidence.

For `account_ifsc`, search only metadata windows and identity columns. Use first
page/header/table position and labels as confidence boosters. If only transaction
narration IFSCs are found, store them as counterparty IFSCs, not account IFSC.

Infer bank name from account IFSC prefix only when account IFSC is confident.

### Expected Improvement

IFSC coverage improves while preventing false bank attribution from counterparty
IFSCs.

### Regression Prevention

Add tests where transaction narrations contain many IFSCs but the account header
does not. The account IFSC should remain unknown while counterparty IFSC entities
are preserved.

## Issue 11: TXT And Fixed-Width Statements

### Issue

`shivlal statement.txt` is reported zero-row by the current run but ground truth
has 362 transactions.

### Why It Happens

TXT statements can be fixed-width tables. A date-anchored free-text parser may
miss rows when columns are aligned by character span rather than by delimiters or
when amount tokens lack the expected shape.

### Why The Ground Truth Did Better

The ground truth parsed TXT as a supported statement type and recovered row
structure from the document content.

### Recommended Generalized Approach

Add TXT/fixed-width mode:

- Detect repeated header labels and their character spans.
- Infer column boundaries from header positions and row value alignment.
- Parse date, narration, debit, credit, amount, Dr/Cr flag, and balance by spans.
- Allow integer money only in confirmed money spans.
- Preserve original line and column-span evidence.
- Fall back to delimiter sniffing for CSV-like TXT.

### Expected Improvement

Plain-text bank statements will no longer depend on PDF-style row parsing.

### Regression Prevention

Add all 3 TXT files as fixtures. Assert non-zero extraction for the two NITIN
files and about 362 rows for `shivlal statement.txt`.

## Issue 12: PDF Table Selection And Geometry

### Issue

Digital PDF extraction is table-aware, but false zero PDFs and low-reconciliation
PDFs show that one table-selection path is not enough.

### Why It Happens

`pdfplumber.extract_tables()` may return:

- metadata tables
- summary tables
- ledger tables split across pages
- tables with merged cells
- tables with missing vertical lines
- multiple tables where the widest table is not always the transaction table

The current parser does not emit enough geometry telemetry to explain wrong
choices.

### Why The Ground Truth Did Better

The investigation used source evidence rather than accepting a parser's table
choice blindly.

### Recommended Generalized Approach

For each PDF page, emit extraction telemetry:

- page number
- text character count
- table count
- candidate table dimensions
- table header words
- candidate transaction-row count
- selected table id and score
- rejected table ids and reasons
- first and last parsed transaction-like lines

Improve table selection scoring:

- Header vocabulary score
- date-column consistency
- money-column consistency
- running-balance consistency
- row count
- continuity with previous and next page

Run alternate table strategies when selected candidate produces low quality:

- line strategy
- text strategy
- explicit vertical/horizontal line detection
- word-coordinate column clustering
- layout-preserving text parser

### Expected Improvement

False zero PDFs and low-reconciliation PDFs will be diagnosable and recoverable
without file-specific patches.

### Regression Prevention

For known table-PDF fixtures, assert parser telemetry is present and selected
candidate scores are stable. Do not allow a zero-row PDF without telemetry and
zero-row adjudication.

## Issue 13: Duplicate Detection Too Broad

### Issue

The current run tags 41,078 duplicates, while the ground-truth raw-to-canonical
difference is about 22,263 for the comparable Secondary corpus. The definitions
may differ, but the current duplicate key is likely too broad:

`Date + Narration + Debit + Credit + Account_ID`

### Why It Happens

Repeated legitimate transactions can share date, narration, amount, and account,
especially in high-volume UPI, ATM, cash, fee, and recurring transfer patterns.
If transaction reference, page/source, row sequence, or running balance is
available, excluding them from duplicate confidence can over-tag real repeats.

### Why The Ground Truth Did Better

Ground truth globally deduplicated duplicate statement representations while
preserving source evidence. It did not treat all repeated same-day same-amount
transactions as identical with equal confidence.

### Recommended Generalized Approach

Use duplicate confidence tiers:

- Exact duplicate:
  - same account
  - same date/time if present
  - same amount and direction
  - same narration normalized
  - same transaction reference, cheque number, UTR/RRN, or same running balance
  - overlapping source statement period or duplicate file representation
- Probable duplicate:
  - same account/date/amount/narration
  - missing reference
  - matching running balance or same source row evidence
- Possible duplicate:
  - same account/date/amount but missing or generic narration/reference

Do not remove rows from the main extraction output. Keep duplicate tags and a
`duplicate_confidence` field. In analysis, canonical views can filter exact
duplicates while investigators can still reconstruct raw source rows.

### Expected Improvement

Duplicate counts become explainable and comparable to ground-truth raw/canonical
counts. Legitimate repeated transactions are less likely to be over-tagged.

### Regression Prevention

Add tests for:

- duplicate PDF/XLS representations of same statement
- repeated ATM withdrawals with same amount and narration
- repeated UPI transactions on the same day with different references
- exact same source uploaded twice

## Issue 14: Flagged Rows Are Extraction Diagnostics

### Issue

The current flagged rows are not fraud findings. They mostly indicate extraction
defects.

### Why It Happens

`validate_and_clean()` flags rows after parsing, but there is no required repair
or fallback loop for the dominant categories.

### Why The Ground Truth Did Better

Ground truth conclusions are evidence-backed and do not treat parser failures as
behavioral indicators.

### Recommended Generalized Approach

Treat each flagged category as a parser diagnostic:

| Category | Root cause | Redesign | Verification |
| --- | --- | --- | --- |
| `missing_amount` | Row captured but debit/credit lost | row-neighborhood amount recovery and column geometry parsing | count drops; reconciliation improves |
| `missing_transaction` | Whole row absent between balances | row-boundary repair, page-continuation repair, bloated-narration re-split | row count increases; balance gaps shrink |
| `direction_inverted` | Debit/credit side wrong | direction hierarchy and balance-delta correction | swapped row reconciles; count stays near zero |
| `narration_contains_multiple_transactions` | Row boundaries missed | generalized row-start detector and post-parse re-splitting | longest narration normalizes; count drops |
| `invalid_date` | Summary/footer row or date parse failure | separate summary rows from transactions; expand date formats | summary rows excluded; true date failures visible |
| `both_debit_credit_filled` | column misalignment | column map validation and single-amount mode | exclusivity failures drop |

### Expected Improvement

Flagged rows become actionable. The pipeline will repair what can be repaired and
leave a smaller, better-explained set of unresolved rows.

### Regression Prevention

Every flagged category must have:

- a unit test
- at least one real fixture if available
- a report count
- a reason code
- no silent suppression

## Issue 15: Validation Should Be A Self-Checking System

### Issue

Validation currently catches many issues, but the system can still finalize weak
files with zero rows, poor reconciliation, or under-extracted metadata.

### Why It Happens

Validation is mostly row-level and aggregate. It does not yet enforce a file-level
quality gate before accepting output.

### Why The Ground Truth Did Better

The ground truth validated totals, accounts, file coverage, unsupported files,
zero-transaction statements, and evidence traceability.

### Recommended Generalized Approach

Add file-level quality gates:

- Technical status:
  - file exists
  - readable
  - route selected
  - parser executed
- Functional status:
  - rows extracted or accepted zero reason
  - reconciliation above threshold or unresolved reason recorded
  - completeness above threshold or unresolved reason recorded
  - metadata fields present or missing reason recorded
  - row count plausible versus expected transaction-like evidence
  - first/last dates plausible
  - opening and closing balances reconcile when present

Recommended status values:

- `complete`
- `complete_with_flags`
- `accepted_zero_activity`
- `functional_extraction_failure`
- `technical_failure`
- `needs_manual_review`

### Expected Improvement

The pipeline will stop presenting "opened successfully" as "extracted correctly".

### Regression Prevention

Report generation should fail tests if a supported, non-empty statement has
`rows_standardised=0` without either `accepted_zero_activity` or
`functional_extraction_failure`.

## Issue 16: Metadata Confidence And Conflicts

### Issue

Some metadata fields contain header fragments rather than clean KYC names in the
ground truth and current artifacts. Extraction should not overstate confidence.

### Why It Happens

Bank statements often have messy headers, masked accounts, duplicate references,
and branch/customer labels close to holder labels.

### Why The Ground Truth Did Better

The ground truth retained source-derived labels but documented metadata caution
and did not let identity ambiguity drive unsupported relationships.

### Recommended Generalized Approach

Attach confidence to metadata:

- High:
  - labelled value in metadata region with valid shape
  - coordinate label-value pair
  - identity column with dominant repeated value
- Medium:
  - unlabelled holder candidate in header
  - IFSC-derived bank name
  - LLM value validated by deterministic filters
- Low:
  - filename candidate
  - single-token holder
  - conflicting alternatives
  - masked account

Store conflicts rather than overwriting:

- `account_number_candidates`
- `holder_candidates`
- `ifsc_candidates`
- `selected_value`
- `selection_reason`

### Expected Improvement

Investigators can distinguish confident identity from weak or masked metadata.

### Regression Prevention

Add tests where branch/city/address labels are near holder labels. The extractor
must not silently report a branch city as high-confidence account holder.

## Issue 17: Cross-File Relationships And Duplicate Formats

### Issue

Many accounts appear in more than one file because of PDF/CSV/XLS/XLSX duplicate
formats or repeated statement periods. File count does not equal account count.

### Why It Happens

The extraction pipeline currently focuses on per-file parsing and only tags row
duplicates after concatenation. It does not yet produce a clear account/file
relationship map.

### Why The Ground Truth Did Better

Ground truth identified 111 unique account profiles from 162 files and retained
duplicate source evidence.

### Recommended Generalized Approach

Add account-file profiling:

- Group files by account number, holder, IFSC, bank, statement period, and row
  fingerprint overlap.
- Identify duplicate representations:
  - PDF plus CSV of same period
  - PDF plus XLS of same statement
  - repeated generated Excel reports
  - overlapping statement periods
- Store `file_relationships`:
  - `same_account`
  - `same_statement_period`
  - `duplicate_representation`
  - `overlapping_period`
  - `conflicting_metadata`

Use cross-file relationships to improve extraction:

- If PDF and CSV siblings exist, compare row counts and metadata.
- If one format extracts confidently and another weakly, use the confident format
  as benchmark evidence in regression mode, not as runtime data override.

### Expected Improvement

The system will explain why 162 files produce fewer unique accounts and will
avoid double-counting duplicate statement representations in analysis.

### Regression Prevention

Add tests that verify known duplicate PDF/CSV/XLS pairs are grouped without
deleting source rows.

## Issue 18: Reporting Is Too Aggregated

### Issue

The current `extraction_summary_report.json` lacks enough per-file evidence to
localize missing rows, false zeros, metadata gaps, or duplicate over-tagging.

### Why It Happens

Report generation summarizes totals from in-memory DataFrames and per-file
records, but the per-file records do not yet include enough telemetry.

### Why The Ground Truth Did Better

The ground truth artifacts include source inventory, validation notes, account
profiles, relationships, and evidence ids.

### Recommended Generalized Approach

Create two reports per run:

1. Machine-readable `extraction_ledger.json`
   - one object per file
   - all telemetry fields from the target architecture
   - metadata field evidence
   - parser candidates and scores
   - validation status
2. Human-readable `extraction_audit.md`
   - totals
   - functional failures
   - accepted zero-activity files
   - low-reconciliation files
   - metadata gaps
   - duplicate confidence summary
   - flagged row diagnosis
   - recommended manual review list

### Expected Improvement

Future issue discovery becomes much faster and less speculative.

### Regression Prevention

The report generator should be tested against a synthetic per-file record set
with zero-row, false-zero, metadata-missing, duplicate, and low-reconciliation
cases.

## Issue 19: LLM Usage Strategy

### Issue

The current LLM fallback architecture is useful, but it can still be too coarse:
full-statement reads are expensive, and metadata fallback may be underused for
field-level gaps.

### Why It Happens

LLM usage is attached to parser tiers rather than specific evidence gaps.

### Why The Ground Truth Did Better

The ground truth investigation effectively used targeted reasoning over source
evidence, not blind full-document extraction for every file.

### Recommended Generalized Approach

Use LLMs only in targeted roles:

- Metadata:
  - call on metadata windows when key fields remain missing or conflicting
  - return structured JSON with field-level confidence and source quote/line id
  - validate output deterministically
- Schema discovery:
  - call on representative rows, not whole files
  - feed returned schema back into deterministic parser
- Row repair:
  - call only on failing row neighborhoods from `grade_parse`
  - require output to preserve source evidence
- Full statement read:
  - last resort only
  - budget-limited
  - selected only when it improves reconciliation/completeness

Text LLM calls should continue using redaction/identifier vaults. Vision calls
remain the unavoidable exception for image content.

### Expected Improvement

Better accuracy at lower token cost, with fewer hallucination risks.

### Regression Prevention

Track LLM calls by purpose: metadata, schema, row repair, full read, vision. Add
tests that ensure LLM fallback cannot overwrite high-confidence deterministic
values with invalid shapes.

## Issue 20: Regression Prevention Strategy

### Issue

Earlier iterations risked fixing one file while breaking another. The final phase
needs durable regression protection.

### Recommended Generalized Approach

Create a tiered fixture suite:

1. Golden real-statement fixtures:
   - false zero PDFs
   - true zero PDFs
   - `shivlal statement.txt`
   - `soa_0167042251865512.pdf`
   - masked account PDFs
   - duplicate PDF/CSV/XLS pairs
   - high-volume PDFs
2. Synthetic layout fixtures:
   - date-first rows
   - reference-before-date rows
   - fixed-width TXT
   - integer-only money columns
   - single amount plus Dr/Cr flag
   - newest-first order
   - multi-line narration
   - page header/footer bleed
3. Full-corpus benchmark mode:
   - run all 162 real statements
   - compare file coverage, zero-row list, row counts, metadata coverage, and
     reconciliation against ground-truth artifacts

Acceptance metrics:

- 162 real files attempted in full-corpus benchmark.
- Only the 2 verified true zero-transaction files accepted as zero.
- No supported non-empty file finalized as zero-row success.
- Metadata coverage approaches or exceeds Secondary reference:
  - account number near 142 / 144 where not masked
  - holder near 139 / 144
  - IFSC near 106 / 144
- `missing_amount` and false-zero counts materially reduced.
- Duplicate counts explainable by confidence tier.
- Every remaining low-confidence file has a reason and source evidence.

## Priority Order

### Priority 0: Reporting And Benchmark Manifest

Build the source manifest and per-file extraction ledger first. Without this,
every other fix is hard to verify.

Deliverables:

- `extraction_ledger.json`
- enhanced `extraction_audit.md`
- technical vs functional failure statuses
- full 162-file manifest support

### Priority 1: False Zero-Row Prevention

Implement zero-row adjudication and fallback escalation.

Regression fixtures:

- the 4 false zero files
- the 2 true zero files

### Priority 2: Row-Level Repair For Balance Failures

Use `grade_parse.failing_row_indices` for localized repair.

First target:

- `soa_0167042251865512.pdf`

### Priority 3: Metadata Coverage And Evidence

Implement field-level metadata evidence, multi-window extraction, coordinate PDF
headers, and targeted LLM fallback.

First targets:

- masked account PDFs
- missing holder files from the audit
- IFSC gap cases

### Priority 4: TXT / Fixed-Width Parser

Implement fixed-width TXT detection and parsing.

First target:

- `shivlal statement.txt`

### Priority 5: Duplicate Confidence Model

Replace one duplicate key with duplicate confidence tiers while preserving all
rows.

### Priority 6: Full-Corpus Benchmark Run

Re-run against all 162 real statements and compare against ground truth.

## Final Acceptance Checklist

The extraction pipeline should be considered ready for the Analysis Phase only
when the next full benchmark report can answer all of these questions:

- Which exact files were processed?
- Which exact files failed technically?
- Which exact files failed functionally?
- Which files are accepted zero-activity statements, and why?
- How many rows were extracted per file?
- How many rows were expected per file by source evidence?
- Which rows were repaired, and by what method?
- Which metadata fields were extracted, from where, and at what confidence?
- Which files have low reconciliation, and why?
- Which duplicates are exact, probable, or possible?
- How close is the full 162-file run to the verified ground truth?
- What limitations remain, with file-level evidence?

The goal is not to force zero flags. The goal is that nothing important is
silently missed, every residual uncertainty is named, and extraction output is
traceable enough for investigators and downstream analysis to trust.
