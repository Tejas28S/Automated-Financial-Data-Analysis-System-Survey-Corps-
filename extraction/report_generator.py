"""
report_generator.py — Extraction-phase summary report.

Produces, after every run, a single source of truth so regressions are caught without
digging through raw CSVs and so the analysis phase has a machine-readable manifest of
what it is receiving (how many accounts, which are reliable, the LLM budget spent).

It computes every number from the in-memory run result — nothing is hardcoded. Writes
both a human-readable `.txt` and a machine-readable `.json`, and prints a short summary.

Team: Survey Corps | CIDECODE Hackathon 2026 | CID Karnataka
"""

import json
import logging
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

logger = logging.getLogger(__name__)
SOURCE_ACCOUNT_ID_COLUMN = "source_account_id"


def generate_extraction_report(
    clean_df: pd.DataFrame,
    flagged_df: pd.DataFrame,
    per_file_records: List[Dict[str, Any]],
    summary: Dict[str, Any],
    output_dir: str,
) -> Dict[str, Any]:
    """Builds the extraction summary, writes report.txt + report.json under
    `output_dir`, prints a 5-line stdout summary, and returns the report dict."""
    clean_df = clean_df if clean_df is not None else pd.DataFrame()
    flagged_df = flagged_df if flagged_df is not None else pd.DataFrame()
    files = per_file_records or []

    n_clean = len(clean_df)
    n_flagged = len(flagged_df)
    n_dupes = int(clean_df["duplicate_of"].notna().sum()) if "duplicate_of" in clean_df.columns else 0

    # Flag breakdown + named mismatch diagnosis.
    flag_reasons = dict(Counter(flagged_df["flag_reason"].dropna())) if "flag_reason" in flagged_df.columns else {}
    mismatch_diag = {}
    if "mismatch_diagnosis" in flagged_df.columns:
        md = flagged_df[flagged_df.get("flag_reason") == "balance_mismatch"]["mismatch_diagnosis"]
        mismatch_diag = dict(Counter(v for v in md if str(v).strip()))

    flagged_account_col = (
        SOURCE_ACCOUNT_ID_COLUMN if SOURCE_ACCOUNT_ID_COLUMN in flagged_df.columns else "Account_ID"
    )
    top_flagged = (Counter(flagged_df[flagged_account_col]).most_common(5)
                   if flagged_account_col in flagged_df.columns and n_flagged else [])

    graded = [f for f in files if f.get("status") != "FAILED"]
    zero_row = [f.get("file") for f in graded if f.get("rows_standardised", 0) == 0]
    recons = [f["reconciliation_rate"] for f in graded if "reconciliation_rate" in f]
    avg_recon = round(sum(recons) / len(recons), 3) if recons else 0.0
    low_recon = [(f.get("file"), f.get("reconciliation_rate"))
                 for f in graded if f.get("reconciliation_rate", 1) < 0.9]

    # Metadata quality (from each file's reconciled account_details).
    def _md(f, k):
        return (f.get("account_details", {}) or {}).get(k, "")
    with_holder = sum(1 for f in graded if _md(f, "account_holder"))
    with_number = sum(1 for f in graded if _md(f, "account_number")
                      and not str(_md(f, "account_number")).startswith("UNKNOWN"))
    with_ifsc = sum(1 for f in graded if _md(f, "ifsc_code") and _md(f, "ifsc_code") != "UNKNOWN")
    missing_holder = [f.get("file") for f in graded if not _md(f, "account_holder")]
    meta_source = dict(Counter(f.get("metadata_source", "n/a") for f in graded))

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "totals": {
            "files_processed": summary.get("files_processed", len(graded)),
            "files_failed": len(summary.get("files_failed", [])),
            "transactions_clean": n_clean,
            "transactions_flagged": n_flagged,
            "duplicates_tagged": n_dupes,
        },
        "flag_breakdown": flag_reasons,
        "mismatch_diagnosis": mismatch_diag,
        "top_flagged_accounts": [{"account": a, "flags": c} for a, c in top_flagged],
        "reconciliation": {
            "average": avg_recon,
            "files_below_0.90": [{"file": f, "recon": r} for f, r in low_recon],
            "zero_row_files": zero_row,
        },
        "metadata_quality": {
            "graded_files": len(graded),
            "with_holder_name": with_holder,
            "with_account_number": with_number,
            "with_ifsc": with_ifsc,
            "missing_holder_files": missing_holder,
            "source_breakdown": meta_source,
        },
        "llm_usage": {
            "total_calls": summary.get("total_llm_calls", 0),
            "key_rotations": summary.get("key_rotations", 0),
        },
    }

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    try:
        (out / "extraction_summary_report.json").write_text(json.dumps(report, indent=2))
        (out / "extraction_summary_report.txt").write_text(_format_txt(report))
        # Per-file extraction ledger (Section 3): one auditable row per processed file.
        _write_ledger(files, out / "extraction_ledger.json")
    except Exception as e:  # a report failure must never break a successful run
        logger.warning("report_generator: could not write report files: %s", e)

    print("\n" + "=" * 55)
    print("EXTRACTION COMPLETE")
    print(f"  Clean: {n_clean:,}  |  Flagged: {n_flagged:,}  |  Dupes tagged: {n_dupes:,}")
    print(f"  Avg recon: {avg_recon:.1%}  |  <0.90: {len(low_recon)}  |  zero-row: {len(zero_row)}")
    print(f"  Metadata: holder {with_holder}/{len(graded)}, acct {with_number}/{len(graded)}")
    print("=" * 55 + "\n")
    return report


def _write_ledger(files: List[Dict[str, Any]], path) -> None:
    """
    Writes the per-file extraction ledger (Section 3) — one auditable object per
    processed file with route, parser tier, fallback used, row counts, reconciliation,
    zero-row status/reason, and per-field metadata + source. This is what lets any
    number in the run be traced back to a specific file without re-running anything.
    """
    ledger = []
    for f in files:
        ad = f.get("account_details", {}) or {}
        rows_clean = f.get("rows_clean", 0)
        rows_flagged = f.get("rows_flagged", 0)
        rows = f.get("rows_standardised", rows_clean + rows_flagged)
        recon = f.get("reconciliation_rate")
        status = f.get("status", "ok")
        # Zero-row status: distinguish a genuine zero from a functional failure.
        if status == "FAILED":
            zr_status, zr_reason = "technical_failure", f.get("error", "")
        elif rows == 0:
            zr_status = f.get("zero_row_status", "true_zero" if f.get("zero_row_reason") else "functional_failure")
            zr_reason = f.get("zero_row_reason", "zero rows extracted; no positive zero-activity evidence")
        else:
            zr_status, zr_reason = "ok", ""
        ledger.append({
            "file_name": f.get("file"),
            "source_account_id": f.get("source_account_id", ""),
            "route": f.get("route"),
            "parser_tier": f.get("tier"),
            "fallback_method_used": f.get("fallback_method_used", ""),
            "raw_text_char_count": f.get("raw_text_chars"),
            "transaction_like_lines": f.get("expected_txn_lines"),
            "rows_extracted": rows,
            "rows_clean": rows_clean,
            "rows_flagged": rows_flagged,
            "reconciliation_rate": recon,
            "zero_row_status": zr_status,
            "zero_row_reason": zr_reason,
            "account_number_found": bool(ad.get("account_number")
                                         and not str(ad.get("account_number")).startswith("UNKNOWN")),
            "account_number": ad.get("account_number", ""),
            "holder_found": bool(ad.get("account_holder")),
            "holder": ad.get("account_holder", ""),
            "ifsc_found": bool(ad.get("ifsc_code") and ad.get("ifsc_code") != "UNKNOWN"),
            "ifsc": ad.get("ifsc_code", ""),
            "bank_name": ad.get("bank_name", ""),
            "metadata_source": f.get("metadata_source", ""),
            "column_map_source": f.get("column_map_source", ""),
            "llm_calls": f.get("llm_calls", 0),
        })
    try:
        Path(path).write_text(json.dumps(ledger, indent=2))
    except Exception as e:
        logger.warning("report_generator._write_ledger: could not write ledger: %s", e)


def _format_txt(r: Dict[str, Any]) -> str:
    L = []
    t = r["totals"]; rec = r["reconciliation"]; mq = r["metadata_quality"]
    L.append("RUN SUMMARY — Extraction Phase Report")
    L.append(f"Generated: {r['generated_at']}")
    L.append("-" * 57)
    L.append("TOTALS")
    L.append(f"  Files processed : {t['files_processed']}   (failed: {t['files_failed']})")
    L.append(f"  Clean txns      : {t['transactions_clean']:,}")
    L.append(f"  Flagged txns    : {t['transactions_flagged']:,}")
    L.append(f"  Duplicates      : {t['duplicates_tagged']:,}")
    L.append("")
    L.append("FLAG BREAKDOWN")
    for k, v in sorted(r["flag_breakdown"].items(), key=lambda kv: -kv[1]):
        L.append(f"  {k:42s} {v:6d}")
    if r["mismatch_diagnosis"]:
        L.append("  balance_mismatch diagnosis:")
        for k, v in sorted(r["mismatch_diagnosis"].items(), key=lambda kv: -kv[1]):
            L.append(f"    {k:40s} {v:6d}")
    L.append("")
    L.append("RECONCILIATION")
    L.append(f"  Average rate          : {rec['average']}")
    L.append(f"  Files below 0.90      : {len(rec['files_below_0.90'])}")
    for d in rec["files_below_0.90"]:
        L.append(f"    {d['recon']:.3f}  {d['file']}")
    L.append(f"  Zero-row files        : {len(rec['zero_row_files'])}")
    for f in rec["zero_row_files"]:
        L.append(f"    {f}")
    L.append("")
    L.append("METADATA QUALITY")
    L.append(f"  Graded files          : {mq['graded_files']}")
    L.append(f"  With holder name      : {mq['with_holder_name']}")
    L.append(f"  With account number   : {mq['with_account_number']}")
    L.append(f"  With IFSC             : {mq['with_ifsc']}")
    L.append(f"  Source breakdown      : {mq['source_breakdown']}")
    if mq["missing_holder_files"]:
        L.append("  Files still missing holder (verify against source):")
        for f in mq["missing_holder_files"]:
            L.append(f"    {f}")
    L.append("")
    L.append("LLM USAGE")
    L.append(f"  Total LLM calls       : {r['llm_usage']['total_calls']}")
    L.append(f"  Key rotations         : {r['llm_usage']['key_rotations']}")
    return "\n".join(L) + "\n"
