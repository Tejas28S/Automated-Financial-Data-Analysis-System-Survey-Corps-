# Verification Notes

## Assumptions

- The dataset root did not contain a literal `bank statements/` directory. The available dataset folders were `primary/` and `Secondary/`, with future sibling folders included if present.
- `ground truth prompt tms.txt` was treated as the controlling specification because `Financial_Investigation_Ground_Truth_Generation_Prompt.md` was not present.
- No OCR was performed. PDF processing used embedded text extraction only.

## Ambiguities

- Some PDFs and CSV/XLSX files duplicate the same statement. Files were all processed, then transactions were deduplicated for global behavioural analysis.
- PDF-only line parsing can be less reliable than tabular CSV/XLSX data. Such transactions carry medium confidence.
- Single-sided large transfers are included as observed movements but not asserted as internal dataset flows unless matched by reference/amount/date.
- Two supported source files contain no transaction activity in the statement body and were retained in the inventory with zero extracted transactions: `Secondary/4513362998.pdf` and `Secondary/BOM_Statement_FTP_02107_xxxxxxxx7596_20250812_20250811_20251112013551.pdf`.
- A small residual account identifier ambiguity remains for two UCO Bank PDFs with distorted account-header text; affected transactions are retained under `UNKNOWN` with source-file/page evidence.

## Investigator Recommendations

- Manually verify all High and Critical Risk accounts against original statements.
- Request source-bank KYC files for accounts with repeated cash-out patterns.
- Validate shared UPI identifiers and recurrent beneficiaries with bank-side beneficiary master data.
