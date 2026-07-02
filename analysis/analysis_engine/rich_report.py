# Copied from: analysis phase cidecode hackathon/analysis phase cidecode hackathon/analysis_engine/rich_report.py
"""
rich_report.py
==============
Generates a clean, focused JSON report from an AnalysisResult and
writes it into the ``analysis_op/`` folder.

The report is designed to be immediately useful for investigators:
  - Every fraud transaction found is listed individually
  - Every flagged account is shown (not just top 10)
  - Patterns detected with their findings
  - Key transactions (all unique txn IDs from findings)
  - Auto-generated overview paragraph

Public API
----------
write_rich_report(result, input_csv_path, output_root)
    Writes ``analysis_op/<case_name>/report.json`` and returns the path.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import AnalysisResult, Finding, PATTERN_CATALOG, pattern_key


# ---------------------------------------------------------------------------
# Pattern labels
# ---------------------------------------------------------------------------

_PATTERN_LABELS: dict[int, str] = {
    1: "Duplicate Transaction Cross-Check",
    2: "Failed / Reversed Transaction Detection",
    3: "Balance Consistency Validation",
    4: "Round Trip Detection",
    5: "Transit / Pass-Through Detection",
    6: "Accumulation Account Detection",
    7: "Structuring / Smurfing Detection",
    8: "Money Flow Graph Construction",
    9: "Circular Flow (Multi-Hop Cycle) Detection",
    10: "Money Trail Tracing",
    11: "High-Throughput Pass-Through Accounts",
    12: "Matched Internal Flow Hub",
    13: "Cross-Statement Money Flow Links",
    14: "Large Credit Followed by Cash Withdrawal",
    15: "Accumulation / Holding Account Detection",
    16: "Repeated Round-Value Debit Pattern",
    17: "Shared UPI Identifier Detection",
    18: "Reversal Cluster Detection",
    19: "Low-Value Reciprocal Account Testing",
    20: "High-Risk Internal Flow Hub Ranking",
    21: "Top Suspicious Account Ranking",
}


def _safe_str(value: Any) -> str:
    """Convert any value to a clean str, replacing Unicode arrows."""
    return str(value).replace("\u2192", "->")


# ---------------------------------------------------------------------------
# Transaction detail lookup
# ---------------------------------------------------------------------------

def _fetch_transaction_details(
    connection: sqlite3.Connection,
    txn_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """Fetch full transaction details for a list of txn_ids from the DB."""
    if not txn_ids or connection is None:
        return {}
    
    result = {}
    # Process in batches to avoid SQL parameter limits
    batch_size = 500
    for i in range(0, len(txn_ids), batch_size):
        batch = txn_ids[i:i + batch_size]
        placeholders = ",".join("?" for _ in batch)
        try:
            rows = connection.execute(
                f"""
                SELECT txn_id, account_id, date, time, narration, reference,
                       debit_amount, credit_amount, balance, bank_name,
                       account_holder, counterparty_account
                FROM transactions
                WHERE txn_id IN ({placeholders})
                """,
                batch,
            ).fetchall()
            for row in rows:
                debit = float(row["debit_amount"] or 0)
                credit = float(row["credit_amount"] or 0)
                amount = max(debit, credit)
                direction = "debit" if debit > credit else "credit"
                result[str(row["txn_id"])] = {
                    "account_id": str(row["account_id"]),
                    "date": str(row["date"] or ""),
                    "time": str(row["time"] or ""),
                    "amount": round(amount, 2),
                    "direction": direction,
                    "narration": str(row["narration"] or ""),
                    "reference": str(row["reference"] or ""),
                    "balance": float(row["balance"]) if row["balance"] is not None else None,
                    "counterparty": str(row["counterparty_account"] or ""),
                }
        except Exception:
            pass
    return result


def _fetch_account_details(
    connection: sqlite3.Connection,
    account_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """Fetch account-level details from the accounts table."""
    if not account_ids or connection is None:
        return {}
    
    result = {}
    batch_size = 500
    for i in range(0, len(account_ids), batch_size):
        batch = account_ids[i:i + batch_size]
        placeholders = ",".join("?" for _ in batch)
        try:
            rows = connection.execute(
                f"""
                SELECT account_id, account_holder, bank_name, ifsc_code,
                       transaction_count, total_credit, total_debit, total_volume,
                       throughput_ratio, unique_counterparty_count,
                       first_date, last_date
                FROM accounts
                WHERE account_id IN ({placeholders})
                """,
                batch,
            ).fetchall()
            for row in rows:
                result[str(row["account_id"])] = {
                    "account_holder": str(row["account_holder"] or ""),
                    "bank_name": str(row["bank_name"] or ""),
                    "ifsc_code": str(row["ifsc_code"] or ""),
                    "transaction_count": int(row["transaction_count"] or 0),
                    "total_credit": round(float(row["total_credit"] or 0), 2),
                    "total_debit": round(float(row["total_debit"] or 0), 2),
                    "total_volume": round(float(row["total_volume"] or 0), 2),
                    "throughput_ratio": round(float(row["throughput_ratio"] or 0), 4),
                    "unique_counterparty_count": int(row["unique_counterparty_count"] or 0),
                    "first_date": str(row["first_date"] or ""),
                    "last_date": str(row["last_date"] or ""),
                }
        except Exception:
            pass
    return result


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

def build_rich_report(
    result: AnalysisResult,
    input_csv_path: str | Path,
) -> dict[str, Any]:
    """Assemble the complete clean report dict from an AnalysisResult."""
    input_path = Path(input_csv_path)
    case_name = input_path.stem

    # Get the DB connection from the pipeline result if available
    connection = getattr(result, '_connection', None)

    # ── 1. Basic metadata ────────────────────────────────────────────────
    meta = result.run_metadata or {}
    bs = result.baseline_summary or {}
    rc = bs.get("row_counts", {})
    dr = bs.get("date_range", {})

    # ── 2. Collect ALL unique txn_ids from ALL findings ──────────────────
    all_txn_ids: set[str] = set()
    # Map: txn_id -> list of (pattern_name, finding_id)
    txn_pattern_map: dict[str, list[tuple[str, str]]] = {}
    # Map: account_id -> set of pattern names
    account_patterns: dict[str, set[str]] = {}
    # Map: account_id -> count of flagged transactions
    account_txn_counts: dict[str, int] = {}

    all_findings_enriched: list[dict[str, Any]] = []
    patterns_detected: list[dict[str, Any]] = []
    
    for pid in sorted(PATTERN_CATALOG.keys()):
        key = pattern_key(pid)
        items = result.findings_by_pattern.get(key, [])
        if not items:
            continue
        
        pattern_name = _PATTERN_LABELS.get(pid, key)
        finding_entries = []
        
        for f in items:
            finding_id = f.finding_id or ""
            
            # Track txn_ids
            for txn_id in f.txn_ids:
                all_txn_ids.add(txn_id)
                txn_pattern_map.setdefault(txn_id, []).append((pattern_name, finding_id))
            
            # Track accounts
            for acct in f.accounts:
                account_patterns.setdefault(acct, set()).add(pattern_name)
                account_txn_counts[acct] = account_txn_counts.get(acct, 0) + len(f.txn_ids)
            
            finding_entries.append({
                "finding_id": finding_id,
                "accounts": f.accounts,
                "transaction_count": len(f.txn_ids),
                "explanation": _safe_str(f.explanation),
                "confidence": f.confidence_tier,
            })
        
        patterns_detected.append({
            "pattern_id": pid,
            "pattern_name": pattern_name,
            "findings_count": len(items),
            "findings": finding_entries,
        })

    # ── 3. Build fraud_transactions list ─────────────────────────────────
    # Fetch details for all flagged txn_ids from DB
    sorted_txn_ids = sorted(all_txn_ids)
    txn_details = _fetch_transaction_details(connection, sorted_txn_ids)

    fraud_transactions: list[dict[str, Any]] = []
    for txn_id in sorted_txn_ids:
        entry: dict[str, Any] = {"transaction_id": txn_id}
        
        # Add DB details if available
        if txn_id in txn_details:
            details = txn_details[txn_id]
            entry["account_id"] = details["account_id"]
            entry["date"] = details["date"]
            entry["time"] = details["time"]
            entry["amount"] = details["amount"]
            entry["direction"] = details["direction"]
            entry["narration"] = details["narration"]
            entry["reference"] = details["reference"]
            entry["balance"] = details["balance"]
            entry["counterparty"] = details["counterparty"]
        
        # Add pattern info
        patterns_for_txn = txn_pattern_map.get(txn_id, [])
        entry["patterns_detected"] = sorted(set(p[0] for p in patterns_for_txn))
        entry["finding_ids"] = sorted(set(p[1] for p in patterns_for_txn if p[1]))
        
        fraud_transactions.append(entry)

    # ── 4. Build flagged_accounts list ───────────────────────────────────
    # Get all flagged account IDs from findings
    all_flagged_account_ids = sorted(account_patterns.keys())
    acct_details = _fetch_account_details(connection, all_flagged_account_ids)

    # Also get scoring data from suspicious_accounts
    scoring_map: dict[str, dict[str, Any]] = {}
    for sa in result.suspicious_accounts:
        acct_id = str(sa.get("account_id", ""))
        if acct_id:
            scoring_map[acct_id] = sa

    flagged_accounts: list[dict[str, Any]] = []
    for acct_id in all_flagged_account_ids:
        entry: dict[str, Any] = {
            "account_id": acct_id,
            "patterns_triggered": sorted(account_patterns.get(acct_id, set())),
            "distinct_pattern_count": len(account_patterns.get(acct_id, set())),
            "total_flagged_transactions": account_txn_counts.get(acct_id, 0),
        }
        
        # Add DB account details if available
        if acct_id in acct_details:
            ad = acct_details[acct_id]
            entry["account_holder"] = ad["account_holder"]
            entry["bank_name"] = ad["bank_name"]
            entry["total_credit"] = ad["total_credit"]
            entry["total_debit"] = ad["total_debit"]
            entry["total_volume"] = ad["total_volume"]
            entry["transaction_count"] = ad["transaction_count"]
        
        # Add scoring info
        if acct_id in scoring_map:
            sa = scoring_map[acct_id]
            entry["total_findings"] = sa.get("total_findings", 0)
        else:
            entry["total_findings"] = 0
        
        flagged_accounts.append(entry)
    
    # Sort by distinct_pattern_count desc, then total_flagged_transactions desc
    flagged_accounts.sort(
        key=lambda x: (-x["distinct_pattern_count"], -x["total_flagged_transactions"], x["account_id"])
    )

    # ── 5. Key transactions (sorted unique list) ────────────────────────
    key_transactions = sorted_txn_ids

    # ── 6. Auto-generated overview ──────────────────────────────────────
    total_rows = rc.get("total", 0) or rc.get("eligible", 0) or 0
    total_accounts = bs.get("account_count", 0) or 0
    date_start = dr.get("start", "unknown")
    date_end = dr.get("end", "unknown")
    num_findings = sum(p["findings_count"] for p in patterns_detected)
    num_patterns = len(patterns_detected)
    pattern_names = ", ".join(p["pattern_name"] for p in patterns_detected)
    num_flagged_accounts = len(flagged_accounts)
    num_fraud_txns = len(fraud_transactions)

    overview = (
        f"Analysis of {input_path.name} containing {total_rows} transactions "
        f"across {total_accounts} accounts from {date_start} to {date_end}. "
    )
    if num_findings > 0:
        overview += (
            f"The engine detected {num_findings} finding(s) across "
            f"{num_patterns} pattern type(s): {pattern_names}. "
            f"{num_flagged_accounts} account(s) were flagged as suspicious "
            f"with {num_fraud_txns} transaction(s) identified as potentially fraudulent."
        )
    else:
        overview += "No suspicious patterns were detected in this dataset."

    # ── 7. Assemble final report ────────────────────────────────────────
    report: dict[str, Any] = {
        "case_name": case_name,
        "input_file": input_path.name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_transactions_analyzed": total_rows,
        "total_accounts": total_accounts,
        "date_range": {"start": date_start, "end": date_end},
        "fraud_transactions": fraud_transactions,
        "flagged_accounts": flagged_accounts,
        "patterns_detected": patterns_detected,
        "key_transactions": key_transactions,
        "overview": overview,
    }

    return report


def _write_summary_txt(report: dict[str, Any], dest_dir: Path) -> Path:
    """Write a plain-text summary with only Fraud Transactions, Accounts, and Key Transactions."""
    lines: list[str] = []
    case_name = report.get("case_name", "unknown")

    lines.append(f"{'=' * 70}")
    lines.append(f"  {case_name} — Analysis Summary")
    lines.append(f"{'=' * 70}")
    lines.append("")

    # ── 1. Fraud Transactions ────────────────────────────────────────────
    lines.append("Fraud Transactions:")
    lines.append("-" * 40)
    fraud_txns = report.get("fraud_transactions", [])
    if fraud_txns:
        for txn in fraud_txns:
            txn_id = txn.get("transaction_id", "")
            acct = txn.get("account_id", "")
            date = txn.get("date", "")
            amount = txn.get("amount", 0)
            direction = txn.get("direction", "")
            narration = txn.get("narration", "")
            patterns = ", ".join(txn.get("patterns_detected", []))
            lines.append(f"  {txn_id}")
            lines.append(f"    Account   : {acct}")
            lines.append(f"    Date      : {date}")
            lines.append(f"    Amount    : {amount}")
            lines.append(f"    Direction : {direction}")
            lines.append(f"    Narration : {narration}")
            lines.append(f"    Pattern   : {patterns}")
            lines.append("")
    else:
        lines.append("  (none detected)")
        lines.append("")

    # ── 2. Accounts ──────────────────────────────────────────────────────
    lines.append("Accounts:")
    lines.append("-" * 40)
    flagged = report.get("flagged_accounts", [])
    if flagged:
        for acct in flagged:
            acct_id = acct.get("account_id", "")
            holder = acct.get("account_holder", "")
            patterns = ", ".join(acct.get("patterns_triggered", []))
            txn_count = acct.get("total_flagged_transactions", 0)
            total_credit = acct.get("total_credit", 0)
            total_debit = acct.get("total_debit", 0)
            lines.append(f"  {acct_id}")
            if holder:
                lines.append(f"    Holder             : {holder}")
            lines.append(f"    Patterns Triggered : {patterns}")
            lines.append(f"    Flagged Txns       : {txn_count}")
            lines.append(f"    Total Credit       : {total_credit}")
            lines.append(f"    Total Debit        : {total_debit}")
            lines.append("")
    else:
        lines.append("  (none flagged)")
        lines.append("")

    # ── 3. Key Transactions to Detect ────────────────────────────────────
    lines.append("Key Transactions to Detect:")
    lines.append("-" * 40)
    key_txns = report.get("key_transactions", [])
    if key_txns:
        for txn_id in key_txns:
            lines.append(f"  {txn_id}")
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append(f"{'=' * 70}")

    txt_path = dest_dir / "report.txt"
    txt_path.write_text("\n".join(lines), encoding="utf-8")
    return txt_path


def write_rich_report(
    result: AnalysisResult,
    input_csv_path: str | Path,
    output_root: str | Path,
) -> Path:
    """Build and write the rich report JSON to ``analysis_op/<case_name>/report.json``.

    Also writes a plain-text summary ``report.txt`` with only
    Fraud Transactions, Accounts, and Key Transactions.

    Parameters
    ----------
    result          : AnalysisResult returned by AnalysisPipeline.run()
    input_csv_path  : Path to the original input CSV (used to derive case_name)
    output_root     : Root directory of the project (``analysis_op/`` will be created here)

    Returns
    -------
    Path  –  absolute path of the written JSON file.
    """
    case_name = Path(input_csv_path).stem
    dest_dir = Path(output_root) / "analysis_op" / case_name
    dest_dir.mkdir(parents=True, exist_ok=True)

    report = build_rich_report(result, input_csv_path)

    # Write report.json
    dest = dest_dir / "report.json"
    dest.write_text(
        json.dumps(report, indent=2, default=str, ensure_ascii=False),
        encoding="utf-8",
    )

    # Write report.txt (Fraud Transactions, Accounts, Key Transactions only)
    txt_path = _write_summary_txt(report, dest_dir)

    return dest
