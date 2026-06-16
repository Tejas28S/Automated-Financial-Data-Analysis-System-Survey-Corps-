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
* Narration
* Debit
* Credit
* Balance
* Account ID
* Bank Name

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

Currently in **Stage 1 — System Design** phase. Implementation begins with the extraction module followed by the 25 detection cases, report generator, RAG chatbot, and frontend integration.
