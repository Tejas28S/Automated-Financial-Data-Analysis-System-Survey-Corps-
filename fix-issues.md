# Extraction Pipeline Audit And Fix Issues

## Executive Summary

This audit compares the current extraction summary against the verified Ground Truth for the Secondary bank-statement folder and reviews the extraction code in the automated financial analysis repository.

Overall finding: the pipeline architecture is strong, but the current extraction summary is not fully accurate against the real Ground Truth. File coverage is correct for the Secondary folder, but row coverage, zero-row reporting, metadata extraction, and duplicate accounting still need fixes before this extraction output should be treated as benchmark-quality.

Important numbers:

| Item | Ground Truth / reference | Current extraction summary | Audit result |
|---|---:|---:|---|
| Secondary files processed | 144 | 144 | Matches |
| Failed files | 0 expected hard failures | 0 | Matches at file-open level |
| Secondary raw transactions | 190,691 | 182,920 clean + flagged | Short by 7,771 rows |
| Approx row coverage | 100% reference | 95.9% | Good, not complete |
| True zero-transaction files | 2 | 6 reported | 4 false zero files |
| Account number metadata present | 142 / 144 in GT | 136 / 144 | Under-extracted |
| Holder metadata present | 139 / 144 in GT | 136 / 144 | Under-extracted |
| IFSC metadata present | 106 / 144 in GT | 85 / 144 | Under-extracted |
| Flagged rows | Not a GT concept | 926 | Mostly extraction/reconciliation issues |
| Duplicate rows tagged | GT raw-canonical difference about 22,263 | 41,078 | Definition likely too broad or not comparable |

The most important correction: only these two Secondary files are true zero-transaction files:

1. `4513362998.pdf`
2. `BOM_Statement_FTP_02107_xxxxxxxx7596_20250812_20250811_20251112013551.pdf`

The extraction summary incorrectly reports these four files as zero-row:

1. `BOM_Statement_FTP_02107_xxxxxxxx7596_20240812_20250801_20250801012337.pdf` - Ground Truth has 570 transaction occurrences.
2. `BOM_Statement_FTP_02772_xxxxxxxx8123_20250514_20251127_20251127115931.pdf` - Ground Truth has 468 source rows, 467 dedup-supported rows.
3. `shivlal statement.txt` - Ground Truth has 362 transactions.
4. `stm REKHA.pdf` - Ground Truth has 470 transactions.

## Overall Quality

Quality grade: medium-high architecture, medium extraction accuracy on this run.

The pipeline is not a weak parser. It has a reasonable production-style extraction design:

- extension and PDF-type router
- digital PDF text/table extraction through `pdfplumber`
- scanned PDF and image OCR path using Tesseract with Groq Vision fallback
- Excel/CSV direct table reader with header detection
- deterministic metadata extraction plus LLM fallback
- deterministic transaction parsing with validation-arbitrated escalation
- balance-chain reconciliation
- flagged-row diagnostics
- duplicate tagging instead of silent deletion
- persisted clean, flagged, duplicate, and metadata outputs

But the measured extraction output still has real gaps:

- It processes all 144 Secondary files, but extracted rows are short by 7,771 versus Ground Truth raw row evidence.
- It falsely classifies four non-empty files as zero-row.
- Metadata extraction misses account numbers, holder names, and especially IFSC codes that Ground Truth found.
- The dominant flagged-row diagnosis is `missing_amount`, meaning rows are being detected but debit/credit values are not being captured correctly.
- Duplicate count is much higher than the Ground Truth raw-to-canonical difference and needs a clearer, stricter duplicate confidence model.

## Strengths

1. File routing is broad and mostly correct.

The router supports `.pdf`, `.xlsx`, `.xls`, `.csv`, `.txt`, `.docx`, `.jpg`, `.jpeg`, and `.png`. It distinguishes digital PDFs from scanned PDFs using embedded-text volume and routes scanned/image files into OCR. This is stronger than the Ground Truth generation approach, which did not perform OCR and used embedded text for PDFs.

2. `.xls` support is present.

`requirements.txt` includes `xlrd>=2.0.1`, and the Excel/CSV extractor explicitly selects `xlrd` for `.xls` where needed. So the issue is not missing `.xls` dependency support.

3. Digital PDF extraction is table-aware.

`extractor_digital_pdf.py` uses `pdfplumber.extract_tables()` when a usable table is detected, preserves page-one metadata, avoids raw page text duplication on later pages, joins multiline cells, and tries to recover inline narration fragments. This is the correct direction for bank statements.

4. Validation is used as a referee.

`validator.grade_parse()` checks completeness and balance reconciliation, and it tries both chronological and reverse chronological ordering. This prevents blindly accepting a parse that merely produced rows.

5. The pipeline does not silently drop duplicates at validation time.

`mark_duplicates()` keeps rows and tags later matches with `duplicate_of`. That is audit-friendly. Storage later separates duplicate rows into `duplicates.csv`, which is acceptable if the duplicate evidence remains available.

6. Metadata extraction is document-first.

The account extractor tries to read account number, holder, IFSC, bank, branch, period, and balances from document content, not only from filenames. This is the right principle for investigation evidence.

7. The previous catastrophic file lifecycle issue appears addressed.

Earlier notes described an old run where many files failed because temporary upload files disappeared. The current `extraction_pipeline.py` has preflight file existence and size checks before parser execution. The current summary also reports `files_failed: 0`.

## Weaknesses

1. Zero-row reporting is currently unreliable.

The summary reports 6 zero-row files, but Ground Truth supports only 2. Four files with hundreds of transactions are marked zero. This is the clearest extraction-level accuracy problem because it directly hides real statements.

2. Functional extraction failures are hidden by `files_failed: 0`.

`files_failed: 0` only means the pipeline did not crash or reject files. It does not mean each file was extracted correctly. A file can be opened, routed, and still produce zero rows incorrectly.

Recommended reporting change: separate `technical_file_failures` from `functional_extraction_failures`. A non-empty supported statement with zero extracted rows should be treated as a functional failure unless raw evidence shows no transaction table.

3. Row coverage is not complete.

The current extraction summary has 182,920 total rows if clean and flagged are combined. Ground Truth has 190,691 raw Secondary transactions. That leaves 7,771 missing rows, about 4.1% of the Secondary corpus.

4. Metadata coverage is below Ground Truth.

Compared with Ground Truth source inventory:

- account number: pipeline 136 / 144, GT 142 / 144
- holder: pipeline 136 / 144, GT 139 / 144
- IFSC: pipeline 85 / 144, GT 106 / 144

IFSC extraction is the largest metadata gap.

5. Balance mismatch flags are concentrated in one failure mode.

The summary reports:

- `balance_mismatch`: 918
- `narration_contains_multiple_transactions`: 8
- `missing_amount`: 885
- `missing_transaction`: 32
- `direction_inverted`: 1

This means most flagged rows are not fraud or detection findings. They are extraction failures where the balance moved but debit/credit amount extraction failed.

6. Duplicate count needs audit clarification.

Current summary reports `duplicates_tagged: 41078`. Ground Truth Secondary raw-to-canonical difference is about 22,263. These are not necessarily identical definitions, but the gap is large enough to inspect. The current duplicate key is:

`Date + Narration + Debit + Credit + Account_ID`

That can over-tag rows when repeated transactions have identical date, narration, amount, and account, especially for high-volume UPI/cash/ATM patterns where the transaction reference or running balance differs.

7. Current summary lacks enough per-file evidence.

The repo `outputs` folder does not contain the detailed run artifacts for this summary. The dataset has `extraction_summary_report.json`, but the audit needs per-file extracted row counts, per-file flagged counts, route, parser tier, metadata source, reconciliation rate, and output paths to localize all 7,771 missing rows.

## Flagged Row Analysis

The flagged rows are mainly extraction-quality signals, not pattern-detection signals.

### Balance mismatch

Count: 918 rows.

Probable meaning: the parser emitted a row, but the row does not satisfy the running-balance chain.

Most useful diagnostic split:

- `missing_amount`: 885 rows
- `missing_transaction`: 32 rows
- `direction_inverted`: 1 row

### Missing amount

This is the main problem. A `missing_amount` diagnosis means the row has a balance transition but debit and credit were zero or blank after parsing.

Likely root causes:

- Amount tokens are split across PDF table cells or wrapped lines.
- Debit/credit columns are not captured even though balance is captured.
- Some statement layouts use integer-looking amounts or formatting not accepted by the text parser's strict money token.
- `pdfplumber.extract_text()` or table extraction may preserve balance but drop the amount column.
- In OCR routes, a low-confidence read may keep the date and balance but miss debit/credit.

High-priority affected account:

- `0167042251865512` has 882 flags.
- `soa_0167042251865512.pdf` has reconciliation rate 0.156 in the summary.
- Ground Truth has 1,928 transactions for this account/file area.

This is likely one large parser-layout failure, not 882 independent unusual transactions.

### Missing transaction

Count: 32 rows.

Probable meaning: the parser captured a row, but the balance gap suggests another transaction is absent between adjacent rows. This may come from row boundary detection, page transition loss, table header/footer filtering, or multiline row stitching.

### Direction inverted

Count: 1 row.

This is a small issue. The validator can identify when swapping debit/credit would reconcile. Direction inference is mostly working.

### Narration contains multiple transactions

Count: 8 rows.

Probable meaning: row stitching joined more than one transaction into one narration. This usually comes from missed date anchors, page-column bleed, or wrapped PDF text being flattened incorrectly.

## Metadata Analysis

The metadata pipeline is conceptually good but still under-extracts compared with Ground Truth.

### Missing-holder report has false positives

The extraction summary lists these as missing holder files, but Ground Truth contains holder names:

| File | Ground Truth holder | Ground Truth account | Ground Truth IFSC |
|---|---|---|---|
| `AccountStmt_0882XXXXXX5304 (1).pdf` | `ANJALI RAM` | `UNKNOWN` | `UCBA0000882` |
| `AccountStmt_1228XXXXXX3352.pdf` | `HARISH RAM` | `UNKNOWN` | `AUBL0002011` |
| `shivlal statement.txt` | `SHIV LAL BISHNOI` | `3127637522775626` | Unknown |
| `statement-33500513952.pdf` | `SANJAY SHETTY` | `81271119214` | `ALLA0212547` |
| `statement-38347344323 (1).pdf` | `ADITYA VERMA` | `38347344323` | `AIRP0000001` |
| `statement-38347344323.pdf` | `ADITYA VERMA` | `38347344323` | `AIRP0000001` |
| `statement-42935093151.pdf` | `Anita Shetty` | `68388838099` | `IBKL0NEFT01` |
| `statement-49952935790.pdf` | `KUNAL SHARMA` | `49952935790` | `SBIN0008603` |

Important nuance: for `AccountStmt_0882XXXXXX5304 (1).pdf` and `AccountStmt_1228XXXXXX3352.pdf`, Ground Truth also has account number `UNKNOWN`. That part may be legitimate because the account number is masked or distorted. But holder and IFSC are present in Ground Truth, so the pipeline should not mark those files as fully missing metadata.

### IFSC extraction is the biggest metadata gap

Ground Truth has IFSC in 106 Secondary files. The pipeline summary has IFSC in only 85. That is 21 missed IFSC values.

Likely root causes:

- IFSC extraction is scoped to the detected header only. If header slicing ends too early or starts after the IFSC line, the global IFSC pattern never sees it.
- PDF table extraction may emit metadata lines differently from plain text extraction.
- TXT/fixed-width metadata may not be handled with enough label variants.
- Some IFSC values are present in multi-column account blocks where label and value are separated by unusual spacing.

Recommended general fix: keep header scoping, but build multiple metadata windows: first-page pre-table text, top N lines of raw text, labelled metadata blocks from Excel/CSV, and any page-one key-value table. Report the exact source line used for every metadata field.

## Extraction Comparison

### Ground Truth extraction approach

The Ground Truth was built as an investigation reference, not as the current production pipeline. It used:

- supported files under Primary, Secondary, and supporting folders
- embedded PDF text for PDFs
- structured parsing for CSV/XLS/XLSX
- text parsing for TXT
- global deduplication across files while preserving duplicate source evidence
- evidence IDs linking transactions to source file/page/location

Important limitation: the Ground Truth generation did not perform OCR. Therefore, when the current pipeline uses OCR/vision correctly, it may be stronger than the Ground Truth on scanned/image-only files. For the Secondary-folder comparison here, however, the current summary still misses rows that Ground Truth found from the available source evidence.

### Current pipeline extraction summary

The current summary reports:

- `files_processed`: 144
- `files_failed`: 0
- `transactions_clean`: 181,994
- `transactions_flagged`: 926
- combined clean + flagged rows: 182,920
- `duplicates_tagged`: 41,078
- zero-row files: 6
- average reconciliation: 0.951

### Main differences

1. File count matches.

The pipeline correctly targets the Secondary folder file count: 144 files.

2. Row extraction is short.

Ground Truth Secondary raw transactions: 190,691.
Pipeline extracted clean + flagged: 182,920.
Difference: 7,771 missing rows.

3. Zero-row list is wrong.

Four files are incorrectly marked zero-row even though Ground Truth has transactions:

| File | Pipeline says | Ground Truth says |
|---|---:|---:|
| `BOM_Statement_FTP_02107_xxxxxxxx7596_20240812_20250801_20250801012337.pdf` | zero rows | 570 transaction occurrences |
| `BOM_Statement_FTP_02772_xxxxxxxx8123_20250514_20251127_20251127115931.pdf` | zero rows | 468 source rows / 467 dedup-supported |
| `shivlal statement.txt` | zero rows | 362 transactions |
| `stm REKHA.pdf` | zero rows | 470 transactions |

4. Metadata extraction is weaker than Ground Truth.

The current summary misses metadata that exists in Ground Truth, especially IFSC.

5. Duplicate reporting is not aligned.

The pipeline duplicate count is much larger than the Ground Truth raw-to-canonical duplicate difference. This may be caused by different definitions, but the duplicate key should be reviewed with transaction references and source-file evidence.

## Parser Architecture Review

### Router

Status: good baseline.

The router correctly supports the required file types and distinguishes digital vs scanned PDFs using embedded text quantity.

Issue: a digital PDF can have embedded text but still require table extraction or OCR/vision for reliable rows. The router only decides the initial path, not extraction quality.

Recommended fix: keep the router, but allow quality-based fallback. If a digital PDF returns zero rows, low completeness, or poor reconciliation, escalate to alternate extraction modes: table-first, raw-text fallback, OCR/vision fallback, or LLM row repair.

### Digital PDF parser

Status: strong design, but not fully sufficient.

Strengths:

- uses `extract_tables()` when possible
- joins multiline cells
- avoids duplicated raw text on later pages
- preserves first-page metadata
- attempts inline narration supplementation

Issues:

- Some non-empty PDFs are still reported as zero rows.
- `soa_0167042251865512.pdf` has very low reconciliation.
- Money parsing remains sensitive to token shape and table layout.

Recommended fix: for every PDF page, store parser telemetry:

- pages read
- tables detected
- table rows detected
- text lines with dates
- transaction-like lines
- parsed rows
- rejected row samples
- first 5 and last 5 transaction-like raw lines

Then use the telemetry to choose fallback paths before declaring zero rows.

### Text/TXT parser

Status: partial.

TXT support exists and routes to the same deterministic text parser as digital PDFs. But `shivlal statement.txt` is falsely reported zero-row while Ground Truth has 362 transactions.

Likely issue: fixed-width text statements may need column-boundary detection instead of only date-anchored line parsing.

Recommended fix: add a TXT/fixed-width table mode:

- detect repeated column starts from header and row positions
- parse date, narration, debit, credit, balance by column span
- support amount tokens with and without decimals when column position confirms that the token is money

### Excel/CSV parser

Status: relatively strong.

The extractor reads all sheets, chooses transaction-like sheets, handles `.xls`, `.xlsx`, and CSV encodings/delimiters, and uses deterministic column mapping before LLM fallback.

Main recommendation: export per-file sheet selection and detected column map into metadata. That will make audit much easier when a file under-extracts.

### Metadata parser

Status: good concept, medium measured accuracy.

The code has many generalized patterns and IFSC prefix-to-bank inference. However, the summary still undercounts holder, account number, and IFSC compared with Ground Truth.

Recommended fix: metadata extraction should emit field-level evidence:

- field name
- value
- source line or table cell
- source method: regex, table metadata block, identity column, LLM fallback
- confidence

This will make false missing metadata immediately visible.

### Validation and reconciliation

Status: strong and useful.

`grade_parse()` is doing the right kind of quality control. It checks reconciliation and completeness and can detect reverse chronological order.

Issue: validation identifies many bad rows but the pipeline still leaves 926 rows flagged and 7,771 rows missing. Validation is acting as a detector, not enough as a repair loop.

Recommended fix: add a row-neighborhood repair step for `missing_amount` and `missing_transaction`, using the raw source lines around the failing balance gap.

### Duplicate detection

Status: audit-friendly, but likely too broad for final duplicate classification.

Current duplicate key:

`Date + Narration + Debit + Credit + Account_ID`

Recommended fix: use duplicate confidence tiers:

- High-confidence duplicate: same account, same date, same amount, same narration, same transaction reference or same running balance and same source-overlap evidence.
- Medium-confidence duplicate: same account, date, amount, narration, but missing reference.
- Low-confidence possible duplicate: same account/date/amount only.

Do not remove or separate rows from the main clean output unless downstream consumers can still reconstruct the original row count and source file evidence.

### Output/reporting

Status: summary useful but not enough.

The current `extraction_summary_report.json` is too aggregated for full debugging.

Recommended fields per file:

- file path
- file type
- route selected
- parser tier selected
- raw text character count
- page count or sheet count
- transaction-like line count
- extracted row count before validation
- clean row count
- flagged row count
- duplicate tagged count
- reconciliation rate
- completeness ratio
- metadata fields found/missing
- metadata source method
- zero-row reason
- first and last parsed transaction date
- unsupported/empty/password/encrypted status

## Priority Fixes

### Priority 0: Fix false zero-row files

Do this before other accuracy work.

Files to use as regression cases:

1. `BOM_Statement_FTP_02107_xxxxxxxx7596_20240812_20250801_20250801012337.pdf`
2. `BOM_Statement_FTP_02772_xxxxxxxx8123_20250514_20251127_20251127115931.pdf`
3. `shivlal statement.txt`
4. `stm REKHA.pdf`

Acceptance criteria:

- none of these files report zero rows
- extracted row count is close to Ground Truth
- file-level reconciliation is reported
- parser route/tier is recorded

### Priority 1: Repair `missing_amount` failures

Use `soa_0167042251865512.pdf` as the main regression case.

Acceptance criteria:

- `missing_amount` count drops materially from 885
- reconciliation for `soa_0167042251865512.pdf` improves from 0.156 toward pass threshold
- raw row evidence is preserved for unrepaired rows

General fix direction:

- when balance changes but debit/credit is zero, inspect raw neighboring lines/table cells
- recover the missing amount from debit/credit columns or balance delta
- only infer from balance delta when the source row proves there is exactly one missing transaction amount
- keep an `amount_recovered_by` audit field if any repair is inference-based

### Priority 2: Improve money-token handling in structured contexts

The strict text money token requires decimals. That protects against reference-number confusion, but it can miss valid statement amounts when a column/table context proves a number is money.

Recommended approach:

- keep strict decimal money matching in free text
- allow integer money tokens only inside confirmed money columns or table spans
- require supporting evidence such as header column, balance chain, or table position
- never globally treat all integers as money

### Priority 3: Improve metadata evidence capture

Acceptance criteria:

- account number coverage approaches GT 142 / 144 where account is not masked
- holder coverage approaches GT 139 / 144
- IFSC coverage approaches GT 106 / 144
- each metadata field has source method and evidence line

### Priority 4: Rework duplicate confidence

Acceptance criteria:

- duplicate count is explainable per file/account
- exact duplicate and possible duplicate are separated
- transaction reference and running balance are used when available
- duplicate output preserves source file evidence and original row count

### Priority 5: Expand summary reporting

Generate a per-file audit table on every extraction run. Without that, row-count gaps like the 7,771 missing Secondary transactions cannot be localized quickly.

Minimum per-file columns:

`file_name`, `route`, `parser_tier`, `raw_transaction_like_lines`, `rows_extracted`, `rows_clean`, `rows_flagged`, `duplicates_tagged`, `reconciliation_rate`, `metadata_found`, `zero_row_reason`.

## Validation Cases To Add

Use these as regression fixtures. They are not statement-specific hard-coded rules; they are representative layout failures.

| Case | File | Expected behavior |
|---|---|---|
| True zero file | `4513362998.pdf` | zero rows accepted with reason |
| True zero file | `BOM_Statement_FTP_02107_xxxxxxxx7596_20250812_20250811_20251112013551.pdf` | zero rows accepted with reason |
| False zero PDF | `BOM_Statement_FTP_02107_xxxxxxxx7596_20240812_20250801_20250801012337.pdf` | about 570 rows, not zero |
| False zero PDF | `BOM_Statement_FTP_02772_xxxxxxxx8123_20250514_20251127_20251127115931.pdf` | about 468 rows, not zero |
| False zero TXT | `shivlal statement.txt` | about 362 rows, not zero |
| False zero PDF | `stm REKHA.pdf` | about 470 rows, not zero |
| Large missing amount cluster | `soa_0167042251865512.pdf` | high row capture and high reconciliation |
| Masked account metadata | `AccountStmt_0882XXXXXX5304 (1).pdf` | holder and IFSC extracted; account may remain masked/unknown |
| Masked account metadata | `AccountStmt_1228XXXXXX3352.pdf` | holder and IFSC extracted; account may remain masked/unknown |

## Final Conclusion

The current extraction pipeline is directionally good and has the right architecture for a generalized financial extraction system. It should not be restarted from scratch.

But the current Secondary-folder extraction summary is not accurate enough to be treated as final Ground Truth or as a clean benchmark output. The priority is to fix extraction completeness, especially false zero-row files and the `missing_amount` cluster, then improve metadata coverage and duplicate confidence reporting.

The most important point: these are extraction-layer issues, not fraud-detection or pattern-matching issues. The pipeline is opening files and producing a large amount of useful data, but it is still missing real transactions and under-reading metadata in measurable, evidence-backed ways.
