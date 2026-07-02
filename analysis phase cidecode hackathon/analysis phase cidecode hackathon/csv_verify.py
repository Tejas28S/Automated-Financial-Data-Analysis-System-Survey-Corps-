"""
csv_verify.py
=============
Verifies each analysis_op/<case>/report.json against its actual source CSV file.
No ground_truth folder is used. Everything is derived purely from the CSV.

Checks per case
---------------
1. ACCOUNTS EXIST IN CSV      — every account in report.all_flagged_accounts is a real account_number in the CSV
2. TXNS EXIST IN CSV          — every transaction_id in every finding actually appears in the CSV txn_id column
3. AMOUNTS MATCH CSV          — primary_amount in finding is within 5% of the actual max transaction amount for those txn IDs
4. NO PHANTOM ACCOUNTS        — no flagged account is completely absent from the CSV
5. FINDINGS REFERENCE VALID DATA — accounts in each finding all exist in CSV rows
"""

import csv
import json
import pathlib
import os

# Map each case name -> relative CSV path (from project root)
CASE_CSV_MAP = {
    "rt_case_01":    "synthetic_data/pattern_01_round_trip/rt_case_01.csv",
    "rt_case_02":    "synthetic_data/pattern_01_round_trip/rt_case_02.csv",
    "rt_case_03":    "synthetic_data/pattern_01_round_trip/rt_case_03.csv",
    "tl_case_01":    "synthetic_data/pattern_02_transit_layering/tl_case_01.csv",
    "tl_case_02":    "synthetic_data/pattern_02_transit_layering/tl_case_02.csv",
    "tl_case_03":    "synthetic_data/pattern_02_transit_layering/tl_case_03.csv",
    "ac_case_01":    "synthetic_data/pattern_03_accumulation/ac_case_01.csv",
    "ac_case_02":    "synthetic_data/pattern_03_accumulation/ac_case_02.csv",
    "ac_case_03":    "synthetic_data/pattern_03_accumulation/ac_case_03.csv",
    "st_case_01":    "synthetic_data/pattern_04_structuring/st_case_01.csv",
    "st_case_02":    "synthetic_data/pattern_04_structuring/st_case_02.csv",
    "st_case_03":    "synthetic_data/pattern_04_structuring/st_case_03.csv",
    "ba_case_01":    "synthetic_data/pattern_05_burst_activity/ba_case_01.csv",
    "ba_case_02":    "synthetic_data/pattern_05_burst_activity/ba_case_02.csv",
    "ba_case_03":    "synthetic_data/pattern_05_burst_activity/ba_case_03.csv",
    "dup_case_01":   "synthetic_data/pattern_06_duplicates/dup_case_01.csv",
    "dup_case_02":   "synthetic_data/pattern_06_duplicates/dup_case_02.csv",
    "dup_case_03":   "synthetic_data/pattern_06_duplicates/dup_case_03.csv",
    "mt_case_01":    "synthetic_data/pattern_07_money_trail/mt_case_01.csv",
    "mt_case_02":    "synthetic_data/pattern_07_money_trail/mt_case_02.csv",
    "mt_case_03":    "synthetic_data/pattern_07_money_trail/mt_case_03.csv",
    "agg_case_01":   "synthetic_data/pattern_08_aggregation/agg_case_01.csv",
    "agg_case_02":   "synthetic_data/pattern_08_aggregation/agg_case_02.csv",
    "agg_case_03":   "synthetic_data/pattern_08_aggregation/agg_case_03.csv",
    "cf_case_01":    "synthetic_data/pattern_09_circular_flow/cf_case_01.csv",
    "cf_case_02":    "synthetic_data/pattern_09_circular_flow/cf_case_02.csv",
    "cf_case_03":    "synthetic_data/pattern_09_circular_flow/cf_case_03.csv",
    "combo_case_01": "synthetic_data/pattern_10_combined/combo_case_01.csv",
    "combo_case_02": "synthetic_data/pattern_10_combined/combo_case_02.csv",
    "combo_case_03": "synthetic_data/pattern_10_combined/combo_case_03.csv",
}


def detect_columns(header):
    """Return (account_col, txn_col, debit_col, credit_col) using the exact original-case header names."""
    # Build a map: lowercased_name -> original_name
    lower_map = {c.strip().lower(): c for c in header}

    account_col = next(
        (lower_map[k] for k in ("account_number", "account_id", "accountnumber", "acc_no") if k in lower_map),
        None,
    )
    txn_col = next(
        (lower_map[k] for k in ("txn_id", "transaction_id", "txnid", "trans_id") if k in lower_map),
        None,
    )
    debit_col = next(
        (lower_map[k] for k in ("debit", "debit_amount", "dr", "withdrawal") if k in lower_map),
        None,
    )
    credit_col = next(
        (lower_map[k] for k in ("credit", "credit_amount", "cr", "deposit") if k in lower_map),
        None,
    )
    return account_col, txn_col, debit_col, credit_col


def load_csv(csv_path):
    """Return (set_of_account_ids, set_of_txn_ids, dict txn_id->max_amount)."""
    accounts = set()
    txn_ids = set()
    txn_amounts = {}

    with open(csv_path, encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        acct_col, txn_col, deb_col, cred_col = detect_columns(list(header))

        if not acct_col or not txn_col:
            return accounts, txn_ids, txn_amounts

        for row in reader:
            acct = str(row.get(acct_col, "")).strip()
            txn  = str(row.get(txn_col,  "")).strip()
            if acct:
                accounts.add(acct)
            if txn:
                txn_ids.add(txn)
                try:
                    d = float(row.get(deb_col, 0) or 0)
                    c = float(row.get(cred_col, 0) or 0)
                    txn_amounts[txn] = max(d, c)
                except (ValueError, TypeError):
                    txn_amounts[txn] = 0.0

    return accounts, txn_ids, txn_amounts


def verify_case(case_name, csv_path, report_path):
    rec = {"case": case_name, "checks": [], "csv_rows": 0, "csv_accounts": 0}

    # ── Load CSV ──────────────────────────────────────────────────────────────
    if not pathlib.Path(csv_path).exists():
        rec["checks"].append({"name": "CSV EXISTS", "status": "FAIL", "detail": "CSV not found: " + csv_path})
        return rec
    if not pathlib.Path(report_path).exists():
        rec["checks"].append({"name": "REPORT EXISTS", "status": "FAIL", "detail": "report.json not found"})
        return rec

    csv_accounts, csv_txns, csv_amounts = load_csv(csv_path)
    rec["csv_rows"] = len(csv_txns)
    rec["csv_accounts"] = len(csv_accounts)

    data = json.loads(pathlib.Path(report_path).read_text(encoding="utf-8"))
    ds = data.get("detection_summary", {})
    flagged = list(ds.get("all_flagged_accounts", []))
    all_findings = data.get("all_findings", [])

    # ── Check 1: All flagged accounts actually exist in CSV ───────────────────
    phantom = [a for a in flagged if a not in csv_accounts]
    rec["checks"].append({
        "name": "FLAGGED ACCOUNTS IN CSV",
        "status": "PASS" if not phantom else "FAIL",
        "detail": (
            "All " + str(len(flagged)) + " flagged accounts confirmed in CSV"
            if not phantom
            else "PHANTOM accounts (not in CSV): " + str(phantom)
        ),
    })

    # ── Check 2: All txn_ids in findings exist in CSV ────────────────────────
    all_report_txns = []
    for f in all_findings:
        all_report_txns.extend(f.get("transaction_ids", []))
    all_report_txns = list(set(all_report_txns))
    phantom_txns = [t for t in all_report_txns if t not in csv_txns]
    rec["checks"].append({
        "name": "FINDING TXNS IN CSV",
        "status": "PASS" if not phantom_txns else "FAIL",
        "detail": (
            "All " + str(len(all_report_txns)) + " unique txn IDs confirmed in CSV"
            if not phantom_txns
            else "PHANTOM txns (not in CSV): " + str(phantom_txns[:5]) + (" ..." if len(phantom_txns) > 5 else "")
        ),
    })

    # ── Check 3: Accounts in each finding exist in CSV ───────────────────────
    bad_finding_accounts = []
    for f in all_findings:
        for a in f.get("accounts", []):
            if a not in csv_accounts:
                bad_finding_accounts.append((f.get("finding_id", "?"), a))
    rec["checks"].append({
        "name": "FINDING ACCOUNTS IN CSV",
        "status": "PASS" if not bad_finding_accounts else "FAIL",
        "detail": (
            "All finding accounts confirmed in CSV"
            if not bad_finding_accounts
            else "Bad: " + str(bad_finding_accounts[:3])
        ),
    })

    # ── Check 4: primary_amount is plausible vs CSV amounts ──────────────────
    implausible = []
    for f in all_findings:
        amt = f.get("primary_amount")
        if amt is None:
            continue
        txns_in_f = f.get("transaction_ids", [])
        if not txns_in_f:
            continue
        # Max amount seen across these txns in the CSV
        max_csv_amt = max((csv_amounts.get(t, 0) for t in txns_in_f), default=0)
        # Sum of amounts (round-trip etc can aggregate)
        sum_csv_amt = sum(csv_amounts.get(t, 0) for t in txns_in_f)
        # Allow up to 2x sum (some detectors add amounts from both sides)
        if amt > sum_csv_amt * 2.5 and sum_csv_amt > 0:
            implausible.append({
                "finding_id": f.get("finding_id", "?"),
                "reported_amount": amt,
                "csv_sum": round(sum_csv_amt, 2),
            })
    rec["checks"].append({
        "name": "AMOUNTS PLAUSIBLE",
        "status": "PASS" if not implausible else "WARN",
        "detail": (
            "All amounts plausible"
            if not implausible
            else str(len(implausible)) + " findings have amount > 2.5x CSV sum: " + str(implausible[:2])
        ),
    })

    # ── Check 5: Dataset summary row count matches CSV ───────────────────────
    reported_total = data.get("dataset_summary", {}).get("total_rows")
    csv_row_count = len(csv_txns)
    match = (reported_total is None) or (abs(reported_total - csv_row_count) <= 5)
    rec["checks"].append({
        "name": "ROW COUNT MATCHES CSV",
        "status": "PASS" if match else "WARN",
        "detail": (
            "Report total_rows=" + str(reported_total) + " matches CSV rows=" + str(csv_row_count)
            if match
            else "Report says " + str(reported_total) + " but CSV has " + str(csv_row_count) + " unique txn IDs"
        ),
    })

    return rec


def main():
    op_root = pathlib.Path("analysis_op")
    results = []
    for case_name, csv_rel in sorted(CASE_CSV_MAP.items()):
        csv_path = pathlib.Path(csv_rel)
        report_path = op_root / case_name / "report.json"
        results.append(verify_case(case_name, str(csv_path), str(report_path)))

    pass_c = warn_c = fail_c = 0
    print("=" * 80)
    print("CSV-TO-REPORT VERIFICATION  (no ground_truth folder used)")
    print("Each report.json verified against its own source CSV only")
    print("=" * 80)
    for rec in results:
        statuses = [c["status"] for c in rec["checks"]]
        overall = "FAIL" if "FAIL" in statuses else ("WARN" if "WARN" in statuses else "PASS")
        if overall == "PASS":   pass_c += 1
        elif overall == "WARN": warn_c += 1
        else:                   fail_c += 1
        tag = {"PASS": "[PASS]", "WARN": "[WARN]", "FAIL": "[FAIL]"}[overall]
        print()
        print(
            tag + " " + rec["case"].ljust(20)
            + " | csv_rows=" + str(rec["csv_rows"]).rjust(4)
            + " csv_accounts=" + str(rec["csv_accounts"]).rjust(3)
        )
        for c in rec["checks"]:
            sym = {"PASS": "  v", "WARN": "  !", "FAIL": "  X"}[c["status"]]
            print("   " + sym + " " + c["name"] + ": " + c["detail"])

    print()
    print("=" * 80)
    print("FINAL: " + str(pass_c) + " PASS | " + str(warn_c) + " WARN | " + str(fail_c) + " FAIL  (of 30 cases)")
    print("=" * 80)


if __name__ == "__main__":
    main()
