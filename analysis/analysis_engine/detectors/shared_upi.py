# Copied from: analysis phase cidecode hackathon/analysis phase cidecode hackathon/analysis_engine/detectors/shared_upi.py
"""Pattern 16: shared UPI identifier across multiple accounts.

Detects the same UPI ID (VPA) appearing in narrations across multiple different
accounts, which may indicate coordinated activity or single-entity control
over multiple accounts.
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict

import pandas as pd

from ..config import AnalysisConfig
from ..database import fetch_transactions
from ..utils import normalize_text
from .common import make_finding

# UPI VPA pattern (e.g., username@bankname)
VPA_RE = re.compile(
    r"(?<![A-Z0-9._-])([A-Z0-9._-]{2,}@[A-Z][A-Z0-9._-]{1,})(?![A-Z0-9._-])",
    re.IGNORECASE,
)

SERVICE_VPA_TOKENS = {
    "airtel",
    "recharge",
    "bill",
    "billpay",
    "biller",
    "bescom",
    "bbdaily",
    "bigbasket",
    "dmart",
    "emi",
    "insurance",
    "utility",
    "utilities",
    "fuel",
    "fuelpay",
    "merchant",
    "paytmqr",
    "razorpay",
    "billdesk",
    "cashfree",
    "ccavenue",
    "payu",
    "juspay",
}


def _is_service_vpa(vpa: str) -> bool:
    compact = normalize_text(vpa).lower().replace("@", " ")
    tokens = set(re.split(r"[^A-Z0-9]+", compact))
    local_part = compact.split(" ", 1)[0] if compact else ""
    return any(token in tokens or token in local_part for token in SERVICE_VPA_TOKENS)


def detect_shared_upi_identifiers(
    connection: sqlite3.Connection,
    baseline: dict,
    config: AnalysisConfig,
) -> list:
    del baseline
    frame = fetch_transactions(connection, "eligible_for_detection = 1")
    if frame.empty:
        return []
    total_accounts = max(1, int(frame["account_id"].astype(str).nunique()))

    # Extract VPAs from narrations and references
    vpa_accounts: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))

    for row in frame.itertuples(index=False):
        narration = str(row.narration or "")
        reference = str(row.reference or "")
        combined = f"{narration} {reference}"

        vpas = VPA_RE.findall(combined)
        if not vpas:
            continue

        account_id = str(row.account_id)
        txn_id = str(row.txn_id)

        for vpa in vpas:
            vpa_lower = vpa.lower()
            vpa_accounts[vpa_lower][account_id].append(txn_id)

    findings = []

    for vpa, account_map in sorted(vpa_accounts.items()):
        account_count = len(account_map)
        if account_count < 2:
            continue
        account_share = account_count / total_accounts
        service_vpa = _is_service_vpa(vpa)
        common_service_vpa = (
            account_count > config.shared_upi_common_account_count
            or (
                account_count > 3
                and account_share >= config.shared_upi_common_account_share
            )
        )
        if service_vpa or common_service_vpa:
            continue

        # This VPA appears across multiple accounts
        all_accounts = sorted(account_map.keys())
        all_txn_ids = []
        account_txn_counts = {}
        for acct, txn_ids in sorted(account_map.items()):
            all_txn_ids.extend(txn_ids)
            account_txn_counts[acct] = len(txn_ids)

        all_txn_ids = list(dict.fromkeys(all_txn_ids))  # deduplicate preserving order

        findings.append(
            make_finding(
                connection,
                16,
                all_accounts,
                all_txn_ids,
                f"UPI identifier '{vpa}' appears across {len(all_accounts)} different accounts: "
                f"{', '.join(all_accounts[:5])}"
                + (f" and {len(all_accounts) - 5} more" if len(all_accounts) > 5 else "")
                + ". This may indicate shared ownership or coordinated activity.",
                {
                    "upi_identifier": vpa,
                    "account_count": len(all_accounts),
                    "total_observed_accounts": total_accounts,
                    "account_share": account_share,
                    "is_service_vpa": service_vpa,
                    "common_service_vpa_suppressed": False,
                    "accounts": all_accounts,
                    "per_account_transaction_counts": account_txn_counts,
                    "total_transactions": len(all_txn_ids),
                    "runtime_thresholds": {
                        "common_account_share": config.shared_upi_common_account_share,
                        "common_account_count": config.shared_upi_common_account_count,
                    },
                },
            )
        )
        if len(findings) >= config.maximum_findings_per_pattern:
            break

    return findings
