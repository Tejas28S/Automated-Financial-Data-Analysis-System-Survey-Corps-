# Investigation Report

## Executive Summary

This ground-truth investigation processed 162 supported bank-statement source files across the dataset folders. The extracted and deduplicated transaction corpus contains 183192 transactions across 111 account identifiers and 17 identified banks. The consolidated statement period spans 2009-06-12 to 2026-01-21 where dates were extractable.

The strongest recurring behaviours are rapid post-credit cash-out, high-velocity UPI/digital activity, repeated round-value withdrawals, repeated beneficiaries, and high balance volatility. These are documented as indicators requiring review, not as standalone proof of fraud.

## Dataset Overview

| File type | Count |
| --- | --- |
| pdf | 103 |
| xlsx | 23 |
| csv | 11 |
| xls | 22 |
| txt | 3 |

## File Processing Inventory

| Source file | Type | Status | Pages | Worksheets | Transactions |
| --- | --- | --- | --- | --- | --- |
| primary/00869354051.pdf | pdf | processed | 11 | 0 | 205 |
| primary/08874795659248.pdf | pdf | processed | 6 | 0 | 77 |
| primary/098030016134598.pdf | pdf | processed | 555 | 0 | 11038 |
| primary/17771917925.pdf | pdf | processed | 10 | 0 | 178 |
| primary/18306700003.pdf | pdf | processed | 10 | 0 | 181 |
| primary/211566492688.pdf | pdf | processed | 7 | 0 | 56 |
| primary/24704559049070.pdf | pdf | processed | 34 | 0 | 694 |
| primary/258082779154.pdf | pdf | processed | 2 | 0 | 32 |
| primary/280442153117.pdf | pdf | processed | 9 | 0 | 224 |
| primary/43920027363506.pdf | pdf | processed | 10 | 0 | 137 |
| primary/50192424882238.pdf | pdf | processed | 11 | 0 | 199 |
| primary/61577175569879.pdf | pdf | processed | 5 | 0 | 72 |
| primary/72615533841078.pdf | pdf | processed | 16 | 0 | 242 |
| primary/772342103350.pdf | pdf | processed | 2 | 0 | 47 |
| primary/8642666611469255.pdf | pdf | processed | 8 | 0 | 62 |
| primary/92883409730.pdf | pdf | processed | 8 | 0 | 132 |
| primary/95773447976527.pdf | pdf | processed | 14 | 0 | 247 |
| primary/99572217148131.pdf | pdf | processed | 46 | 0 | 941 |
| Secondary/001029700065_SOA.pdf | pdf | processed | 10 | 0 | 347 |
| Secondary/112108374579 SOA.xlsx | xlsx | processed |  | 1 | 267 |
| Secondary/138488664629235-23-11-2024to11-12-2025.csv | csv | processed |  | 0 | 135 |
| Secondary/138488664629235-23-11-2024to11-12-2025.pdf | pdf | processed | 5 | 0 | 135 |
| Secondary/145090675346 SOA.xlsx | xlsx | processed |  | 1 | 97 |
| Secondary/15060151642297 statement.pdf | pdf | processed | 9 | 0 | 273 |
| Secondary/17496072039317 statement.xlsx | xlsx | processed |  | 1 | 9273 |
| Secondary/21558442690581muhammedGHOSHstatement.pdf | pdf | processed | 2 | 0 | 55 |
| Secondary/216655101347_01-Jan-2025_22-May-2025.pdf | pdf | processed | 5 | 0 | 282 |
| Secondary/216655101347_SOA.pdf | pdf | processed | 8 | 0 | 281 |
| Secondary/250269305544183-23-11-2024to26-11-2025.csv | csv | processed |  | 0 | 80 |
| Secondary/250269305544183-23-11-2024to26-11-2025.pdf | pdf | processed | 4 | 0 | 80 |
| Secondary/25078124219247-YASH DUBEY.csv | csv | processed |  | 0 | 10876 |
| Secondary/269415176159622-18-03-2025to26-11-2025.csv | csv | processed |  | 0 | 97 |
| Secondary/269415176159622-18-03-2025to26-11-2025.pdf | pdf | processed | 4 | 0 | 97 |
| Secondary/285265765401_stmt.xls | xls | processed |  | 1 | 71 |
| Secondary/3277373660.xlsx | xlsx | processed |  | 2 | 794 |
| Secondary/331087 CASA Account Statement_Report (1).xlsx | xlsx | processed |  | 1 | 555 |
| Secondary/331087 CASA Account Statement_Report (44).xlsx | xlsx | processed |  | 1 | 342 |
| Secondary/331087 CASA Account Statement_Report - 2025-12-01T152741.012.xlsx | xlsx | processed |  | 1 | 342 |
| Secondary/331087 CASA Account Statement_Report - 2025-12-01T153834.423.xlsx | xlsx | processed |  | 1 | 555 |
| Secondary/331087 CASA Account Statement_Report - 2026-01-05T125426.715.xlsx | xlsx | processed |  | 1 | 1287 |
| Secondary/331087 CASA Account Statement_Report - 2026-01-05T125443.633.xlsx | xlsx | processed |  | 1 | 265 |
| Secondary/331087 CASA Account Statement_Report - 2026-01-05T125452.696.xlsx | xlsx | processed |  | 1 | 253 |
| Secondary/331087 CASA Account Statement_Report - 2026-01-05T125512.728.xlsx | xlsx | processed |  | 1 | 1653 |
| Secondary/331087 CASA Account Statement_Report - 2026-01-05T125521.765.xlsx | xlsx | processed |  | 1 | 343 |
| Secondary/331087 CASA Account Statement_Report - 2026-01-05T125543.717.xlsx | xlsx | processed |  | 1 | 11 |
| Secondary/331087 CASA Account Statement_Report - 2026-01-05T125553.377.xlsx | xlsx | processed |  | 1 | 141 |
| Secondary/331087 CASA Account Statement_Report - 2026-01-05T125612.249.xlsx | xlsx | processed |  | 1 | 377 |
| Secondary/331087 CASA Account Statement_Report - 2026-01-05T125637.336.xlsx | xlsx | processed |  | 1 | 433 |
| Secondary/351964263933349_26-MAR-2024_04-JAN-2026.pdf | pdf | processed | 157 | 0 | 3256 |
| Secondary/351964263933349_26-MAR-2024_28-NOV-2025.pdf | pdf | processed | 157 | 0 | 3255 |
| Secondary/378-147-3193 Statement.pdf | pdf | processed | 3 | 0 | 46 |
| Secondary/4185179967.pdf | pdf | processed | 6 | 0 | 74 |
| Secondary/42618891001229 STATEMENT IN EXCEL.xlsx | xlsx | processed |  | 1 | 192 |
| Secondary/42618891001229STATEMENT IN PDF.pdf | pdf | processed | 7 | 0 | 192 |
| Secondary/4513362998.pdf | pdf | processed | 1 | 0 | 0 |
| Secondary/45170 stmt.pdf | pdf | processed | 46 | 0 | 1565 |
| Secondary/457-111-2165 Statement 1.pdf | pdf | processed | 234 | 0 | 10872 |
| Secondary/457-111-2165 Statement 2.pdf | pdf | processed | 2 | 0 | 3 |
| Secondary/464196045738107-04-01-2024to11-12-2025.csv | csv | processed |  | 0 | 154 |
| Secondary/464196045738107-04-01-2024to11-12-2025.pdf | pdf | processed | 6 | 0 | 154 |
| Secondary/520698390475976-21-12-2020to26-11-2025.csv | csv | processed |  | 0 | 3797 |
| Secondary/520698390475976-21-12-2020to26-11-2025.pdf | pdf | processed | 111 | 0 | 3797 |
| Secondary/524159813738 SOA.xlsx | xlsx | processed |  | 1 | 58 |
| Secondary/5629592364.pdf | pdf | processed | 5 | 0 | 53 |
| Secondary/6147181405386.pdf | pdf | processed | 34 | 0 | 543 |
| Secondary/654658757412329-05-09-2024to26-11-2025.csv | csv | processed |  | 0 | 186 |
| Secondary/654658757412329-05-09-2024to26-11-2025.pdf | pdf | processed | 7 | 0 | 186 |
| Secondary/700870937566_statment.pdf | pdf | processed | 10 | 0 | 362 |
| Secondary/7979137025.pdf | pdf | processed | 21 | 0 | 304 |
| Secondary/79895082327702 ARJUN SHAILESHBHA Excel Statement.csv | csv | processed |  | 0 | 47 |
| Secondary/79895082327702-ARJUN SHAILESHBHA.csv | csv | processed |  | 0 | 47 |
| Secondary/839178205347_SOA.pdf | pdf | processed | 14 | 0 | 526 |
| Secondary/882358884158137_Statement.pdf | pdf | processed | 35 | 0 | 991 |
| Secondary/8855611820.pdf | pdf | processed | 90 | 0 | 1269 |
| Secondary/913628731289_stmt.xls | xls | processed |  | 1 | 326 |
| Secondary/958533930537174-14-02-2024to11-12-2025.csv | csv | processed |  | 0 | 300 |
| Secondary/958533930537174-14-02-2024to11-12-2025.pdf | pdf | processed | 12 | 0 | 300 |
| Secondary/9810055876.pdf | pdf | processed | 107 | 0 | 1312 |
| Secondary/_nfscbsdata__20251201_Acct_Statement_PDF_RB_014_87889641689_10231_0000000061981820.pdf | pdf | processed | 6 | 0 | 92 |
| Secondary/AccountStmt_0882XXXXXX5304 (1).pdf | pdf | processed | 13 | 0 | 221 |
| Secondary/AccountStmt_1228XXXXXX3352.pdf | pdf | processed | 13 | 0 | 218 |
| Secondary/Acct_Statement_XLS_87889641689_10231_0000000061982837_01122025_132749.xls | xls | processed |  | 1 | 92 |
| Secondary/ADITYA (SOA).pdf | pdf | processed | 3 | 0 | 68 |
| Secondary/Bank statement from opening to till date (2).pdf | pdf | processed | 15 | 0 | 413 |
| Secondary/Bank statement from opening to till date (3).pdf | pdf | processed | 15 | 0 | 413 |
| Secondary/Bank statement from opening to till date.pdf | pdf | processed | 2 | 0 | 27 |
| Secondary/BOM_Statement_FTP_01701_xxxxxxxx1206_20250327_20251127_20251127122714.pdf | pdf | processed | 2 | 0 | 34 |
| Secondary/BOM_Statement_FTP_02107_xxxxxxxx7596_20240812_20250801_20250801012337.pdf | pdf | processed | 14 | 0 | 570 |
| Secondary/BOM_Statement_FTP_02107_xxxxxxxx7596_20250812_20250811_20251112013551.pdf | pdf | processed | 1 | 0 | 0 |
| Secondary/BOM_Statement_FTP_02772_xxxxxxxx8123_20250514_20251127_20251127115931.pdf | pdf | processed | 11 | 0 | 468 |
| Secondary/CASA_STATEMENT_8442098066767557_10-JUL-2023_27-Nov-2025.pdf | pdf | processed | 90 | 0 | 1073 |
| Secondary/CASA_STATEMENT_8442098066767557_10-JUL-2023_27-Nov-2025_.xlsx | xlsx | processed |  | 1 | 1284 |
| Secondary/DEVANSHU_STMNT.pdf | pdf | processed | 3 | 0 | 23 |
| Secondary/ICORE_STMT_294500196490.csv | csv | processed |  | 0 | 1117 |
| Secondary/ISHA STAT NW.pdf | pdf | processed | 1 | 0 | 6 |
| Secondary/KOMAL statement.pdf | pdf | processed | 3 | 0 | 31 |
| Secondary/NITIN stat (1).txt | txt | processed |  | 0 | 197 |
| Secondary/NITIN stat.txt | txt | processed |  | 0 | 197 |
| Secondary/NITIN statement.pdf | pdf | processed | 3 | 0 | 49 |
| Secondary/SACHIN SETHI account statement.pdf | pdf | processed | 4 | 0 | 290 |
| Secondary/shivlal statement.txt | txt | processed |  | 0 | 362 |
| Secondary/SOA 294500196490.xlsx | xlsx | processed |  | 1 | 1117 |
| Secondary/SOA.pdf | pdf | processed | 14 | 0 | 200 |
| Secondary/soa_0167042251865512.pdf | pdf | processed | 222 | 0 | 1928 |
| Secondary/SOA_214526512302.xlsx | xlsx | processed |  | 1 | 3168 |
| Secondary/SOA_489506257213.xlsx | xlsx | processed |  | 1 | 8372 |
| Secondary/statement (2).pdf | pdf | processed | 12 | 0 | 103 |
| Secondary/STATEMENT (3).pdf | pdf | processed | 194 | 0 | 2603 |
| Secondary/statement (4).pdf | pdf | processed | 8 | 0 | 99 |
| Secondary/statement (5).pdf | pdf | processed | 12 | 0 | 103 |
| Secondary/STATEMENT (6).pdf | pdf | processed | 194 | 0 | 2603 |
| Secondary/statement (7).pdf | pdf | processed | 8 | 0 | 99 |
| Secondary/STATEMENT - 17496072039317.pdf | pdf | processed | 274 | 0 | 9273 |
| Secondary/STATEMENT 1026(2).pdf | pdf | processed | 35 | 0 | 446 |
| Secondary/STATEMENT 1026.pdf | pdf | processed | 35 | 0 | 446 |
| Secondary/STATEMENT 4.pdf | pdf | processed | 80 | 0 | 913 |
| Secondary/Statement 57856891688032 (1).pdf | pdf | processed | 106 | 0 | 2214 |
| Secondary/Statement 57856891688032.pdf | pdf | processed | 106 | 0 | 2214 |
| Secondary/Statement from 01042021 to 05012026.pdf | pdf | processed | 36 | 0 | 550 |
| Secondary/Statement from 16082019 to 31032021 .pdf | pdf | processed | 2 | 0 | 41 |
| Secondary/statement-16649443003.pdf | pdf | processed | 12 | 0 | 139 |
| Secondary/statement-23383849532.pdf | pdf | processed | 40 | 0 | 473 |
| Secondary/statement-29680171959.pdf | pdf | processed | 4 | 0 | 39 |
| Secondary/statement-33500513952.pdf | pdf | processed | 919 | 0 | 9399 |
| Secondary/statement-38347344323 (1).pdf | pdf | processed | 205 | 0 | 2447 |
| Secondary/statement-38347344323.pdf | pdf | processed | 1011 | 0 | 12086 |
| Secondary/statement-38675866795.pdf | pdf | processed | 4 | 0 | 35 |
| Secondary/statement-42935093151.pdf | pdf | processed | 58 | 0 | 710 |
| Secondary/statement-49952935790.pdf | pdf | processed | 4 | 0 | 36 |
| Secondary/statement-57918678797.pdf | pdf | processed | 3 | 0 | 31 |
| Secondary/statement-64711576662.pdf | pdf | processed | 373 | 0 | 3809 |
| Secondary/statement-72735988647.pdf | pdf | processed | 11 | 0 | 112 |
| Secondary/statement-74715157448.pdf | pdf | processed | 32 | 0 | 328 |
| Secondary/statement-80417906569.pdf | pdf | processed | 3 | 0 | 23 |
| Secondary/statement-80708265377.pdf | pdf | processed | 370 | 0 | 3785 |
| Secondary/statement-81954044160.pdf | pdf | processed | 110 | 0 | 1101 |
| Secondary/statement-85393777281.pdf | pdf | processed | 11 | 0 | 98 |
| Secondary/statement-91612657813.pdf | pdf | processed | 111 | 0 | 1516 |
| Secondary/statement-92460899397.pdf | pdf | processed | 8 | 0 | 83 |
| Secondary/Statement.pdf | pdf | processed | 3 | 0 | 46 |
| Secondary/statement16649443003.xls | xls | processed |  | 1 | 139 |
| Secondary/statement23383849532.xls | xls | processed |  | 1 | 473 |
| Secondary/statement29680171959.xls | xls | processed |  | 1 | 39 |
| Secondary/statement33500513952.xls | xls | processed |  | 1 | 9381 |
| Secondary/statement38347344323 (1) (2).xls | xls | processed |  | 1 | 2447 |
| Secondary/statement38347344323.xls | xls | processed |  | 1 | 12087 |
| Secondary/statement38675866795.xls | xls | processed |  | 1 | 35 |
| Secondary/statement42935093151.xls | xls | processed |  | 1 | 710 |
| Secondary/statement49952935790.xls | xls | processed |  | 1 | 36 |
| Secondary/statement57918678797.xls | xls | processed |  | 1 | 31 |
| Secondary/statement64711576662.xls | xls | processed |  | 1 | 3809 |
| Secondary/statement72735988647.xls | xls | processed |  | 1 | 112 |
| Secondary/statement74715157448.xls | xls | processed |  | 1 | 328 |
| Secondary/statement80417906569.xls | xls | processed |  | 1 | 23 |
| Secondary/statement80708265377.xls | xls | processed |  | 1 | 3785 |
| Secondary/statement81954044160.xls | xls | processed |  | 1 | 1101 |
| Secondary/statement85393777281.xls | xls | processed |  | 1 | 98 |
| Secondary/statement91612657813.xls | xls | processed |  | 1 | 1516 |
| Secondary/statement92460899397.xls | xls | processed |  | 1 | 83 |
| Secondary/Statement_120126_164703.pdf | pdf | processed | 14 | 0 | 291 |
| Secondary/stm REKHA.pdf | pdf | processed | 10 | 0 | 470 |
| Secondary/TARUN PILLAI statement.pdf | pdf | processed | 717 | 0 | 9700 |

## Account Profiles

| Account | Holder | Bank | Txn Count | Debits | Credits | Risk |
| --- | --- | --- | --- | --- | --- | --- |
| 38347344323 | PO- BAHIRCHAK, PS- DHOLAHAT, Nominee Name ADITYA VERMA | State Bank of India | 29063 | 47937703.43 | 47985539.26 | Critical Risk |
| 81271119214 | SANJAY SHETTY | State Bank of India | 18780 | 97713679.74 | 97720229.2 | Critical Risk |
| 17496072039317 | 17496072039317 Sarita Deepa 0 Cr Opening Balance | HDFC Bank | 18546 | 36019319.42 | 54273982.53 | High Risk |
| 25078124219247 | 25078124219247 YASH DUBEY for the period 11-09-2024 - to- 04-01-2026 | Axis Bank | 11136 | 42464824.62 | 42094824.92 | High Risk |
| 098030016134598 | A/C type: FREEDOM FLEXI 25 | State Bank of India | 11038 | 73229426.03 | 73228426.92 | High Risk |
| 38211367068923 | Page No .: 1 | IDFC FIRST Bank | 9700 | 39063365.21 | 39031767.44 | Critical Risk |
| 489506257213 | Unknown | Canara Bank | 8372 | 0 | 0 | High Risk |
| 64711576662 | ARJUN | State Bank of India | 7614 | 6751549.8 | 6707663.06 | Critical Risk |
| 520698390475976 | - AISHWARYA PATEL | Axis Bank | 7592 | 14440730.5 | 14470540.47 | Critical Risk |
| 80708265377 | Nitin : 0.00 | State Bank of India | 7568 | 5268648.02 | 5267097.62 | High Risk |
| 351964263933349 | ARJUN AMIT KUMAR Branch : 001 - THOOTHUKUDI MAIN | Unknown | 3257 | 3332905.54 | 9676484.56 | High Risk |
| 214526512302 | Unknown | Bandhan Bank | 3168 | 0 | 0 | High Risk |
| 91612657813 | PRIYA CHAUHAN | State Bank of India | 3026 | 4853769.95 | 4852156.91 | Critical Risk |
| 16423304381803 | Page No .: 1 | HDFC Bank | 2603 | 2819533.0 | 2819533.0 | High Risk |
| 8442098066767557 | Mr Rajesh Aditya Kapoor | Kotak Mahindra Bank | 2357 | 876051.38 | 845601.57 | Critical Risk |

## Money Flow Summary

| Date | Source | Destination | Amount | Method | Confidence |
| --- | --- | --- | --- | --- | --- |
| 2025-06-05 | External/Unknown | 17496072039317 | 17764659.71 | UPI | Medium |
| 2023-12-22 | 38211367068923 | 23 5 YBL-YESB0YBLUPI-372238368016-PAYMENT FROM PHONE | 6879714.79 | UPI | Medium |
| 2025-01-07 | External/Unknown | 57856891688032 | 6879691.61 | UPI | Medium |
| 2025-03-03 | External/Unknown | 57856891688032 | 6879677.27 | UPI | Medium |
| 2025-03-02 | 57856891688032 | rohan@ibl | 6879627.27 | UPI | Medium |
| 2023-12-21 | External/Unknown | 38211367068923 | 6879624.79 | UPI | Medium |
| 2025-03-28 | 57856891688032 | paytmqr5wxfvf@ptys | 6879507.44 | UPI | Medium |
| 2025-03-27 | External/Unknown | 57856891688032 | 6879476.44 | UPI | Medium |
| 2025-03-01 | External/Unknown | 57856891688032 | 6879327.27 | UPI | Medium |
| 2025-01-03 | 57856891688032 | 7980332122 33212277 UPI | 6879008.61 | UPI | Medium |
| 2025-01-03 | External/Unknown | 57856891688032 | 6878928.61 | UPI | Medium |
| 2025-03-03 | 57856891688032 | 5062867571 86757119 MBK | 6878227.27 | Unknown | Medium |
| 2025-05-03 | 46652787342452 | 25 12 HDFC BANK LIMITED Contents of this statement will be considered correct if no error is reported within 30 days of  | 6876826.92 | UPI | Medium |
| 2025-05-03 | External/Unknown | 46652787342452 | 6876496.92 | UPI | Medium |
| 2025-02-24 | 57856891688032 | swati@ybl | 6876429.27 | UPI | Medium |
| 2025-02-23 | External/Unknown | 57856891688032 | 6876292.27 | UPI | Medium |
| 2025-01-08 | 57856891688032 | Payment | 6876226.61 | UPI | Medium |
| 2024-11-16 | 38211367068923 | 24 36 YBL-YESB0YBLUPI-432106182997-UPI | 6868344.73 | UPI | Medium |
| 2024-11-15 | External/Unknown | 38211367068923 | 6867794.73 | UPI | Medium |
| 2025-06-13 | External/Unknown | 258082779154 | 6224000.0 | Unknown | Medium |
| 2025-11-27 | External/Unknown | 9810055876 | 5597686.84 | UPI | Medium |
| 2025-05-29 | External/Unknown | 92883409730 | 5020000.0 | Unknown | Medium |
| 2025-04-24 | External/Unknown | 098030016134598 | 5000000.0 | RTGS | Medium |
| 2025-05-13 | External/Unknown | 24704559049070 | 5000000.0 | NEFT | Medium |
| 2025-03-27 | External/Unknown | 61577175569879 | 5000000.0 | RTGS | Medium |
| 2025-05-21 | 50192424882238 | Cr. Count Count 0.00 C 167 31 ******END OF STATEMENT****** Account, as per DICGC norms. Details on Deposit Insurance Cov | 4839473.0 | POS/Card | Medium |
| 2025-04-24 | External/Unknown | 50192424882238 | 4784997.0 | RTGS | Medium |
| 2025-05-09 | External/Unknown | 43920027363506 | 4700000.0 | RTGS | Medium |
| 2025-05-02 | External/Unknown | 280442153117 | 4000000.0 | Unknown | Medium |
| 2024-10-05 | External/Unknown | 958533930537174 | 3800000.0 | RTGS | Medium |

## Network Analysis

The transaction network contains 51495 nodes and 53283 directed account-counterparty edges. Top connected nodes are: [('098030016134598', 10473), ('38211367068923', 7269), ('38347344323', 5068), ('17496072039317', 3893), ('81271119214', 3445), ('16423304381803', 2446), ('64711576662', 2054), ('489506257213', 1472), ('25078124219247', 1470), ('80708265377', 1216)].

## Fraud Pattern Summary

| Instance | Pattern | Name | Accounts | Confidence |
| --- | --- | --- | --- | --- |
| PAT-0001 | FP-001 | Rapid Fund Movement | 00869354051 | High |
| PAT-0002 | FP-012 | High Velocity Transactions | 00869354051 | Medium |
| PAT-0003 | FP-013 | Round Value Behaviour | 00869354051 | Medium |
| PAT-0004 | FP-015 | Repeated Beneficiary | 00869354051 | High |
| PAT-0005 | FP-029 | Balance Volatility | 00869354051 | Medium |
| PAT-0006 | FP-001 | Rapid Fund Movement | 08874795659248 | High |
| PAT-0007 | FP-012 | High Velocity Transactions | 08874795659248 | Medium |
| PAT-0008 | FP-029 | Balance Volatility | 08874795659248 | Medium |
| PAT-0009 | FP-001 | Rapid Fund Movement | 098030016134598 | High |
| PAT-0010 | FP-012 | High Velocity Transactions | 098030016134598 | Medium |
| PAT-0011 | FP-013 | Round Value Behaviour | 098030016134598 | Medium |
| PAT-0012 | FP-015 | Repeated Beneficiary | 098030016134598 | High |
| PAT-0013 | FP-029 | Balance Volatility | 098030016134598 | Medium |
| PAT-0014 | FP-001 | Rapid Fund Movement | 17771917925 | High |
| PAT-0015 | FP-012 | High Velocity Transactions | 17771917925 | Medium |
| PAT-0016 | FP-015 | Repeated Beneficiary | 17771917925 | High |
| PAT-0017 | FP-029 | Balance Volatility | 17771917925 | Medium |
| PAT-0018 | FP-001 | Rapid Fund Movement | 113154476620 | High |
| PAT-0019 | FP-012 | High Velocity Transactions | 113154476620 | Medium |
| PAT-0020 | FP-015 | Repeated Beneficiary | 113154476620 | High |
| PAT-0021 | FP-029 | Balance Volatility | 113154476620 | Medium |
| PAT-0022 | FP-001 | Rapid Fund Movement | 211566492688 | High |
| PAT-0023 | FP-012 | High Velocity Transactions | 211566492688 | Medium |
| PAT-0024 | FP-029 | Balance Volatility | 211566492688 | Medium |
| PAT-0025 | FP-001 | Rapid Fund Movement | 24704559049070 | High |
| PAT-0026 | FP-012 | High Velocity Transactions | 24704559049070 | Medium |
| PAT-0027 | FP-013 | Round Value Behaviour | 24704559049070 | Medium |
| PAT-0028 | FP-029 | Balance Volatility | 24704559049070 | Medium |
| PAT-0029 | FP-001 | Rapid Fund Movement | 258082779154 | High |
| PAT-0030 | FP-015 | Repeated Beneficiary | 258082779154 | High |
| PAT-0031 | FP-029 | Balance Volatility | 258082779154 | Medium |
| PAT-0032 | FP-001 | Rapid Fund Movement | 280442153117 | High |
| PAT-0033 | FP-012 | High Velocity Transactions | 280442153117 | Medium |
| PAT-0034 | FP-013 | Round Value Behaviour | 280442153117 | Medium |
| PAT-0035 | FP-015 | Repeated Beneficiary | 280442153117 | High |
| PAT-0036 | FP-029 | Balance Volatility | 280442153117 | Medium |
| PAT-0037 | FP-001 | Rapid Fund Movement | 43920027363506 | High |
| PAT-0038 | FP-012 | High Velocity Transactions | 43920027363506 | Medium |
| PAT-0039 | FP-013 | Round Value Behaviour | 43920027363506 | Medium |
| PAT-0040 | FP-029 | Balance Volatility | 43920027363506 | Medium |

## Evidence And Limitations

Every JSON finding references evidence IDs tied to source file, page/sheet/row or line, account, date, amount, and narration. PDF-only transactions were parsed from embedded text without OCR; scanned or poorly extracted lines are therefore lower-confidence. Duplicate source documents were processed independently and then deduplicated for global analysis.

## Conclusion

The dataset shows multiple accounts with short-lived balances, repeated digital transfers, and post-credit ATM cash-outs. These behaviours justify focused human review, especially for accounts categorized as High or Critical Risk. Findings remain conservative: unmatched single-sided transfers are treated as medium confidence unless another dataset statement confirms the counterparty side.
