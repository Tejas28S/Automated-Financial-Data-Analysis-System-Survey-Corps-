"""
settings.py — Central configuration for the Survey Corps extraction pipeline.

This file is the single source of truth for every configurable value in the
entire extraction phase. All other files import constants from here instead
of hardcoding values themselves.

Why this matters: If CID Karnataka investigators need to change a threshold
(e.g., the confidence level for OCR quality), they change it in ONE place
here and the change automatically applies everywhere in the system.

Team: Survey Corps | CIDECODE Hackathon 2026 | CID Karnataka
"""

import os
import shutil
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from the .env file in the project root.
# This makes API keys available via os.getenv() without ever hardcoding them.
load_dotenv()

# ── Project Paths ────────────────────────────────────────────────────────────
# BASE_DIR is the root of the repository (two levels up from this file:
# config/settings.py → config/ → project root).
BASE_DIR = Path(__file__).resolve().parent.parent

# Directory where investigators upload bank statement files
UPLOAD_DIR = BASE_DIR / "uploads"

# Directory where all system outputs (reports, graphs) are saved
OUTPUT_DIR = BASE_DIR / "outputs"

# Sub-directory for generated PDF investigation reports
REPORTS_DIR = OUTPUT_DIR / "reports"

# Sub-directory for generated charts and network graphs
GRAPHS_DIR = OUTPUT_DIR / "graphs"

# Sub-directory where each extraction run persists its results so the team can
# open them without running any code (Problem 3). One folder per session:
#   outputs/extractions/<session_id>/clean_transactions.csv
#                                    /flagged_transactions.csv
#                                    /metadata.json
EXTRACTIONS_DIR = OUTPUT_DIR / "extractions"

# Directory for all persistent local storage (ChromaDB, LLM cache)
STORAGE_DIR = BASE_DIR / "storage"

# Cache directory: stores Groq API responses so we don't call the API
# repeatedly for the same document during testing
LLM_CACHE_DIR = STORAGE_DIR / "llm_cache"

# ChromaDB vector database directory: stores transaction embeddings for
# the RAG chatbot — runs entirely on local disk, no internet required
CHROMADB_DIR = STORAGE_DIR / "chromadb"

# Create all required directories if they do not already exist.
# parents=True means it will also create any missing parent directories.
# exist_ok=True means it will not raise an error if the directory already exists.
for directory in [
    UPLOAD_DIR,
    OUTPUT_DIR,
    REPORTS_DIR,
    GRAPHS_DIR,
    EXTRACTIONS_DIR,
    STORAGE_DIR,
    LLM_CACHE_DIR,
    CHROMADB_DIR,
]:
    directory.mkdir(parents=True, exist_ok=True)

# ── API Keys — THREE Groq keys, one provider (see INSTRUCTIONS.md §7) ─────────
# We deliberately use three separate Groq keys, split by phase, so each key keeps
# its own free-tier rate-limit quota and nothing throttles mid-demo:
#
#   GROQ1  → Extraction · column identification   (this phase — text model)
#   GROQ2  → Extraction · blurry-image OCR fallback (this phase — vision model)
#   GROQ3  → Analysis + report generation          (a LATER phase — NOT used here)
#
# These are read from the git-ignored .env file and are NEVER hardcoded.
# If a key is absent it is None here; the module that needs it raises a clear,
# readable error at startup via require_extraction_keys() below — it never
# crashes silently mid-run.
GROQ1_KEY = os.getenv("GROQ1")  # column identification (column_identifier.py)
GROQ2_KEY = os.getenv("GROQ2")  # vision OCR fallback   (extractor_ocr.py)
# NOTE: GROQ3 is intentionally NOT loaded here. It belongs to the analysis phase.
# Loading it in extraction code would blur the phase split the keys exist to keep.

# ── Tesseract OCR ────────────────────────────────────────────────────────────
# Where is the Tesseract executable? We deliberately do NOT hardcode one machine's
# path, because a path copied from another teammate's computer (e.g. a macOS
# Homebrew path sitting in a shared .env, opened on a Windows laptop) silently
# breaks OCR and forces every scanned page onto the paid Groq Vision fallback.
# Instead we RESOLVE it at startup: an explicit TESSERACT_CMD in .env wins ONLY if
# that file actually exists on THIS machine; otherwise we look on the PATH and in
# the standard install locations for Windows / macOS / Linux. That is what lets the
# OCR "just work" on any teammate's machine without editing code or the .env.
def _resolve_tesseract_cmd() -> str:
    """Finds the Tesseract binary; returns the first candidate that exists on disk."""
    candidates = []
    env_cmd = os.getenv("TESSERACT_CMD", "").strip().strip('"')
    if env_cmd:
        candidates.append(env_cmd)             # explicit override — used only if real
    on_path = shutil.which("tesseract")         # an install already on the PATH
    if on_path:
        candidates.append(on_path)
    local_appdata = os.getenv("LOCALAPPDATA", "")
    candidates += [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",        # Windows default
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",  # Windows 32-bit
        os.path.join(local_appdata, "Programs", "Tesseract-OCR", "tesseract.exe")
        if local_appdata else "",                                # Windows per-user
        "/opt/homebrew/bin/tesseract",          # macOS, Apple-Silicon Homebrew
        "/usr/local/bin/tesseract",             # macOS, Intel Homebrew
        "/usr/bin/tesseract",                   # Linux
    ]
    for cand in candidates:
        if cand and Path(cand).exists():
            return cand
    # Nothing found on disk — fall back to the bare name so pytesseract still tries
    # the PATH at run time and raises a clear, actionable "not found" error rather
    # than pointing at a path we know is wrong.
    return env_cmd or "tesseract"


# The single source of truth for the rest of the code (extractor_ocr.py reads this
# and assigns it to pytesseract.pytesseract.tesseract_cmd).
TESSERACT_CMD = _resolve_tesseract_cmd()

# ── OCR Confidence Thresholds ────────────────────────────────────────────────
# Tesseract gives each word a confidence score from 0 to 100.
# If the average confidence across all words is at or above this threshold,
# we trust the Tesseract output directly.
# If it falls below this threshold, the image is likely blurry or photographed
# at an angle, so we fall back to Groq Vision for better accuracy.
TESSERACT_CONFIDENCE_THRESHOLD = 80.0

# ── LLM Models ───────────────────────────────────────────────────────────────
# Groq model used for identifying column structure in bank statements.
# llama-3.3-70b-versatile is a fast, capable model available on Groq's API.
GROQ_MODEL = "llama-3.3-70b-versatile"

# Groq vision model used for OCR fallback on blurry/low-quality scanned images.
# meta-llama/llama-4-scout-17b-16e-instruct is Groq's vision model.
# This is read through GROQ2_KEY — its own key, kept separate from the column-ID
# text key (GROQ1) so the two never compete for the same rate limit.
GROQ_VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"

# NOTE: There is intentionally NO Gemini model here. The locked design decision is
# "one provider only — Groq". Gemini was removed from the entire codebase.

# ── LLM Behaviour ────────────────────────────────────────────────────────────
# We only send the first 40 lines of a document to Groq for column identification.
# Sending the full document would be expensive and unnecessary — the column
# structure is always visible in the header and first few rows.
COLUMN_ID_SAMPLE_LINES = 40

# ── Standard Output Schema ───────────────────────────────────────────────────
# These are the EXACT column names that the unified output DataFrame will always
# have, regardless of what bank or file format the original statement came from.
# The analysis engine (25 fraud detection cases) depends on these exact names.
# Do not change these without also updating the analysis engine.
# Date and Time are SEPARATE columns. Date holds only the calendar date (no false
# "00:00:00"); Time holds the transaction time when the statement prints one, else
# it is blank — we never invent a midnight time.
STANDARD_COLUMNS = ["Date", "Time", "Narration", "Debit", "Credit", "Balance", "Account_ID", "Bank_Name"]

# Additional identifier columns carried ALONGSIDE the standard schema whenever a
# statement provides them: a cheque/instrument number and a transaction reference
# (Ref No / RRN / UTR / Txn ID). They are ADDITIVE — STANDARD_COLUMNS above is left
# unchanged so the analysis engine's required columns are untouched — but these
# identifiers are now preserved end-to-end (CSV + JSON) for EVERY format instead of
# being dropped just because a bank labelled the column "CHQNO" or "Ref No".
# Team decision: a cheque number and a transaction reference are DIFFERENT things in
# an investigation, so they are kept as two separate fields, never merged.
REFERENCE_COLUMNS = ["Reference_Number", "Cheque_Number"]

# ── Validation Tolerances ────────────────────────────────────────────────────
# When checking balance arithmetic (previous_balance + credit - debit = current_balance),
# we allow a small rounding error of up to 1 rupee because banks sometimes
# round interest calculations differently from simple arithmetic.
BALANCE_TOLERANCE = 1.0

# ── Escalation thresholds (the Validation-Arbitrated Tiered Hybrid) ───────────
# These decide WHEN a cheap deterministic parse is trusted and when the pipeline
# must escalate to the LLM. Kept here (not hidden in code) so the team can tune
# them in one visible place. See CHANGES_INSTRUCTIONS.md / System Design v2.0.
#
# A parse is ACCEPTED (no LLM needed) only when, on a statement that HAS a running
# balance, at least this fraction of rows reconcile (prev ± amount = balance).
# Below this, the pipeline asks the LLM to describe the layout and re-parses.
ACCEPT_RECONCILE_RATE = 0.98
# A parse must also have captured at least this fraction of the lines that LOOK
# like transactions (parsed_rows / transaction_like_lines). A big shortfall means
# the cheap parser under-extracted, so we escalate even if the few rows reconcile.
MIN_COMPLETENESS_RATIO = 0.90
# Max number of representative lines sent to the LLM for schema discovery (Tier 4).
# The LLM only ever sees this small sample, never the whole statement.
SCHEMA_SAMPLE_LINES = 30
# When repairing rows the cheap parser could not reconcile (Tier 5), process them
# in bounded batches of this size. This replaces the old silent 40-chunk cap:
# nothing is ever dropped — any row not repaired is FLAGGED for manual review.
REPAIR_BATCH_ROWS = 60

# ── ChromaDB Vector Store ────────────────────────────────────────────────────
# Name prefix for ChromaDB collections. Each investigation session gets its
# own collection named "transactions_{session_id}" to keep cases separate.
CHROMADB_COLLECTION_NAME = "transactions"

# Sentence embedding model used to convert transaction text into vectors.
# all-MiniLM-L6-v2 is a small, fast model that runs locally on CPU.
# It is downloaded once by sentence-transformers and then cached locally.
EMBEDDING_MODEL = "all-MiniLM-L6-v2"

# ── Supported File Types ─────────────────────────────────────────────────────
# Average number of extractable characters per page above which a PDF is
# considered a "digital PDF" (generated by a computer, not scanned).
# Below this threshold, the PDF is treated as a scanned image.
DIGITAL_PDF_CHAR_THRESHOLD = 100

# Complete list of file extensions that the system accepts from investigators.
SUPPORTED_EXTENSIONS = [".pdf", ".xlsx", ".xls", ".csv", ".docx", ".jpg", ".jpeg", ".png"]


# ── Startup key check ────────────────────────────────────────────────────────
def require_extraction_keys() -> None:
    """
    Fails fast at the start of a run if the extraction keys are missing.

    Extraction uses exactly two keys: GROQ1 (column identification) and GROQ2
    (the blurry-image OCR fallback). If either is absent from .env we stop here
    with a message a non-technical teammate can act on, instead of letting the
    pipeline crash halfway through a run with a confusing stack trace.

    Call this once at the start of any extraction entry point.
    """
    missing = []
    if not GROQ1_KEY:
        missing.append("GROQ1 (used for column identification)")
    if not GROQ2_KEY:
        missing.append("GROQ2 (used for the blurry-image OCR fallback)")
    if missing:
        raise RuntimeError(
            "Cannot start extraction — these keys are missing from your .env file:\n  - "
            + "\n  - ".join(missing)
            + "\n\nFix: copy .env.example to .env and paste the real Groq keys in. "
            "GROQ1 and GROQ2 are both required before running extraction."
        )
