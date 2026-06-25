# Automated Financial Data Analysis System — Survey Corps

> Built for **CIDECODE Hackathon 2026** | Problem Statement 1
> Organised by **CCITR – CID Karnataka** in association with **PES University, Bengaluru**

---

## Team — Survey Corps

* Tejas M S
* Nikhil Santosh
* Tejas S
* Vinayak G K

---

## Problem Statement

Financial cybercrime investigations in India are severely hampered by the manual effort required to analyse bank statements. CID investigators regularly receive statements from multiple suspects, across different banks, in inconsistent formats — PDF, scanned images, and Excel sheets. The current process requires manually reviewing thousands of transactions, which takes weeks and often misses critical hidden connections between accounts.

### Core Challenges

* Every bank produces statements in a different layout, making unified manual analysis impractical
* Cyber-fraud cases routinely involve hundreds of accounts and thousands of transactions
* Money-mule chains and hawala networks span multiple accounts and are invisible without cross-account analysis
* Manually compiling a court-ready forensic report from raw findings is slow and error-prone

---

## Proposed Solution

The **Multi-Accused Cross-Account Investigation Engine** is an AI-powered platform that combines:

* **OCR** to read scanned and photographed bank statements
* **Large Language Models (LLMs)** to identify document structure, generate dynamic thresholds, explain findings, and power the chatbot
* **Retrieval-Augmented Generation (RAG)** to let investigators query the actual transaction data in natural language

The guiding design principle is that every numeric or structural finding is produced by **deterministic code** (pandas, NetworkX, scikit-learn) so that it is reproducible and legally defensible, while the LLM is used only where it provides genuine value over code — reading the investigator's natural-language case brief, generating context-appropriate thresholds, and writing plain-English explanations of what the mathematics found. This separation is what makes the tool's output trustworthy enough for an investigation file.

---

## Key Features

* **Multi-format ingestion** — accepts PDF (digital and scanned), Excel, CSV, DOCX, JPG and PNG files covering multiple suspect accounts in a single session
* **Investigator-guided analysis** — the investigator provides a natural-language case brief that contextualises every analysis step
* **25 fraud detection cases** across seven categories — round-tripping, layering, smurfing, hawala, mule chains, dormancy activation, velocity spikes, and more
* **Interactive money-flow graph** — visualises the entire suspect network showing who sent money to whom and where it accumulated
* **Composite suspicion scoring** — every account receives a score from 0–100 based on weighted findings from all triggered detection cases
* **Court-ready forensic reports** — automatically generated PDF and Excel reports with executive summary, detailed findings, and technical appendix
* **RAG-powered investigation chatbot** — lets the investigator query the actual uploaded transaction data in plain English

---

## How It Works

The system processes every case through four sequential phases.

### Phase 1 — Input

The investigator types the case brief and uploads all suspect statements.

### Phase 2 — Extraction

Every uploaded file, regardless of format, is converted into one standardised transaction table with columns:

* Date
* Time
* Narration
* Debit
* Credit
* Balance
* Account ID
* Bank Name

plus optional identifier columns carried through when a statement provides them — Transaction ID, Reference Number, Transaction Reference (parsed out of the narration), Cheque Number, and IFSC Code.

**Architecture — the Validation-Arbitrated Tiered Hybrid.** Cheap deterministic code does the work for every file it can handle; the LLM is used only where deterministic code genuinely cannot. The referee between the two is a **balance-reconciliation validator** — a format-agnostic arithmetic check (`previous balance + credit − debit = current balance`) that works for every bank on earth because all banks print a running balance. Each file exits at the cheapest tier the validator accepts:

```
Tier 0  Route + raw extract            (no LLM)
Tier 1  Metadata (local regex first; LLM only if regex finds nothing)
Tier 2  Cheap deterministic parse      (no LLM)
Tier 3  VALIDATE  → PASS → accept
Tier 4  LLM schema discovery on a small SAMPLE → re-parse all rows → re-validate
Tier 5  LLM full read (last resort)    → unparsed rows are FLAGGED, never dropped
```

Three guarantees the engine is built on:

* **No code path branches on a bank's name.** Bank identity is only ever data (a label in the output), never a condition — enforced by a build-guard test, so the engine generalises to unseen banks by construction.
* **The deterministic result is the source of truth.** An LLM tier replaces it only when it is *strictly better* by a combined reconciliation × completeness score, so an empty or weaker LLM response can never corrupt a good deterministic parse.
* **Nothing is ever silently dropped.** Any row that cannot be parsed or reconciled is written to `flagged_transactions.csv` with a reason; a per-file receipt records the tier reached, the reconciliation rate, and the LLM-call count.

A clean digital PDF or Excel/CSV file therefore costs **zero** transaction LLM calls — tokens scale with the number of genuinely hard documents, not with the number of rows — and all raw text is anonymised before any LLM call.

### Phase 3 — Analysis

25 detection cases run across seven categories:

#### Graph-Based (Cases 1–5)

* Round-trip detection
* Multi-hop layering
* Hub identification
* Isolated cluster detection
* Convergence point identification using NetworkX

#### Time-Based (Cases 6–10)

* Dwell time
* Dormancy activation
* Velocity spike
* Synchronised transactions
* Periodicity using pandas

#### Amount-Based (Cases 11–15)

* Structuring/smurfing
* Hawala matched pair
* FIFO money trail
* Cash withdrawal fragmentation
* Micro-transaction aggregation using pandas

#### Counterparty (Cases 16–19)

* Fan-in/fan-out ratio
* Single counterparty concentration
* Ghost beneficiary
* New counterparty spike using pandas

#### Narration (Cases 20–21)

* Blank/generic narration clustering
* Case-brief keyword matching using pandas and LLM

#### Statistical (Case 22)

* Isolation Forest anomaly detection using scikit-learn

#### LLM-Driven (Cases 23–25)

* Dynamic threshold generation
* Case-brief pattern matching
* Hypothesis generation

### Phase 4 — Output

Court-ready PDF and Excel reports are generated, the interactive graph is rendered, and the RAG chatbot becomes available.

---

## Technology Stack

| Layer                         | Technology                      |
| ----------------------------- | ------------------------------- |
| Data processing               | pandas, NumPy                   |
| Graph analysis                | NetworkX                        |
| Statistical anomaly detection | scikit-learn                    |
| OCR                           | Tesseract (pytesseract)         |
| Digital PDF extraction        | pdfplumber                      |
| LLM integration               | Claude / Groq API               |
| Vector store and RAG          | ChromaDB, sentence-transformers |
| Report generation             | ReportLab, openpyxl, matplotlib |
| Backend                       | Python, FastAPI                 |

---

## Synthetic Dataset

The repository includes a synthetic dataset with deliberately planted fraud patterns across seven case types, used as the accuracy benchmark during development:

```text
synthetic_dataset_full/
├── CASE_A_Admission_Bribe_Network/
├── CASE_B_Hawala_Operation/
├── CASE_C_Mule_Chain/
├── CASE_D_Govt_Scheme_Fraud/
├── CASE_E_Cyber_Fraud/
├── CASE_F_Loan_App_Fraud/
└── CASE_G_Blind_Audit/
```

---

## Folder Structure

```text
survey-corps/
│
├── synthetic_dataset_full/     # Synthetic case data for testing
├── extraction/                 # File parsing and standardisation modules
├── analysis/                   # 25 fraud detection cases (files added during build phase)
├── reporting/                  # PDF and Excel report generation
├── chatbot/                    # RAG-based investigation chatbot
├── storage/                    # Session data and LLM response cache
│   └── llm_cache/
├── outputs/                    # Generated reports and graphs
│   ├── reports/
│   └── graphs/
├── tests/                      # Test scripts
└── config/                     # Configuration and settings
```

---

## Development Status

**Phase 1 — Extraction: COMPLETE and validated.** The extraction engine (the Validation-Arbitrated Tiered Hybrid described above) is finished and validated against the full `original bank statements/` dataset (61 files spanning digital PDFs, Excel `.xls`/`.xlsx`, CSV and TXT, across many different banks and layouts). **52 files parse deterministically with zero LLM calls**; the remainder use the designed LLM fallback and are never corrupted — every row is accounted for as clean or flagged. The full engine passes its **43-test** suite, including the anti-overfitting build-guard that fails if any bank name is used in control flow. A complete work log lives in `phase1 final.md`.

**Next — Phase 2 (Analysis):** the 25 detection cases, the money-flow graph, composite suspicion scoring, the court-ready report generator, and the RAG investigation chatbot.
