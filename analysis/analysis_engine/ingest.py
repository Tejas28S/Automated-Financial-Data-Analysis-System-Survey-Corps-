# Copied from: analysis phase cidecode hackathon/analysis phase cidecode hackathon/analysis_engine/ingest.py
"""Inspect and normalize extraction outputs at the analysis boundary only."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any

import pandas as pd

from .utils import normalize_compact, normalize_text, parse_amount, parse_date, parse_time


@dataclass
class NormalizedInput:
    transactions: pd.DataFrame
    input_manifest: list[dict[str, Any]]
    metadata: dict[str, Any]
    input_contract: dict[str, Any]


def _header_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "txn_id": ("txn_id", "transaction_id", "ref_txn_no", "reftxnno"),
    "doc_id": ("doc_id", "document_id", "source_document"),
    "account_id": ("account_id", "source_account_id", "account_number", "account_no", "account"),
    "source_account_id": ("source_account_id",),
    "date": ("date", "transaction_date", "txn_date", "txndt", "postdate", "post_date"),
    "time": ("time", "transaction_time", "txn_time"),
    "narration": ("narration", "description", "particulars", "remarks"),
    "reference_number": ("reference_number", "reference", "ref_no", "utr", "refchqno", "ref_chq_no"),
    "transaction_reference": ("transaction_reference",),
    "transaction_id_external": ("transaction_id",),
    "debit_amount": ("debit_amount", "debit", "withdrawal", "withdrawal_amount"),
    "credit_amount": ("credit_amount", "credit", "deposit", "deposit_amount"),
    "balance": ("balance", "running_balance", "closing_balance"),
    "account_number": ("account_number", "account_no"),
    "account_holder": ("account_holder", "holder_name", "customer_name"),
    "bank_name": ("bank_name", "bank", "txn_branch", "txnbranch"),
    "ifsc_code": ("ifsc_code", "ifsc"),
    "confidence_score": ("confidence_score", "confidence"),
    "extraction_tier": ("extraction_tier", "tier"),
    "flag_reason": ("flag_reason", "reason_flagged"),
    "duplicate_of_txn_id": ("duplicate_of_txn_id", "duplicate_of"),
    "duplicate_row_number": ("duplicate_row_number",),
    "original_row_number": ("original_row_number",),
    "is_reversed_source_label": ("is_reversed",),
    "source_page": ("page", "page_number", "source_page"),
}

# Column keywords used to auto-detect the real header row in messy CSVs
_HEADER_INDICATOR_KEYS = frozenset({
    "debit", "credit", "balance", "narration", "date", "txndt",
    "withdrawal", "deposit", "description", "particulars",
})


def _extract_casa_metadata(path: Path) -> dict[str, Any]:
    """Scan the first ~20 rows for CASA-style metadata (account number, holder, branch)."""
    meta: dict[str, Any] = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i > 25:
                    break
                stripped = line.strip()
                # CASA TRANSACTION DETAILS :  ,,<account_number>
                if "CASA TRANSACTION DETAILS" in stripped.upper() or "TRANSACTION DETAILS" in stripped.upper():
                    parts = [p.strip() for p in stripped.split(",") if p.strip()]
                    for p in parts:
                        # account number is a numeric-ish token
                        cleaned = re.sub(r"[^0-9]", "", p)
                        if len(cleaned) >= 6:
                            meta["account_id"] = cleaned
                            break
                # Line 8 is typically the account holder name
                elif i == 7 and stripped and not stripped.startswith(","):
                    name = stripped.split(",")[0].strip()
                    if name and len(name) > 2 and not name[0].isdigit():
                        meta["account_holder"] = name
                # Line 6 is typically the branch
                elif i == 5 and stripped and not stripped.startswith(","):
                    branch = stripped.split(",")[0].strip()
                    if branch:
                        meta["branch"] = branch
    except Exception:
        pass
    return meta


def _find_header_row(path: Path) -> int:
    """Return the 0-based row index of the real header in a CSV that may have metadata rows."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i > 30:
                    break
                cells = [c.strip().lower() for c in line.split(",")]
                keys = {_header_key(c) for c in cells if c}
                # Check if this row has enough indicator columns to be the real header
                overlap = keys & _HEADER_INDICATOR_KEYS
                if len(overlap) >= 2:
                    return i
    except Exception:
        pass
    return 0  # Fallback: assume first row is header


def _read_csv(path: Path) -> pd.DataFrame:
    """Read a CSV, auto-detecting the header row to skip bank metadata rows."""
    header_row = _find_header_row(path)
    frame = pd.read_csv(
        path, dtype=str, keep_default_na=False, low_memory=False,
        header=header_row,
    )
    # Clean column names: strip whitespace
    frame.columns = [str(c).strip() for c in frame.columns]
    return frame


def _column_lookup(columns: list[str]) -> dict[str, str]:
    keyed = {_header_key(column): column for column in columns}
    found: dict[str, str] = {}
    for canonical, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            actual = keyed.get(_header_key(alias))
            if actual is not None:
                found[canonical] = actual
                break
    return found


def _first_value(row: pd.Series, lookup: dict[str, str], keys: tuple[str, ...]) -> str:
    for key in keys:
        column = lookup.get(key)
        if column is not None:
            value = str(row.get(column, "")).strip()
            if value:
                return value
    return ""


def _statement_doc_map(input_dir: Path) -> dict[str, str]:
    statements_dir = input_dir / "statements"
    mapping: dict[str, str] = {}
    if not statements_dir.is_dir():
        return mapping
    for path in sorted(statements_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for row in payload.get("transactions", []) or []:
            txn_id = str(row.get("txn_id", "")).strip()
            if txn_id and txn_id not in mapping:
                mapping[txn_id] = path.stem
    return mapping


def _read_metadata(input_dir: Path) -> dict[str, Any]:
    path = input_dir / "metadata.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"metadata_read_error": str(path)}


def _read_json_if_exists(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"read_error": str(path)}


def _build_input_contract(input_dir: Path, manifest: list[dict[str, Any]], metadata: dict[str, Any]) -> dict[str, Any]:
    by_name = {Path(item["path"]).name: item for item in manifest}
    clean_rows = int(by_name.get("clean_transactions.csv", {}).get("rows", 0) or 0)
    flagged_rows = int(by_name.get("flagged_transactions.csv", {}).get("rows", 0) or 0)
    duplicate_rows = int(
        by_name.get("duplicate_transactions.csv", by_name.get("duplicates.csv", {})).get("rows", 0) or 0
    )
    summary_report = _read_json_if_exists(input_dir / "extraction_summary_report.json")
    metadata_clean = metadata.get("summary", {}).get("clean_rows")
    report_clean = summary_report.get("totals", {}).get("transactions_clean")
    warnings: list[str] = []
    status = "ok"
    detail = "CSV counts match extraction metadata."
    expected_clean_values = [v for v in (metadata_clean, report_clean) if v is not None]
    if expected_clean_values:
        expected = int(expected_clean_values[0])
        if clean_rows != expected:
            if clean_rows + duplicate_rows == expected:
                status = "ok_reconciled_by_duplicates"
                detail = (
                    "Metadata clean_rows includes duplicate rows; final clean CSV excludes "
                    "duplicates stored separately."
                )
                warnings.append(
                    f"clean CSV rows ({clean_rows}) + duplicate rows ({duplicate_rows}) "
                    f"= metadata clean_rows ({expected})."
                )
            else:
                status = "mismatch"
                detail = f"clean CSV rows ({clean_rows}) do not reconcile to metadata clean_rows ({expected})."
                warnings.append(detail)
    return {
        "input_path": str(input_dir.resolve()),
        "clean_row_count": clean_rows,
        "flagged_row_count": flagged_rows,
        "duplicate_row_count": duplicate_rows,
        "metadata_clean_rows": metadata_clean,
        "summary_report_clean_rows": report_clean,
        "count_reconciliation_status": status,
        "count_reconciliation_detail": detail,
        "warnings": warnings,
    }


def _generated_txn_id(source_account_id: str, source_bucket: str, row_number: int) -> str:
    identity = f"{source_account_id}::{source_bucket}::{row_number}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"generated::{source_account_id or 'unknown'}::{row_number:06d}::{digest}"


def _normalize_frame(
    frame: pd.DataFrame,
    source_path: Path,
    source_bucket: str,
    doc_map: dict[str, str],
    flat_bucket_labels: bool = False,
) -> list[dict[str, Any]]:
    lookup = _column_lookup(list(frame.columns))
    required = {"account_id", "date", "debit_amount", "credit_amount"}
    if source_bucket != "duplicate" and not required.issubset(lookup):
        missing = sorted(required - set(lookup))
        raise ValueError(f"{source_path} is missing required semantic columns: {missing}")

    records: list[dict[str, Any]] = []
    for row_number, (_, row) in enumerate(frame.iterrows(), start=1):
        row_bucket = source_bucket
        duplicate_of = _first_value(row, lookup, ("duplicate_of_txn_id",))
        if flat_bucket_labels and duplicate_of:
            row_bucket = "duplicate"

        account_id = _first_value(row, lookup, ("source_account_id", "account_id", "account_number"))
        txn_id = _first_value(row, lookup, ("txn_id",)) or _generated_txn_id(
            account_id, row_bucket, row_number
        )
        raw_doc_id = _first_value(row, lookup, ("doc_id",))
        doc_id = raw_doc_id or doc_map.get(txn_id, source_path.stem)
        date_value = _first_value(row, lookup, ("date",))
        flag_reason = _first_value(row, lookup, ("flag_reason",))
        if row_bucket == "duplicate" and not duplicate_of:
            original_row = _first_value(row, lookup, ("original_row_number",))
            duplicate_of = f"source-row::{original_row}" if original_row else ""

        is_balance_mismatch = "BALANCE_MISMATCH" in normalize_text(flag_reason).replace(" ", "_")
        eligible = row_bucket != "duplicate" and not is_balance_mismatch
        if row_bucket == "duplicate":
            exclusion_reason = "suspected_duplicate"
        elif is_balance_mismatch:
            exclusion_reason = "extraction_error_balance_mismatch"
        else:
            exclusion_reason = ""
        confidence_tier = "low" if row_bucket == "flagged" and eligible else "high"

        reference = _first_value(
            row,
            lookup,
            ("reference_number", "transaction_reference", "transaction_id_external"),
        )
        reference_alt = _first_value(
            row,
            lookup,
            ("transaction_reference", "reference_number", "transaction_id_external"),
        )
        confidence_raw = _first_value(row, lookup, ("confidence_score",))
        try:
            confidence_score = float(confidence_raw) if confidence_raw else None
        except ValueError:
            confidence_score = None

        record = {
            "txn_id": txn_id,
            "doc_id": doc_id,
            "account_id": account_id,
            "source_account_id": account_id,
            "date": parse_date(date_value),
            "date_raw": date_value,
            "time": parse_time(_first_value(row, lookup, ("time",))),
            "narration": _first_value(row, lookup, ("narration",)),
            "reference": reference,
            "reference_alt": reference_alt,
            "debit_amount": parse_amount(_first_value(row, lookup, ("debit_amount",))),
            "credit_amount": parse_amount(_first_value(row, lookup, ("credit_amount",))),
            "balance": parse_amount(_first_value(row, lookup, ("balance",)))
            if lookup.get("balance") is not None
            else None,
            "account_number": _first_value(row, lookup, ("account_number", "account_id")),
            "account_holder": _first_value(row, lookup, ("account_holder",)),
            "bank_name": _first_value(row, lookup, ("bank_name",)),
            "ifsc_code": _first_value(row, lookup, ("ifsc_code",)),
            "confidence_score": confidence_score,
            "extraction_tier": _first_value(row, lookup, ("extraction_tier",)),
            "flag_reason": flag_reason,
            "duplicate_of_txn_id": duplicate_of,
            "source_bucket": row_bucket,
            "eligible_for_detection": bool(eligible),
            "confidence_tier": confidence_tier,
            "exclusion_reason": exclusion_reason,
            "source_file": str(source_path.resolve()),
            "source_row_number": row_number,
            "source_page": _first_value(row, lookup, ("source_page",)),
            "is_reversed_source_label": normalize_text(
                _first_value(row, lookup, ("is_reversed_source_label",))
            )
            in {"TRUE", "1", "YES"},
            "raw_payload_json": json.dumps(row.to_dict(), ensure_ascii=False, default=str),
        }
        records.append(record)
    return records


def _disambiguate_txn_ids(frame: pd.DataFrame) -> pd.DataFrame:
    counts: dict[str, int] = {}
    unique_ids: list[str] = []
    for txn_id in frame["txn_id"].astype(str):
        occurrence = counts.get(txn_id, 0)
        counts[txn_id] = occurrence + 1
        unique_ids.append(txn_id if occurrence == 0 else f"{txn_id}#occurrence-{occurrence + 1}")
    frame = frame.copy()
    frame["txn_id"] = unique_ids
    return frame


def load_inputs(input_path: str | Path) -> NormalizedInput:
    """Inspect headers, merge buckets, and apply the locked eligibility rule."""

    path = Path(input_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(path)

    manifest: list[dict[str, Any]] = []
    all_records: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {}
    input_contract: dict[str, Any] = {}

    if path.is_file():
        # Extract CASA-style metadata (account_id, holder, branch) before reading
        casa_meta = _extract_casa_metadata(path)
        frame = _read_csv(path)

        # If the CSV has no account_id column but CASA metadata yielded one,
        # inject it as a synthetic column so all downstream logic works.
        lookup_test = _column_lookup(list(frame.columns))
        if "account_id" not in lookup_test and casa_meta.get("account_id"):
            frame["account_id"] = casa_meta["account_id"]
        if "account_holder" not in lookup_test and casa_meta.get("account_holder"):
            frame["account_holder"] = casa_meta["account_holder"]

        manifest.append({"path": str(path), "headers": list(frame.columns), "rows": len(frame)})
        all_records.extend(_normalize_frame(frame, path, "clean", {}, flat_bucket_labels=True))
        input_contract = {
            "input_path": str(path),
            "clean_row_count": len(frame),
            "flagged_row_count": 0,
            "duplicate_row_count": int(frame.get("duplicate_of", pd.Series(dtype=str)).astype(str).ne("").sum())
            if "duplicate_of" in frame.columns
            else 0,
            "count_reconciliation_status": "ok",
            "count_reconciliation_detail": "Single flat CSV input.",
            "warnings": [],
        }
    else:
        metadata = _read_metadata(path)
        doc_map = _statement_doc_map(path)
        candidates = [
            ("clean", path / "clean_transactions.csv"),
            ("flagged", path / "flagged_transactions.csv"),
            ("duplicate", path / "duplicate_transactions.csv"),
        ]
        if not candidates[-1][1].exists() and (path / "duplicates.csv").exists():
            candidates[-1] = ("duplicate", path / "duplicates.csv")
        if not candidates[0][1].exists():
            raise FileNotFoundError(f"Missing clean_transactions.csv in {path}")
        for bucket, csv_path in candidates:
            if not csv_path.exists():
                frame = pd.DataFrame()
                manifest.append({"path": str(csv_path), "headers": [], "rows": 0, "missing": True})
                continue
            frame = _read_csv(csv_path)
            manifest.append({"path": str(csv_path), "headers": list(frame.columns), "rows": len(frame)})
            if not frame.empty:
                all_records.extend(_normalize_frame(frame, csv_path, bucket, doc_map))
        input_contract = _build_input_contract(path, manifest, metadata)

    transactions = pd.DataFrame.from_records(all_records)
    if transactions.empty:
        raise ValueError(f"No transaction rows found in {path}")
    transactions = _disambiguate_txn_ids(transactions)
    transactions.insert(0, "source_order", range(1, len(transactions) + 1))
    transactions["narration_normalized"] = transactions["narration"].map(normalize_text)
    transactions["account_id_normalized"] = transactions["account_id"].map(normalize_compact)
    balance_mismatch_mask = (
        transactions["exclusion_reason"].astype(str).str.contains("balance_mismatch", case=False, na=False)
        | transactions["flag_reason"].astype(str).str.contains("balance_mismatch", case=False, na=False)
    )
    input_contract["balance_mismatch_excluded_count"] = int(balance_mismatch_mask.sum())
    input_contract["eligible_row_count"] = int(transactions["eligible_for_detection"].astype(bool).sum())
    input_contract["excluded_row_count"] = int((~transactions["eligible_for_detection"].astype(bool)).sum())
    return NormalizedInput(
        transactions=transactions,
        input_manifest=manifest,
        metadata=metadata,
        input_contract=input_contract,
    )
