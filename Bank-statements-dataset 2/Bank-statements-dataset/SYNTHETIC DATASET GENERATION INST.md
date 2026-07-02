# SYNTHETIC DATASET GENERATION INSTRUCTIONS
# For: Analysis Phase Testing — Financial Crime Investigation Platform
# Read every word of this file before writing a single line of code.

---

## WHAT YOU ARE DOING AND WHY

You are generating synthetic bank transaction CSV files that will be used to test
the analysis phase of a financial crime investigation platform built for CID/police.

The analysis phase takes one single merged CSV file as input (output of the extraction
phase) and must detect fraud patterns in it. To test whether the analysis code works
correctly, you need synthetic CSV files where you KNOW exactly which transactions are
fraudulent and which are innocent.

You will generate:
- 33 synthetic CSV files total (3-4 files per pattern, 10 patterns)
- 1 ground truth file (tells exactly which transactions are fraud in which file)
- Organized into 10 folders, one per pattern

These CSV files must look EXACTLY like real extracted bank statement data.
The most important thing is the NARRATION column. Read that word again.
THE NARRATION IS THE MOST IMPORTANT COLUMN. Everything else can be approximate.
The narration must look exactly like a real Indian bank transaction narration.
You will learn narration formats by reading the real dataset files first.
The narration is what connects accounts. The narration reveals the pattern.
Never invent a fake narration format. Always replicate real bank narration formats.
The narration is how investigators trace money. Get the narration right.
If the narration is wrong, the entire synthetic dataset is useless.
The narration format must match the bank named in Bank_Name column exactly.
Narration is the soul of this dataset. Treat it as the highest priority.
Every narration must look like it came from a real Indian bank statement.

---

## STEP 1 — READ THE REAL DATASET FIRST (MANDATORY)

Before generating anything, you must read and analyse the real bank statement files.

**Location of real files:**
```
Bank statement/primary/     ← read all files here
Bank statement/secondary/   ← read all files here
```

**What to extract from each real file:**

For every unique bank you find across all 162 files, record:
1. The exact narration format for each transaction type (UPI, NEFT, IMPS, RTGS, ATM, POS, CASH, INTEREST, REVERSAL, CHEQUE)
2. The IFSC code format used by that bank
3. The account number format (how many digits, any prefix)
4. How the bank writes the account holder name (ALL CAPS, Title Case, etc.)
5. Any unique narration patterns or prefixes that bank uses

**Store your learnings as a mental template like this:**

```
BANK: Indian Bank
UPI narration: BY TRANSFER UPI/[12digit_ref]/Payment from PhonePe XXXXX[last5_phone]/[upi_handle]@[suffix] [IFSC]/[SENDER_NAME] TRANSFER FROM [phone] - BY TRANSFER UPI/[ref]/Payment from PhonePe -
UPI credit narration: BY UPI CREDIT UPI/[ref]/Payment from PhonePe XXXXX[last5]/[handle]@[suffix] [IFSC]/[NAME] - BY UPI CREDIT UPI/[ref]/Payment from PhonePe -
NEFT narration: NEFT CR-[ref]-[sender_name]-[sender_bank]-[sender_ifsc]
IMPS narration: IMPS/[ref]/[sender_name]/[sender_bank]
ATM narration: ATM WDL [location_code]/[city]/[date_ref]
INTEREST: CREDIT INTEREST -
IFSC format: IDIB000XXXX
Account number: 10 digits
```

Do this for EVERY bank found in the 162 files. Do not skip any bank.
The narration templates you learn here are what you will use during generation.
This step is not optional. The quality of narrations depends entirely on this step.

---

## STEP 2 — UNDERSTAND THE EXACT CSV SCHEMA

Every synthetic CSV file you generate must have EXACTLY these 18 columns in this order:

```
account_number, account_holder, ifsc_code, Date, Time, Narration,
Transaction_ID, Reference_Number, Transaction_Reference, Cheque_Number,
Debit, Credit, Balance, Transaction_Type, Bank_Name, txn_id,
duplicate_of, is_reversed
```

### Column Rules — Read Each One Carefully

**account_number**
- Fake but realistic. Match the digit count of that bank's real account numbers.
- Example: Indian Bank uses 10 digits → use 10 digit fake number like 8823451209
- SBI uses 11 digits → 98234512001
- HDFC uses 14 digits → 50100234512345
- Never reuse the same account number across different account holders.
- Same account holder always has the same account number throughout a file.

**account_holder**
- Fake Indian names. Use realistic South Indian, North Indian, mixed names.
- Format must match the bank's style from real data (ALL CAPS if real data shows ALL CAPS).
- Examples: RAVI KUMAR SHARMA, PRIYA VENKATESH, MOHAMMED SALIM, LAKSHMI DEVI K
- Never use celebrity names, politician names, or obviously fake names like "Test User".

**ifsc_code**
- Use the real IFSC format for that bank but with a fake branch code.
- Format: 4 letter bank code + 0 + 6 alphanumeric branch identifier
- HDFC Bank: HDFC0XXXXXX → HDFC0009234
- SBI: SBIN0XXXXXX → SBIN0034521
- Axis Bank: UTIB0XXXXXX → UTIB0023451
- Indian Bank: IDIB000XXXX → IDIB000M987
- ICICI Bank: ICIC0XXXXXX → ICIC0034512
- PNB: PUNB0XXXXXX → PUNB0034521
- Bank of Baroda: BARB0XXXXXX → BARB0034521
- Canara Bank: CNRB0XXXXXX → CNRB0034521
- Use the actual bank code prefix from real IFSC codes you observed in the dataset.

**Date**
- Format: DD/MM/YYYY (this is the standard found in the sample CSV)
- Must be chronologically sequential within each account's transactions.
- Span at least 3-6 months of transaction history per account.
- Pattern transactions must happen within realistic time windows (see pattern rules).

**Time**
- Leave empty string for most rows (matches real data where Time is often blank).
- Occasionally add HH:MM:SS for some rows (some banks include it).

**Narration**
- THIS IS THE MOST IMPORTANT COLUMN. Read this section 10 times.
- The narration must exactly replicate the format of the bank named in Bank_Name.
- You learned the narration formats in Step 1 from real files. Use those formats here.
- Never invent a narration format that you did not see in the real dataset.
- The narration must contain real-looking UPI handles, IFSC codes, reference numbers.
- UPI handles must follow real patterns: name@ybl, name@axl, name@upi, name@okhdfcbank, number@paytm, number@ibl
- NEFT narrations must contain sender IFSC, sender name, reference number.
- IMPS narrations must contain reference number and sender name.
- ATM narrations must contain location-like codes.
- For fraud pattern transactions, the narration must show the money trail — it must contain
  the counterparty account reference so the analysis phase can build the entity graph.
- The narration is how the analysis phase knows Account A sent money to Account B.
  If the narration does not contain counterparty information, pattern detection fails.
- Narration is the most critical field. Every narration must look authentic.
- Always include reference numbers that match Transaction_Reference column.
- Narration format must match Bank_Name. Indian Bank narration ≠ HDFC narration.
- The narration reveals the story. Make it tell the right story.

**Transaction_ID**
- Leave empty for most rows (many banks do not provide this).
- Add a numeric ID for some rows where real data shows it.

**Reference_Number**
- Leave empty for most rows.
- For NEFT and RTGS, add the UTR number here (format: [IFSC_prefix][YYYYMMDD][sequence]).
- Example UTR: HDFC0250114000123

**Transaction_Reference**
- This is the reference number extracted from inside the narration string.
- For UPI: the 12-digit UPI reference number from narration.
- For NEFT: the UTR number.
- For IMPS: the IMPS reference number.
- For ATM: leave empty.
- Must match the reference number visible inside the Narration column.
- Example: if narration says UPI/350806131061/... then Transaction_Reference = 350806131061

**Cheque_Number**
- Leave empty except for cheque transactions.
- For cheque transactions: 6-digit cheque number.

**Debit**
- Amount debited (money going OUT of account). Float with 1 decimal place.
- If transaction is a credit, Debit = 0.0
- Never leave blank. Use 0.0 when no debit.

**Credit**
- Amount credited (money coming IN to account). Float with 1 decimal place.
- If transaction is a debit, Credit = 0.0
- Never leave blank. Use 0.0 when no credit.

**Balance**
- Running balance after this transaction.
- Must be mathematically correct: previous_balance - debit + credit = this_balance
- Start each account with a realistic opening balance (5000 to 500000).
- Balance must never go negative (unless it is an overdraft account, avoid this).
- Balance chain must be unbroken throughout the account's transaction history.

**Transaction_Type**
- Exactly one of: `debit` or `credit` (lowercase)
- debit = money went OUT (Debit column has value, Credit = 0.0)
- credit = money came IN (Credit column has value, Debit = 0.0)

**Bank_Name**
- The full name of the bank as it appears in the real dataset.
- Examples from real data: Indian Bank, State Bank of India, HDFC Bank, Axis Bank
- Must be consistent for all rows of the same account.
- Must match the IFSC code prefix.

**txn_id**
- Format: [account_number]_[6digit_sequence]
- Example: 8823451209_000000, 8823451209_000001, 8823451209_000002
- Must be unique across the entire CSV file.
- Sequence resets per account starting from 000000.

**duplicate_of**
- Empty string for normal transactions.
- For duplicate transactions: put the txn_id of the original transaction here.
- Example: if 8823451209_000005 is a duplicate of 8823451209_000002, then
  duplicate_of = 8823451209_000002 for row 000005.

**is_reversed**
- Boolean: True or False (Title case, not TRUE/FALSE or true/false)
- False for normal transactions.
- True for reversal transactions (failed UPI that was credited back, etc.)

---

## STEP 3 — THE 10 FRAUD PATTERNS TO EMBED

For each pattern, you will create 3-4 CSV files. Each CSV file must:
- Have minimum 4 accounts (maximum 8 accounts)
- Have at least 1 innocent account that has NO connection to the fraud
- Have at least 1 partially connected account (receives from fraud account but is not itself committing fraud)
- Embed the fraud pattern among normal everyday transactions
- Have minimum 80 transactions per account (so patterns are hidden, not obvious)
- Have at least 200 transactions per CSV file total

---

### PATTERN 1 — ROUND TRIP TRANSACTIONS
**Folder:** `synthetic_data/pattern_01_round_trip/`
**Files:** 3 CSV files

**What it is:**
Money leaves Account A, goes through 1-3 intermediary accounts, and returns to Account A
or a closely related account. The money goes in a circle to simulate business activity.

**How to embed it:**
- Account A sends a large amount to Account B (narration: NEFT/UPI transfer)
- Account B sends similar amount (minus small deduction) to Account C within 7 days
- Account C sends similar amount back to Account A within another 7 days
- Total round trip time: 7 to 21 days
- Amount returned: 85% to 98% of original (small cuts taken at each hop)
- Do this 3-5 times across the date range with different amounts

**Narration must show:**
- A→B: NEFT transfer with B's account reference in narration
- B→C: NEFT or UPI transfer with C's account reference
- C→A: NEFT transfer with A's account reference
- The counterparty account number or name must appear in the narration

**Innocent accounts in this file:** 1-2 accounts doing normal salary, UPI payments, bill payments

**What analysis must find:**
- Bidirectional flow between same account pairs within 21 days
- Amount returned within 15% of original

---

### PATTERN 2 — TRANSIT / LAYERING ACCOUNTS
**Folder:** `synthetic_data/pattern_02_transit_layering/`
**Files:** 3 CSV files

**What it is:**
Account B receives money from Account A and immediately sends it out to Account C.
Account B is a pass-through. It keeps almost nothing. This is used to hide the origin of money.

**How to embed it:**
- Account A sends large amounts to Account B
- Within 1-3 days, Account B sends 90-97% of received amount to Account C
- Account B's total_debit / total_credit ratio is above 0.85 (high throughput)
- Account B never accumulates more than 15% of what it receives
- Do this for 5-8 cycles over 3-4 months

**Narration must show:**
- A→B: NEFT/RTGS with reference showing A's details
- B→C: NEFT/UPI with reference showing B sending to C
- B should have almost no other transactions (no salary, no retail purchases — only this pass-through activity)

**Innocent accounts:** 1-2 accounts with normal salary credits, EMI debits, grocery UPI payments

---

### PATTERN 3 — ACCUMULATION ACCOUNT
**Folder:** `synthetic_data/pattern_03_accumulation/`
**Files:** 3 CSV files

**What it is:**
One account receives money from many different sources (10+ different senders) but rarely
sends money out. This is the destination account where stolen/fraudulent money collects.

**How to embed it:**
- Account D receives credits from 5-8 different accounts
- Account D's total outgoing is less than 20% of total incoming
- Account D makes only occasional small withdrawals (ATM, small UPI)
- The incoming credits arrive from multiple different banks, multiple different senders
- Large accumulation of balance over time

**Narration must show:**
- Multiple different senders in credit narrations (different UPI handles, different NEFT senders)
- Rare debit narrations (ATM withdrawal, small UPI to merchant)
- Balance keeps growing with each credit

**Innocent accounts:** 1 account that sends money to D as a legitimate business payment
(but D never sends anything back — making D suspicious)

---

### PATTERN 4 — STRUCTURING / SMURFING
**Folder:** `synthetic_data/pattern_04_structuring/`
**Files:** 4 CSV files

**What it is:**
An account makes multiple transactions just below ₹1,00,000 on the same day or consecutive
days to avoid banking reporting thresholds. Each individual transaction looks small but
the total is large.

**How to embed it:**
- Account C makes 4-8 NEFT/UPI transfers on the same day
- Each transfer is between ₹85,000 and ₹99,500
- All go to different accounts (different beneficiaries)
- Total sent that day: ₹4,00,000 to ₹7,00,000
- Do this on 3-5 different dates spread across the transaction history
- Mix with normal days where Account C makes regular small transactions

**Narration must show:**
- Multiple NEFT/UPI narrations on same date
- Each showing a different beneficiary name and account
- Amounts just below 1 lakh visible in Debit column

**Innocent accounts:** Receivers of these transfers who are not themselves suspicious
(they received one payment and that is all — they are unwitting recipients)

---

### PATTERN 5 — BURST ACTIVITY
**Folder:** `synthetic_data/pattern_05_burst_activity/`
**Files:** 3 CSV files

**What it is:**
An account is dormant or very low activity for months. Then suddenly it processes a very
large number of transactions and large total value in a short window (1-2 weeks). Then
goes back to low activity. Classic mule account behaviour.

**How to embed it:**
- Months 1-3: Account E has 3-5 transactions per month, small amounts (salary, bills)
- Month 4 week 2-3: Account E suddenly has 80-150 transactions in 11 days, total value 50x normal
- Month 5: Account E back to 3-5 transactions per month
- The burst period transactions are all UPI/NEFT transfers to/from fraud accounts
- Outside burst: normal salary, utility bill, grocery, ATM transactions

**Narration must show:**
- Pre-burst: salary credit (NEFT from employer), utility bill debit (UPI to BESCOM/BSNL etc.)
- Burst period: rapid UPI and NEFT transfers with different counterparties each day
- Post-burst: back to normal salary and bill pattern

---

### PATTERN 6 — DUPLICATE TRANSACTIONS
**Folder:** `synthetic_data/pattern_06_duplicates/`
**Files:** 3 CSV files

**What it is:**
The same transaction appears twice in the statement. Either same UTR number appears twice,
or same amount + same date + similar narration appears twice. This can indicate
system errors or deliberate double-crediting fraud.

**How to embed it:**
- Create 5-8 pairs of duplicate transactions in each CSV file
- Type 1 exact duplicate: same date, same amount, same narration, same Transaction_Reference
  → duplicate_of column filled with original txn_id
- Type 2 near-duplicate: same date, same amount, narration slightly different
  → duplicate_of column filled with original txn_id
- Type 3 reversal pair: debit followed by equal credit with REVERSAL in narration
  → is_reversed = True for the credit row

**Narration must show:**
- For exact duplicates: identical narration strings
- For near-duplicates: narration differs only in a reference number or timestamp
- For reversals: original debit narration + reversal credit narration like
  "REVERSAL OF UPI/[ref]/[details]" or "UPI REFUND/[ref]/[original_details]"

**The duplicate_of and is_reversed columns are the ground truth for this pattern.**

---

### PATTERN 7 — MONEY TRAIL (FUND DIVERSION)
**Folder:** `synthetic_data/pattern_07_money_trail/`
**Files:** 3 CSV files

**What it is:**
A specific large credit arrives in an account. That money is then spent/diverted across
multiple debits until the account returns to approximately its pre-credit balance.
The analysis must trace where that specific credited money went.

**How to embed it:**
- Account F receives a large credit (e.g. ₹5,00,000) via NEFT on Day 1
- Before this credit, balance was e.g. ₹12,000
- After credit, balance is ₹5,12,000
- Over the next 10-15 days, Account F makes multiple debits:
  * ₹1,50,000 to Account G (Day 2)
  * ₹2,00,000 to Account H (Day 4)
  * ₹80,000 to Account I (Day 7)
  * ₹50,000 ATM withdrawal (Day 9)
  * ₹20,000 UPI to merchants (Day 10-12, small amounts)
- By Day 15, balance is back near ₹12,000 (the money is gone)
- This shows diversion of the credited funds

**Narration must show:**
- Day 1 credit: NEFT from sender with full sender details
- Day 2-15 debits: each narration shows where money went (NEFT to beneficiary name, UPI to handle)
- The FIFO logic: the first credit's money was spent first in the debits that follow

**For this pattern, 2 CSV files should have 1 money trail each,
1 CSV file should have 3 separate money trails for different accounts.**

---

### PATTERN 8 — MULTIPLE SMALL CREDITS THEN LARGE DEBIT (AGGREGATION)
**Folder:** `synthetic_data/pattern_08_aggregation/`
**Files:** 3 CSV files

**What it is:**
An account receives many small credits from many different people over a period,
then makes one or two large outward transfers. Classic collection-then-transfer pattern
seen in scam operations where victims send small amounts to a collector account.

**How to embed it:**
- Account J receives 20-40 small credits over 2-3 weeks
- Credits range from ₹500 to ₹5,000 each
- Credits come from many different senders (different UPI handles, different banks)
- Then: 1-2 large NEFT transfers of the total accumulated amount to another account
- The number of unique senders >> number of unique receivers
- Do this pattern 2-3 times with different amounts

**Narration must show:**
- Credits: UPI received from many different handles (name@upi, number@paytm, etc.)
- Final debit: NEFT to one specific account with beneficiary details in narration

---

### PATTERN 9 — CIRCULAR FLOW (3-HOP CYCLE)
**Folder:** `synthetic_data/pattern_09_circular_flow/`
**Files:** 3 CSV files

**What it is:**
Money moves in a closed loop: A → B → C → A (or A → B → C → D → A).
Different from round trip in that there are 3 or 4 hops, not 2.
Used to make the money trail harder to follow.

**How to embed it:**
- Account A sends ₹X to Account B (Day 1)
- Account B sends ₹X*0.95 to Account C (Day 3)
- Account C sends ₹X*0.90 to Account A (Day 6)
- This completes one cycle. Do 3-4 cycles over the date range.
- For 4-hop variant: A → B → C → D → A

**Narration must show:**
- Each hop: NEFT narration with sending account's details visible in narration
- The chain must be traceable through narrations alone
- Each account's statement shows the inflow and outflow clearly

**Key difference from round trip:** Round trip is A→B→A (2 hops). Circular is A→B→C→A (3+ hops).

---

### PATTERN 10 — MIXED / COMBINED PATTERNS
**Folder:** `synthetic_data/pattern_10_combined/`
**Files:** 4 CSV files

**What it is:**
Real fraud cases involve multiple patterns simultaneously. These CSV files combine
2-3 patterns from the above list in one file. The analysis phase must detect all
present patterns simultaneously.

**Combination rules:**
- File 1: Round trip (pattern 1) + Structuring (pattern 4)
- File 2: Transit account (pattern 2) + Accumulation (pattern 3) + Burst activity (pattern 5)
- File 3: Money trail (pattern 7) + Aggregation (pattern 8)
- File 4: Circular flow (pattern 9) + Duplicates (pattern 6)

**Each file must have:**
- Minimum 5 accounts
- At least 1 fully innocent account
- All present patterns clearly detectable through narrations and transaction amounts
- Minimum 300 transactions total

---

## STEP 4 — INNOCENT ACCOUNT RULES

Every CSV file must have at least 1-2 fully innocent accounts. These accounts must:

- Show realistic daily life transactions: salary credit, EMI debit, electricity bill,
  mobile recharge, grocery UPI, fuel UPI, rent payment, insurance premium
- Have NO direct transaction with any fraud account
- Have normal balanced inflow/outflow (not 97% throughput, not massive accumulation)
- Show month-end salary patterns: large credit on 1st or last day of month
- Show monthly fixed debits: same amount on same date every month (EMI, rent, SIP)

**Realistic innocent narration examples (replicate from real dataset formats):**

Salary credit: `NEFT CR-[UTR]-[EMPLOYER NAME]-[EMPLOYER BANK]-[EMPLOYER IFSC]`
Electricity bill: `UPI/[ref]/BESCOM/[upi_handle]` or `NACH DR-[mandate]-BESCOM-[ref]`
Mobile recharge: `UPI/[ref]/AIRTEL/airtel.[number]@airtel`
Grocery: `UPI/[ref]/DMart/[ref]`
ATM: `ATM WDL [location]/[city]`
EMI: `NACH DR-[ref]-[BANK NAME] EMI-[loan_account]`
Interest credit: `CREDIT INTEREST -` or `INT CR/[quarter]`

---

## STEP 5 — ACCOUNT DESIGN RULES

### Naming convention for accounts within a CSV file:

When a fraud pattern requires Account A to send to Account B and Account B sends to
Account C — all three accounts MUST appear in the same CSV file. This is because the
extraction phase merges all statements into one CSV. So one synthetic CSV = one merged
output from a set of bank statements.

### Account linking rules:

When Account A sends money to Account B:
- Account A's row: Transaction_Type=debit, Debit=[amount], narration shows B's details
- Account B's row: Transaction_Type=credit, Credit=[amount], narration shows A's details
- The UTR/reference number in Account A's debit narration MUST match the reference
  in Account B's credit narration (this is how analysis phase identifies linked transactions)
- The date must be the same (NEFT/IMPS) or next day (RTGS cutoff timing)

### Realistic account balance rules:

- Each account must start with a believable opening balance
- Fraud accounts: tend to start lower, spike during fraud, drop after
- Innocent accounts: steady growth pattern with salary, steady decrease with expenses

---

## STEP 6 — FILE NAMING AND FOLDER STRUCTURE

Create this exact folder structure:

```
synthetic_data/
├── pattern_01_round_trip/
│   ├── rt_case_01.csv
│   ├── rt_case_02.csv
│   └── rt_case_03.csv
├── pattern_02_transit_layering/
│   ├── tl_case_01.csv
│   ├── tl_case_02.csv
│   └── tl_case_03.csv
├── pattern_03_accumulation/
│   ├── ac_case_01.csv
│   ├── ac_case_02.csv
│   └── ac_case_03.csv
├── pattern_04_structuring/
│   ├── st_case_01.csv
│   ├── st_case_02.csv
│   ├── st_case_03.csv
│   └── st_case_04.csv
├── pattern_05_burst_activity/
│   ├── ba_case_01.csv
│   ├── ba_case_02.csv
│   └── ba_case_03.csv
├── pattern_06_duplicates/
│   ├── dup_case_01.csv
│   ├── dup_case_02.csv
│   └── dup_case_03.csv
├── pattern_07_money_trail/
│   ├── mt_case_01.csv
│   ├── mt_case_02.csv
│   └── mt_case_03.csv
├── pattern_08_aggregation/
│   ├── agg_case_01.csv
│   ├── agg_case_02.csv
│   └── agg_case_03.csv
├── pattern_09_circular_flow/
│   ├── cf_case_01.csv
│   ├── cf_case_02.csv
│   └── cf_case_03.csv
├── pattern_10_combined/
│   ├── combo_case_01.csv
│   ├── combo_case_02.csv
│   ├── combo_case_03.csv
│   └── combo_case_04.csv
└── ground_truth/
    ├── GROUND_TRUTH.csv
    └── GROUND_TRUTH_SUMMARY.md
```

---

## STEP 7 — GROUND TRUTH FILES (MANDATORY)

You must generate two ground truth files after generating all 33 CSV files.

### GROUND_TRUTH.csv

This file has exactly these columns:

```
csv_file, txn_id, account_number, date, amount, pattern_type, pattern_role, is_fraud, notes
```

**Column definitions:**

**csv_file:** Relative path to the CSV file. Example: `pattern_01_round_trip/rt_case_01.csv`

**txn_id:** The txn_id of this specific transaction from the synthetic CSV.

**account_number:** The account number of this transaction.

**date:** The date of this transaction (DD/MM/YYYY).

**amount:** The debit or credit amount (whichever is non-zero).

**pattern_type:** One of: round_trip, transit_layering, accumulation, structuring,
burst_activity, duplicate, money_trail, aggregation, circular_flow, combined, innocent

**pattern_role:** What role this transaction plays. Examples:
- `round_trip_sender` — Account A sending money out in the round trip
- `round_trip_receiver` — Account A receiving the returned money
- `round_trip_intermediary_hop1` — Account B in the chain
- `transit_passthrough` — The transit account passing money through
- `accumulation_deposit` — A credit into the accumulation account
- `structuring_transfer` — One of the multiple small transfers
- `burst_active_period` — Transaction during the burst window
- `duplicate_original` — The original transaction
- `duplicate_copy` — The duplicate of the original
- `reversal_original` — The transaction that was reversed
- `reversal_credit` — The reversal credit
- `money_trail_source_credit` — The large credit that starts the trail
- `money_trail_diversion` — The debits that follow the source credit
- `aggregation_collection` — Small incoming credits in aggregation pattern
- `aggregation_transfer` — The large outward transfer after collection
- `circular_hop1`, `circular_hop2`, `circular_hop3` — Hops in circular flow
- `innocent` — Normal transaction from innocent account

**is_fraud:** True or False

**notes:** Short human-readable explanation. Example:
"Part of round trip cycle 2 of 3. Account 8823451209 sent ₹1,50,000 to 9934521098 on 15/03/2024"

Every fraud transaction in every CSV file must have a row in GROUND_TRUTH.csv.
Every innocent account's transactions must also be listed as innocent in ground truth.

### GROUND_TRUTH_SUMMARY.md

A human-readable summary with this structure:

```markdown
# Ground Truth Summary

## Total Statistics
- Total CSV files generated: 33
- Total transactions (all files): [count]
- Total fraud transactions: [count]
- Total innocent transactions: [count]
- Fraud percentage: [%]

## Per-Pattern Summary

### Pattern 1: Round Trip
- Files: rt_case_01.csv, rt_case_02.csv, rt_case_03.csv
- Accounts involved in fraud: [list of account numbers]
- Innocent accounts: [list of account numbers]
- Total round trip cycles embedded: [count]
- Amounts involved: ₹[min] to ₹[max]
- Detection signal: [what the analysis code should find]

[Repeat for all 10 patterns]

## Per-File Summary

### rt_case_01.csv
- Accounts: [list with role: fraud/innocent/partial]
- Pattern: Round trip
- Cycles: 3
- Innocent accounts: [list]
- Key transactions to detect: [txn_ids of the round trip transactions]

[Repeat for all 33 files]
```

---

## STEP 8 — QUALITY CHECKS BEFORE FINISHING

Before you declare the dataset complete, verify every file passes these checks:

**Check 1 — Balance chain integrity:**
For every account in every CSV file, walk the transactions in chronological order.
Verify: starting_balance + credit - debit = next_balance for every row.
If any row breaks the chain, fix it before finishing.

**Check 2 — Narration authenticity:**
For every row, verify the narration format matches the Bank_Name.
Indian Bank narration must not look like HDFC narration and vice versa.
The narration formats you learned in Step 1 must be applied consistently.

**Check 3 — Transaction_Reference consistency:**
For every row that has a Transaction_Reference value, verify that same reference
number appears inside the Narration string of that row.

**Check 4 — Cross-account UTR matching:**
For every NEFT/RTGS transfer from Account A to Account B:
The UTR reference in Account A's debit narration must match
the UTR reference in Account B's credit narration.
This is how the analysis phase links transactions across accounts.

**Check 5 — Pattern detectability:**
For each pattern, manually trace through the transactions of the fraud accounts.
Can you see the pattern clearly by reading the narrations and amounts?
If you cannot see it clearly, the analysis code will not find it either.

**Check 6 — Ground truth completeness:**
Every fraud transaction in every CSV file must have a corresponding row in GROUND_TRUTH.csv.
Count fraud transactions in all CSV files. Count rows in GROUND_TRUTH.csv with is_fraud=True.
These two counts must match exactly.

**Check 7 — Minimum transaction counts:**
Every account must have minimum 80 transactions.
Every CSV file must have minimum 200 transactions total.
Pattern 10 combined files must have minimum 300 transactions.

**Check 8 — Innocent account isolation:**
Innocent accounts must have zero transactions with fraud accounts.
If any innocent account's narration references a fraud account number or name, fix it.

---

## STEP 9 — WHAT NOT TO DO

- Do NOT invent narration formats. Only use formats learned from the real dataset in Step 1.
- Do NOT reuse the same account number for different account holders.
- Do NOT create obvious fraud (10 transfers all exactly ₹99,000 with no normal transactions around them).
- Do NOT use celebrity names or obviously fake names.
- Do NOT make all fraud accounts from the same bank. Use diverse banks.
- Do NOT forget to fill the duplicate_of column for duplicate transactions.
- Do NOT leave the is_reversed column blank. It must be True or False for every row.
- Do NOT create patterns where amounts are mathematically impossible (balance goes negative).
- Do NOT hardcode the same narration string for multiple transactions. Each narration must be unique even if the format is the same (different reference numbers each time).
- Do NOT create CSV files where the pattern is in the first 10 rows. Embed patterns at random positions among normal transactions.

---

## EXECUTION ORDER

Execute these steps in this exact order. Do not skip or reorder.

1. Read all files in `Bank statement/primary/` and `Bank statement/secondary/`
2. Build narration format templates for every bank found
3. Generate pattern_01_round_trip files (3 files)
4. Generate pattern_02_transit_layering files (3 files)
5. Generate pattern_03_accumulation files (3 files)
6. Generate pattern_04_structuring files (4 files)
7. Generate pattern_05_burst_activity files (3 files)
8. Generate pattern_06_duplicates files (3 files)
9. Generate pattern_07_money_trail files (3 files)
10. Generate pattern_08_aggregation files (3 files)
11. Generate pattern_09_circular_flow files (3 files)
12. Generate pattern_10_combined files (4 files)
13. Run all 8 quality checks on every file
14. Generate GROUND_TRUTH.csv
15. Generate GROUND_TRUTH_SUMMARY.md
16. Report final statistics

---

## FINAL REMINDER

The narration is the most important column in this entire dataset.
The narration is what the analysis phase uses to build the entity graph.
The narration is what connects Account A to Account B.
The narration is how the pattern detection code knows money moved between accounts.
The narration must look exactly like a real Indian bank narration.
The narration format must match the bank name in Bank_Name column.
The narration must contain the counterparty reference for every inter-account transfer.
The narration is not a description. It is a structured string with embedded references.
Learn narration formats from the real 162 files in Step 1. Apply them everywhere.
If the narrations are wrong, the ground truth is useless, the testing is useless,
and the analysis phase cannot be validated. The narration is everything.