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
# Path to the Tesseract OCR executable on the current machine.
# Default is the macOS Homebrew path; override in .env on Linux/Windows.
TESSERACT_CMD = os.getenv("TESSERACT_CMD", "/opt/homebrew/bin/tesseract")

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

# ── Validation Tolerances ────────────────────────────────────────────────────
# When checking balance arithmetic (previous_balance + credit - debit = current_balance),
# we allow a small rounding error of up to 1 rupee because banks sometimes
# round interest calculations differently from simple arithmetic.
BALANCE_TOLERANCE = 1.0

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
