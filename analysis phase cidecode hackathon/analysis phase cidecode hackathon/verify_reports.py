"""
verify_reports.py
=================
Cross-checks every generated analysis_op/<case>/report.json against the
ground-truth defined in GROUND_TRUTH_SUMMARY.md.

Checks per case
---------------
1. FRAUD ACCOUNTS CAUGHT    – at least one GT fraud account appears in flagged_accounts
2. NO FALSE POSITIVES       – no GT innocent account appears in flagged_accounts
3. KEY TXNS IN FINDINGS     – representative key transactions appear in any finding's txn list
4. FINDINGS WELL-FORMED     – every finding has finding_id, accounts, transaction_ids
"""

import json
import pathlib

# ────────────────────────────────────────────────────────────────────────────
# Ground truth (from GROUND_TRUTH_SUMMARY.md)
# ────────────────────────────────────────────────────────────────────────────
GT = {
    "rt_case_01": {
        "fraud_accounts": {"21081385298452", "51703956546", "76469732163180"},
        "innocent": {"261647363933", "384961901558", "52401335283"},
        "key_txns": {"21081385298452_000022", "21081385298452_000028",
                     "51703956546_000018", "76469732163180_000019"},
        "pattern": "Round Trip",
    },
    "rt_case_02": {
        "fraud_accounts": {"16400202955", "82389122072698", "67947948262536"},
        "innocent": {"261647363933", "384961901558", "52401335283"},
        "key_txns": {"16400202955_000021", "82389122072698_000018"},
        "pattern": "Round Trip",
    },
    "rt_case_03": {
        "fraud_accounts": {"28317586857", "29663337463975", "92247771802759"},
        "innocent": {"261647363933"},
        "key_txns": {"28317586857_000019", "29663337463975_000020", "92247771802759_000021"},
        "pattern": "Round Trip",
    },
    "tl_case_01": {
        "fraud_accounts": {"6793198740", "721215922125", "44883153319"},
        "innocent": {"15219673978"},
        "key_txns": {"6793198740_000018", "721215922125_000020"},
        "pattern": "Transit",
    },
    "tl_case_02": {
        "fraud_accounts": {"28858586401516", "98598169111", "64876803668202"},
        "innocent": {"3596738792"},
        "key_txns": {"28858586401516_000015", "98598169111_000015"},
        "pattern": "Transit",
    },
    "tl_case_03": {
        "fraud_accounts": {"6392324879252041", "105195161443", "60496617950999"},
        "innocent": {"62156833748"},
        "key_txns": {"6392324879252041_000015", "105195161443_000017"},
        "pattern": "Transit",
    },
    "ac_case_01": {
        "fraud_accounts": {"906189047158", "4435913768653", "22999110146", "42963560676"},
        "innocent": {"60697697777855"},
        "key_txns": {"906189047158_000019", "4435913768653_000016", "42963560676_000017"},
        "pattern": "Accumulation",
    },
    "ac_case_02": {
        "fraud_accounts": {"40950368208", "7837260444928914", "68881455666132", "82127478352"},
        "innocent": {"12284972979920"},
        "key_txns": {"40950368208_000024", "7837260444928914_000018"},
        "pattern": "Accumulation",
    },
    "ac_case_03": {
        "fraud_accounts": {"82103150246875", "7841527058376400", "3431701808", "51445752807598"},
        "innocent": {"430208521043"},
        "key_txns": {"82103150246875_000015", "3431701808_000020", "51445752807598_000018"},
        "pattern": "Accumulation",
    },
    "st_case_01": {
        "fraud_accounts": {"663458340862"},
        "innocent": {"9682098120335"},
        "key_txns": {"663458340862_000021", "663458340862_000022"},
        "pattern": "Structuring",
    },
    "st_case_02": {
        "fraud_accounts": {"69200978299933"},
        "innocent": {"94921392322"},
        "key_txns": {"69200978299933_000022", "69200978299933_000023"},
        "pattern": "Structuring",
    },
    "st_case_03": {
        "fraud_accounts": {"30033115798987"},
        "innocent": {"8109737393647"},
        "key_txns": {"30033115798987_000022", "30033115798987_000023"},
        "pattern": "Structuring",
    },
    "ba_case_01": {
        "fraud_accounts": {"52085645014", "301880669130", "42403500363456"},
        "innocent": {"46430145038"},
        "key_txns": {"52085645014_000054", "52085645014_000055"},
        "pattern": "Burst Activity",
    },
    "ba_case_02": {
        "fraud_accounts": {"2170855136", "8197880275022", "97717246673297"},
        "innocent": {"39640208296"},
        "key_txns": {"2170855136_000060", "2170855136_000061"},
        "pattern": "Burst Activity",
    },
    "ba_case_03": {
        "fraud_accounts": {"531475372764", "39339860232963", "2617869067302634"},
        "innocent": {"1998728735417"},
        "key_txns": {"531475372764_000058", "531475372764_000059"},
        "pattern": "Burst Activity",
    },
    "dup_case_01": {
        "fraud_accounts": {"19378026502785", "79955762294", "8455857487204638"},
        "innocent": {"947456503875"},
        "key_txns": {"19378026502785_000024", "19378026502785_000025", "79955762294_000033"},
        "pattern": "Duplicate",
    },
    "dup_case_02": {
        "fraud_accounts": {"9094834319063919", "14046264793", "61034063526"},
        "innocent": {"28336138650684"},
        "key_txns": {"9094834319063919_000021", "9094834319063919_000022", "14046264793_000029"},
        "pattern": "Duplicate",
    },
    "dup_case_03": {
        "fraud_accounts": {"60274405686", "342949044011", "219320109917"},
        "innocent": {"21873482108351"},
        "key_txns": {"60274405686_000026", "60274405686_000027"},
        "pattern": "Duplicate",
    },
    "mt_case_01": {
        "fraud_accounts": {"3545244589369467", "89477031740948", "6564227161247", "50603887137"},
        "innocent": {"959195047945"},
        "key_txns": {"3545244589369467_000026", "89477031740948_000030"},
        "pattern": "Money Trail",
    },
    "mt_case_02": {
        "fraud_accounts": {"47569602855", "45824806118187", "443615501854", "39062252019"},
        "innocent": {"68578940765363"},
        "key_txns": {"47569602855_000029", "45824806118187_000030"},
        "pattern": "Money Trail",
    },
    "mt_case_03": {
        "fraud_accounts": {"65805264167801", "101021785929", "95599894774085", "80275595460"},
        "innocent": {"4250634202"},
        "key_txns": {"65805264167801_000026", "101021785929_000028"},
        "pattern": "Money Trail",
    },
    "agg_case_01": {
        "fraud_accounts": {"18808003162473", "3163841068821604", "9976956729194", "35588709529484"},
        "innocent": {"331255803196"},
        "key_txns": {"18808003162473_000022", "18808003162473_000023"},
        "pattern": "Aggregation",
    },
    "agg_case_02": {
        "fraud_accounts": {"86957120095922", "5074474330030", "69373695548", "84392586324366"},
        "innocent": {"56144677627303"},
        "key_txns": {"86957120095922_000022", "86957120095922_000023"},
        "pattern": "Aggregation",
    },
    "agg_case_03": {
        "fraud_accounts": {"98039194778", "5356944236978633", "83389651038594", "7901090204782"},
        "innocent": {"77064670041"},
        "key_txns": {"98039194778_000017", "98039194778_000018"},
        "pattern": "Aggregation",
    },
    "cf_case_01": {
        "fraud_accounts": {"40154448554135", "12853535777", "192811835953", "2707954975"},
        "innocent": {"27084980361"},
        "key_txns": {"40154448554135_000019", "12853535777_000020", "192811835953_000021"},
        "pattern": "Circular Flow",
    },
    "cf_case_02": {
        "fraud_accounts": {"2264541082227", "4412553087", "14170744216", "81137151773710"},
        "innocent": {"60651647111"},
        "key_txns": {"2264541082227_000022", "4412553087_000020", "14170744216_000025"},
        "pattern": "Circular Flow",
    },
    "cf_case_03": {
        "fraud_accounts": {"16043820598", "86358940321701", "78475377216413", "82478389358"},
        "innocent": {"9173060573203056"},
        "key_txns": {"16043820598_000022", "86358940321701_000022", "78475377216413_000022"},
        "pattern": "Circular Flow",
    },
    "combo_case_01": {
        "fraud_accounts": {"17502554280", "600329946641", "7938492694", "93704090560413"},
        "innocent": {"53979974934441"},
        "key_txns": {"17502554280_000020", "600329946641_000019", "7938492694_000022"},
        "pattern": "Combined",
    },
    "combo_case_02": {
        "fraud_accounts": {"35014923082", "5247835235396", "54859072041765", "259296579885"},
        "innocent": {"15686622603501"},
        "key_txns": {"35014923082_000013", "35014923082_000026"},
        "pattern": "Combined",
    },
    "combo_case_03": {
        "fraud_accounts": {"13955638706921", "58963443476721", "2245428367", "340479896795"},
        "innocent": {"62563923085"},
        "key_txns": {"13955638706921_000032", "58963443476721_000031"},
        "pattern": "Combined",
    },
}


def verify_case(case_name, gt, op_root):
    report_path = op_root / case_name / "report.json"
    rec = {
        "case": case_name,
        "pattern": gt["pattern"],
        "checks": [],
        "total_findings": 0,
        "total_flagged": 0,
        "patterns_triggered": 0,
    }

    if not report_path.exists():
        rec["checks"].append(
            {"name": "REPORT EXISTS", "status": "FAIL", "detail": "File not found"}
        )
        return rec

    data = json.loads(report_path.read_text(encoding="utf-8"))

    # New format: flagged_accounts is a list of dicts with account_id
    flagged_accounts_list = data.get("flagged_accounts", [])
    flagged = set(str(a.get("account_id", "")) for a in flagged_accounts_list)

    # New format: key_transactions is a flat list of txn_ids
    all_txns = set(data.get("key_transactions", []))

    # New format: patterns_detected is a list of dicts
    patterns_list = data.get("patterns_detected", [])
    total_findings = sum(p.get("findings_count", 0) for p in patterns_list)

    rec["total_findings"] = total_findings
    rec["total_flagged"] = len(flagged)
    rec["patterns_triggered"] = len(patterns_list)
    rec["flagged_accounts"] = sorted(flagged)

    # ── Check 1: At least one fraud account caught ──────────────────────────
    caught = flagged & gt["fraud_accounts"]
    missed = gt["fraud_accounts"] - flagged
    rec["checks"].append({
        "name": "FRAUD ACCOUNTS CAUGHT",
        "status": "PASS" if caught else "FAIL",
        "detail": (
            "Caught: " + str(sorted(caught)) +
            (" | Missed: " + str(sorted(missed)) if missed else "")
        ),
    })

    # ── Check 2: No innocent accounts falsely flagged ───────────────────────
    false_pos = flagged & gt["innocent"]
    rec["checks"].append({
        "name": "NO FALSE POSITIVES",
        "status": "PASS" if not false_pos else "WARN",
        "detail": "Clean" if not false_pos else "FP: " + str(sorted(false_pos)),
    })

    # ── Check 3: Key transactions appear in findings ────────────────────────
    hit = {k for k in gt["key_txns"] if k in all_txns}
    rec["checks"].append({
        "name": "KEY TXNS IN FINDINGS",
        "status": "PASS" if hit else "WARN",
        "detail": str(len(hit)) + "/" + str(len(gt["key_txns"])) + " key txns found",
    })

    # ── Check 4: All findings are well-formed ──────────────────────────────
    bad_count = 0
    total_finding_count = 0
    for p in patterns_list:
        for f in p.get("findings", []):
            total_finding_count += 1
            if not f.get("finding_id") or not f.get("accounts"):
                bad_count += 1
    rec["checks"].append({
        "name": "FINDINGS WELL-FORMED",
        "status": "PASS" if bad_count == 0 else "FAIL",
        "detail": "All " + str(total_finding_count) + " findings OK" if bad_count == 0 else str(bad_count) + " malformed",
    })

    return rec


def main():
    op_root = pathlib.Path("analysis_op")
    results = []
    for case_name, gt in sorted(GT.items()):
        results.append(verify_case(case_name, gt, op_root))

    pass_c = warn_c = fail_c = 0
    print("=" * 80)
    print("REPORT VERIFICATION  —  analysis_op vs GROUND_TRUTH_SUMMARY.md")
    print("=" * 80)
    for rec in results:
        statuses = [c["status"] for c in rec["checks"]]
        overall = "FAIL" if "FAIL" in statuses else ("WARN" if "WARN" in statuses else "PASS")
        if overall == "PASS":
            pass_c += 1
        elif overall == "WARN":
            warn_c += 1
        else:
            fail_c += 1
        tag = {"PASS": "[PASS]", "WARN": "[WARN]", "FAIL": "[FAIL]"}[overall]
        print()
        print(
            tag + " "
            + rec["case"].ljust(20)
            + " | "
            + rec["pattern"].ljust(14)
            + " | findings=" + str(rec["total_findings"]).rjust(4)
            + " flagged=" + str(rec["total_flagged"]).rjust(3)
            + " patterns=" + str(rec["patterns_triggered"])
        )
        for c in rec["checks"]:
            sym = {"PASS": "  v", "WARN": "  !", "FAIL": "  X"}[c["status"]]
            print("   " + sym + " " + c["name"] + ": " + c["detail"])

    print()
    print("=" * 80)
    print("FINAL: " + str(pass_c) + " PASS | " + str(warn_c) + " WARN | " + str(fail_c) + " FAIL  (of 30 cases)")
    print("=" * 80)
    return results


if __name__ == "__main__":
    main()
