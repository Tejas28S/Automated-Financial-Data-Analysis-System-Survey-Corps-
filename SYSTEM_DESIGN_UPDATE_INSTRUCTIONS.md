# SYSTEM_DESIGN_UPDATE_INSTRUCTIONS.md — How to Read the Old Design and Produce the New One

**Project:** Multi-Accused Cross-Account Investigation Engine
**Team:** Survey Corps · CIDECODE Hackathon 2026 (CID Karnataka / PES University)
**Task for Claude:** Read the existing (outdated) System Design Report, understand it fully, then produce an **updated System Design Report** that reflects the re-architected extraction phase. **Do not change any application code in this task — this is a documentation task only.**

---

## 1. Why this task exists

The original System Design (Version 1.0) was written *before* the extraction phase was built. During implementation, the extraction pipeline drifted away from that design and developed problems (two contradictory parsing strategies, high token usage, overfitting risk, a silent data-loss bug). The team has since agreed on a corrected architecture — the **Validation-Arbitrated Tiered Hybrid** — and the design document must now be brought in line with it.

Your job: read the old design, then write the new one so the document on file matches what the system actually does.

---

## 2. Inputs you must read first (in this order)

1. **`System_Design_Report_SurveyCorps.pdf`** — the OLD design (Version 1.0). Read it in full with the PDF reader (it is ~32 pages; read it in page ranges). Pay closest attention to:
   - Section 3 — System Architecture Overview (three layers, four phases)
   - Section 4 — Module-Level Design, especially **4.2 Extraction Module** (Components 1–5). *This is the part that is now outdated.*
   - Section 8 — Technology Stack
   - Section 9 — Data Storage & Data Flow
2. **`CHANGES_INSTRUCTIONS.md`** — the authoritative description of the NEW extraction architecture (the five tiers, the validator, the escalation ladder, the LLM interface, the anti-overfitting law). **This file is the source of truth for everything that changed.**
3. **`System_Design_Report_SurveyCorps_v2.pdf`** — a first draft of the updated design that already exists. Read it; either improve it or regenerate a better version. Do not assume it is perfect.
4. **`phase1.md`** — the implementation work log, for grounding (what the code actually does today).

Read once, thoroughly. Do not re-read the same file repeatedly — it wastes the token budget.

---

## 3. What to KEEP from the old design (do not rewrite these)

The following are still correct and must be carried into the new document, summarised faithfully:

- The overall vision and problem statement (Section 1).
- Functional and non-functional requirements (Section 2) — though note NFR-01 (performance) and the accuracy emphasis are now satisfied by the new architecture.
- The three-layer / four-phase framing (Section 3).
- The **Analysis phase** (Section 5 — 25 detection cases), **Reporting** (Section 6), and **RAG chatbot** (Section 7). These phases are **unchanged**; summarise them, do not redesign them.
- The technology stack (Section 8), with the addition of the provider-independent LLM interface.
- Security/privacy and scalability framing (Sections 10–11).

---

## 4. What to REPLACE (the outdated parts)

Rewrite the **Extraction Module** so it matches `CHANGES_INSTRUCTIONS.md`. Specifically, replace the old "five sequential components, LLM identifies columns once, pandas parses every row" description with the new model:

1. **The core philosophy** — Validation-Arbitrated Tiered Hybrid, and its three principles:
   - The LLM fills parameters; humans write the engine (the LLM never writes per-statement parsers/regex).
   - The balance math is the referee (`balance_prev ± amount = balance_curr`), a universal, bank-agnostic self-check.
   - Spend intelligence only where cheap methods provably fail.
2. **The Anti-Overfitting Law** — no bank-name branching; correctness proven on a blind set; parameters from schema, not hard-coded.
3. **The five-tier architecture** — Tier 0 raw extract → Tier 1 metadata → Tier 2 cheap deterministic parse → Tier 3 validate → Tier 4 schema discovery (LLM on a sample) → Tier 5 row repair (LLM on failing rows only) → flag the rest. Include the ASCII tier diagram.
4. **The Validator** — the four checks (balance reconciliation, debit/credit exclusivity, date validity, completeness), with worked examples, and the four tricky cases (newest-first, no opening balance, no balance column, wrapped narrations).
5. **The deterministic schema-driven engine** — one engine parameterised by a schema object.
6. **The escalation ladder** — with the named config thresholds (`ACCEPT_RECONCILE_RATE = 0.98`, `MIN_COMPLETENESS_RATIO = 0.90`, `BALANCE_TOLERANCE = 1.0`).
7. **The provider-independent LLM interface** — four functions, behind one module, so a local model can replace Groq by changing only that module.
8. **Cost/performance/local-model path** — tokens scale with documents, not rows.
9. **Output schema & storage** — `Date | Time | Narration | Debit | Credit | Balance | Account_ID | Bank_Name | IFSC_Code`, plus clean/flagged CSVs and the metadata receipt.
10. **A worked end-to-end example** — the same three-file scenario (digital PDF + Excel + phone photo) showing how each flows through the tiers and lands in one table.

Use the worked examples and exact numbers already written in `CHANGES_INSTRUCTIONS.md` — do not invent different ones.

---

## 5. Required structure of the new document

Produce these sections (titles may be refined, content must be present):

1. Purpose of This Revision (and that it supersedes Version 1.0)
2. What Changed Since Version 1.0 (a before/after table)
3. Design Goal & Guiding Principles
4. Core Philosophy — Validation-Arbitrated Tiered Hybrid
5. The Anti-Overfitting Law
6. Extraction Architecture — The Five Tiers (with diagram)
7. The Validator — The Heart of the System (worked examples + tricky cases)
8. The Deterministic Schema-Driven Engine
9. The Escalation Ladder (with config thresholds)
10. The LLM Interface (provider-independent / local-model-ready)
11. Worked End-to-End Example (PDF + Excel + Photo) with a sample output table
12. Output Schema & Storage
13. Privacy & Security
14. Cost, Performance & Local-Model Path
15. Testing & Accuracy Proof (blind set)
16. Unchanged Downstream Phases (Analysis, Reporting, RAG — summary only)
17. Conclusion

---

## 6. How to produce the PDF

- Generate the document as a **PDF** named `System_Design_Report_SurveyCorps_v2.pdf` (overwrite the existing draft if improving it).
- Use **ReportLab** (already in `requirements.txt`) via a small build script. Match the original's professional style: a cover page; navy section headings; purple sub-headings; readable body text; bordered tables with a header row; monospaced blocks for diagrams/schemas; and a page footer reading `Team Survey Corps  |  CIDECODE Hackathon 2026  |  System Design Report v2.0  |  Page N`.
- Keep diagrams as monospaced/preformatted ASCII (they render reliably in a PDF).
- After building, **verify** the PDF opens and has a sensible page count, then **delete the throwaway build script** so it is not committed.
- Mark the document clearly as **Version 2.0 — June 2026**, stating it supersedes Version 1.0.

---

## 7. Handling the old PDF

- Do **not** delete `System_Design_Report_SurveyCorps.pdf` (the v1.0 original) unless the team explicitly asks. The new document states it supersedes v1.0, so the history is preserved on purpose.
- If the team has confirmed deletion, remove only the old v1.0 file, never the new one.

---

## 8. Quality bar (you are done when all are true)

- The new PDF accurately describes the Validation-Arbitrated Tiered Hybrid and matches `CHANGES_INSTRUCTIONS.md` (no contradictions between the two documents).
- The extraction section no longer describes "LLM reads every row" or "one column map + pandas parses everything" as the design.
- The validator, the five tiers, the escalation thresholds, the provider-independent interface, and the anti-overfitting law are all present and explained in plain language a CID judge could follow.
- The unchanged downstream phases are summarised, not redesigned.
- The worked end-to-end example and the output schema are included.
- The PDF is valid, styled, versioned 2.0, and the build script is removed.
- **No application code was modified in this task.**

---

## 9. What NOT to do

- Do **not** modify any code in `extraction/`, `config/`, or elsewhere — this is a documentation task.
- Do **not** redesign the analysis, reporting, or chatbot phases — summarise them as-is.
- Do **not** invent new numbers or examples that conflict with `CHANGES_INSTRUCTIONS.md`.
- Do **not** describe any bank-specific logic as part of the design (it violates the Anti-Overfitting Law).
- Do **not** leave the throwaway PDF build script in the repository.
- Do **not** delete the new PDF; only the old v1.0 may be removed, and only if explicitly approved.

---

*End of SYSTEM_DESIGN_UPDATE_INSTRUCTIONS.md — Survey Corps · CIDECODE Hackathon 2026.*
