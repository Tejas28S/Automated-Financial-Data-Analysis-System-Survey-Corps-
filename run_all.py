"""
run_all.py — One-shot batch runner for all original bank statements.

Usage:
    python run_all.py

Processes every file in 'original bank statements/' through the extraction
pipeline and prints a per-file summary table plus aggregate stats.
"""

import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# ── Setup ─────────────────────────────────────────────────────────────────────
# Add project root to sys.path so all imports resolve.
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s %(name)s: %(message)s",
)
# Show pipeline-level INFO so we can track progress without drowning in debug.
logging.getLogger("extraction.extraction_pipeline").setLevel(logging.INFO)

from config.settings import SUPPORTED_EXTENSIONS
from extraction.extraction_pipeline import run_extraction_pipeline

# ── Collect files ─────────────────────────────────────────────────────────────
BANK_STMTS_DIR = PROJECT_ROOT / "original bank statements"

files = []
for p in sorted(BANK_STMTS_DIR.iterdir()):
    if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS:
        files.append({
            "file_path": str(p),
            "account_id": p.stem,
            "bank_name": "",
        })

print(f"\n{'='*70}")
print(f"Survey Corps — Full Dataset Batch Run")
print(f"Files found : {len(files)}")
print(f"Started     : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print(f"{'='*70}\n")

# ── Run pipeline ──────────────────────────────────────────────────────────────
session_id = f"batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
t0 = time.perf_counter()

result = run_extraction_pipeline(
    files=files,
    session_id=session_id,
    ingest_to_chromadb=False,
    persist=True,
)

elapsed = time.perf_counter() - t0

# ── Report ────────────────────────────────────────────────────────────────────
per_file = result.get("per_file", [])

HEADER = (
    f"{'File':<55} {'Rows':>5} {'Clean':>5} {'Flag':>5} "
    f"{'Recon':>6} {'Comp':>5} {'Tier':<14} {'LLM':>4}"
)
print(HEADER)
print("-" * len(HEADER))

total_rows = 0
total_clean = 0
total_flagged = 0
tier_counts: dict = {}
llm_total = 0
failed_files = []

for fr in per_file:
    fname  = Path(fr.get("file", "")).name
    rows   = fr.get("rows_standardised", 0) or 0
    clean  = fr.get("rows_clean", 0) or 0
    flagged= fr.get("rows_flagged", 0) or 0
    recon  = fr.get("reconciliation_rate", 0.0) or 0.0
    tier   = fr.get("tier", "—")
    llm    = fr.get("llm_calls", 0) or 0
    status = fr.get("status", "")

    caud   = fr.get("transaction_count_audit") or {}
    comp   = caud.get("completeness_ratio", 1.0)

    total_rows    += rows
    total_clean   += clean
    total_flagged += flagged
    llm_total     += llm
    tier_counts[tier] = tier_counts.get(tier, 0) + 1

    if status == "failed":
        failed_files.append(fname)
        marker = " ✗"
    else:
        marker = ""

    recon_s = f"{recon:.3f}" if recon is not None else "  N/A"
    comp_s  = f"{comp:.3f}" if comp is not None else "  N/A"
    print(
        f"{(fname+marker):<55} {rows:>5} {clean:>5} {flagged:>5} "
        f"{recon_s:>6} {comp_s:>5} {tier:<14} {llm:>4}"
    )

print("-" * len(HEADER))
print(
    f"{'TOTAL':<55} {total_rows:>5} {total_clean:>5} {total_flagged:>5}"
)

print(f"\n{'='*70}")
print(f"Session     : {session_id}")
print(f"Duration    : {elapsed:.1f}s")
print(f"Files OK    : {len(per_file) - len(failed_files)} / {len(per_file)}")
if failed_files:
    print(f"Failed      : {', '.join(failed_files)}")
print(f"Total rows  : {total_rows}  clean={total_clean}  flagged={total_flagged}")
print(f"LLM calls   : {llm_total}")
print(f"Tier mix    : {dict(sorted(tier_counts.items()))}")

storage = result.get("storage_paths", {})
if storage:
    print(f"\nOutputs:")
    for k, v in storage.items():
        print(f"  {k:<20}: {v}")

# Print first_balance_break for any file that has one, to aid debugging.
breaks = [
    (Path(fr.get("file", "")).name, fr["transaction_count_audit"]["first_balance_break"])
    for fr in per_file
    if fr.get("transaction_count_audit", {}).get("first_balance_break")
]
if breaks:
    print(f"\nBalance breaks detected in {len(breaks)} file(s):")
    for fname, brk in breaks:
        print(f"  {fname}: row {brk['row_index']}  date={brk['date']}  "
              f"delta={brk['delta']}  narration={brk['narration'][:60]}")

print(f"\n{'='*70}\n")
