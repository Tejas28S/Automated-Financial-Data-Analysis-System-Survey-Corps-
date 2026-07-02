# Ground Truth Validation

## Files Processed

Supported files processed: 162

Unsupported files: []

## Completeness

- PDFs processed: 103
- CSV files processed: 11
- XLS files processed: 22
- XLSX files processed: 23
- TXT files processed: 3

## Statements And Transactions

Accounts profiled: 111

Deduplicated transactions: 183192

Pattern instances: 459

Money flow records: 5114

Statements with zero extracted transactions after review:

- Secondary/4513362998.pdf - statement summary reports zero withdrawal and zero deposit count.
- Secondary/BOM_Statement_FTP_02107_xxxxxxxx7596_20250812_20250811_20251112013551.pdf - statement text reports "No Transactions in this Period" and total transaction count 0.

Residual ambiguity:

- 439 transactions remain under account `UNKNOWN`, all from two UCO Bank PDF statements with distorted/masked account-header extraction. The transactions remain evidence-linked to their source files and pages.

## Validation Result

Pass with noted limitations. Every supported source file was opened and processed; unsupported files are listed separately. Conclusions in the primary JSON artifacts reference source evidence IDs.
