# Copied from: analysis phase cidecode hackathon/analysis phase cidecode hackathon/tests/test_heldout_cases.py
"""Run ALL held-out case_03 files once per pattern (Testing Protocol compliance)."""

import json
import sys
import logging
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from analysis_engine.config import AnalysisConfig
from analysis_engine.pipeline import AnalysisPipeline
from analysis_engine.output import print_summary

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")

SYNTHETIC = PROJECT_DIR / "synthetic_cases"
results = []


def run(path, name, credits=None):
    out = PROJECT_DIR / "outputs" / f"heldout_{name}"
    p = AnalysisPipeline(str(path), str(out), AnalysisConfig(enable_llm_fallback=False), credits or [])
    return p.run()


def report(label, condition, detail=""):
    status = "[PASS]" if condition else "[FAIL]"
    results.append((label, condition))
    msg = f"  {status} {label}"
    if detail:
        msg += f"  -- {detail}"
    print(msg)
    return condition


print("=" * 80)
print("HELD-OUT CASE 03 VALIDATION (single run per pattern)")
print("=" * 80)

# ── Pattern 4: Round Trip case_03 ──
print("\n--- Pattern 4: Round Trip (held-out) ---")
rt03 = SYNTHETIC / "pattern_01_round_trip" / "rt_case_03.csv"
if rt03.exists():
    r = run(rt03, "rt03")
    p4 = r.findings_by_pattern.get("17_round_trip_detection", [])
    report("rt_case_03: found round-trip findings", len(p4) >= 1, f"count={len(p4)}")
    if p4:
        all_accts = set()
        for f in p4:
            all_accts.update(f.accounts)
        report("rt_case_03: round-trip involves >= 2 accounts", len(all_accts) >= 2, f"accounts={all_accts}")
    p3 = r.findings_by_pattern.get("3_balance_consistency_validation", [])
    report("rt_case_03: zero unexplained balance findings", len(p3) == 0, f"balance_findings={len(p3)}")
else:
    report("rt_case_03: file exists", False, "MISSING")

# ── Pattern 5: Transit case_03 ──
print("\n--- Pattern 5: Transit/Layering (held-out) ---")
tl03 = SYNTHETIC / "pattern_02_transit_layering" / "tl_case_03.csv"
if tl03.exists():
    r = run(tl03, "tl03")
    p5 = r.findings_by_pattern.get("3_pass_through_routing_account", [])
    report("tl_case_03: found transit finding", len(p5) >= 1, f"count={len(p5)}")
    if p5:
        accts = [a for f in p5 for a in f.accounts]
        report("tl_case_03: conduit account identified", len(accts) >= 1, f"accounts={accts}")
else:
    report("tl_case_03: file exists", False, "MISSING")

# ── Pattern 6: Accumulation case_03 ──
print("\n--- Pattern 6: Accumulation (held-out) ---")
ac03 = SYNTHETIC / "pattern_03_accumulation" / "ac_case_03.csv"
if ac03.exists():
    r = run(ac03, "ac03")
    p6 = r.findings_by_pattern.get("4_fund_pooling_account", [])
    report("ac_case_03: found accumulation finding", len(p6) >= 1, f"count={len(p6)}")
    if p6:
        accts = [a for f in p6 for a in f.accounts]
        report("ac_case_03: accumulator identified", len(accts) >= 1, f"accounts={accts}")
else:
    report("ac_case_03: file exists", False, "MISSING")

# ── Pattern 7: Structuring case_03 ──
print("\n--- Pattern 7: Structuring (held-out) ---")
st03 = SYNTHETIC / "pattern_04_structuring" / "st_case_03.csv"
if st03.exists():
    r = run(st03, "st03")
    p7 = r.findings_by_pattern.get("5_structuring_smurfing_detection", [])
    report("st_case_03: found structuring findings", len(p7) >= 1, f"count={len(p7)}")
    if p7:
        accts = [a for f in p7 for a in f.accounts]
        dates = [f.details.get("date") for f in p7]
        report("st_case_03: structuring account identified", len(set(accts)) >= 1, f"accounts={set(accts)}, dates={dates}")
else:
    report("st_case_03: file exists", False, "MISSING")

# ── Patterns 1 & 2: Duplicates/Reversals case_03 ──
print("\n--- Pattern 1 & 2: Duplicates/Reversals (held-out) ---")
dup03 = SYNTHETIC / "pattern_06_duplicates" / "dup_case_03.csv"
if dup03.exists():
    r = run(dup03, "dup03")
    p1 = r.findings_by_pattern.get("1_duplicate_verification", [])
    p2 = r.findings_by_pattern.get("2_failed_reversed_transaction_detection", [])
    report("dup_case_03: found duplicate findings", len(p1) >= 1, f"count={len(p1)}")
    report("dup_case_03: found reversal findings", len(p2) >= 1, f"count={len(p2)}")
else:
    report("dup_case_03: file exists", False, "MISSING")

# ── Pattern 9: Circular Flow case_03 ──
print("\n--- Pattern 9: Circular Flow (held-out) ---")
cf03 = SYNTHETIC / "pattern_09_circular_flow" / "cf_case_03.csv"
if cf03.exists():
    r = run(cf03, "cf03")
    p9 = r.findings_by_pattern.get("7_circular_flow_multi_hop_cycle_detection", [])
    report("cf_case_03: found circular flow", len(p9) >= 1, f"count={len(p9)}")
    if p9:
        accts = set()
        for f in p9:
            accts.update(f.accounts)
        report("cf_case_03: cycle involves >= 3 accounts", len(accts) >= 3, f"accounts={accts}")
else:
    report("cf_case_03: file exists", False, "MISSING")

# ── Pattern 10: Money Trail case_03 ──
print("\n--- Pattern 10: Money Trail (held-out) ---")
mt03 = SYNTHETIC / "pattern_07_money_trail" / "mt_case_03.csv"
if mt03.exists():
    # First run without credits to inspect the data and find a credit transaction
    r_inspect = run(mt03, "mt03_inspect")
    # Find any substantial credit transaction to trace
    import sqlite3
    import pandas as pd
    db_path = PROJECT_DIR / "outputs" / "heldout_mt03_inspect" / "analysis.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    credits_df = pd.read_sql_query(
        "SELECT txn_id, credit_amount, account_id FROM transactions WHERE eligible_for_detection = 1 AND credit_amount > 100000 ORDER BY credit_amount DESC LIMIT 5",
        conn,
    )
    conn.close()
    if not credits_df.empty:
        top_credit = credits_df.iloc[0]["txn_id"]
        credit_amount = credits_df.iloc[0]["credit_amount"]
        print(f"  Tracing top credit: {top_credit} (amount={credit_amount})")
        r = run(mt03, "mt03", credits=[top_credit])
        p10 = r.findings_by_pattern.get("8_money_trail_tracing", [])
        report("mt_case_03: traced credit successfully", len(p10) >= 1, f"count={len(p10)}")
        if p10:
            report("mt_case_03: FIFO allocations produced", len(p10[0].details.get("allocations", [])) >= 1,
                   f"allocations={len(p10[0].details.get('allocations', []))}, status={p10[0].details.get('trace_status')}")
    else:
        report("mt_case_03: found credit transactions to trace", False, "no eligible credits > 100000")
else:
    report("mt_case_03: file exists", False, "MISSING")

# ── Pattern 8: Aggregation/Graph case_03 ──
print("\n--- Pattern 8: Graph Construction (held-out) ---")
agg03 = SYNTHETIC / "pattern_08_aggregation" / "agg_case_03.csv"
if agg03.exists():
    r = run(agg03, "agg03")
    report("agg_case_03: graph has nodes", r.graph.number_of_nodes() > 0, f"nodes={r.graph.number_of_nodes()}, edges={r.graph.number_of_edges()}")
    report("agg_case_03: graph has edges", r.graph.number_of_edges() > 0, f"edges={r.graph.number_of_edges()}")
else:
    report("agg_case_03: file exists", False, "MISSING")

# ── Burst case_03 ──
print("\n--- Cross-pattern: Burst (held-out) ---")
ba03 = SYNTHETIC / "pattern_05_burst_activity" / "ba_case_03.csv"
if ba03.exists():
    r = run(ba03, "ba03")
    total_findings = sum(len(fl) for fl in r.findings_by_pattern.values())
    report("ba_case_03: pipeline completes without error", True, f"total_findings={total_findings}")
else:
    report("ba_case_03: file exists", False, "MISSING")

# ── Combined case_03 ──
print("\n--- Cross-pattern: Combined (held-out) ---")
combo03 = SYNTHETIC / "pattern_10_combined" / "combo_case_03.csv"
if combo03.exists():
    r = run(combo03, "combo03")
    total_findings = sum(len(fl) for fl in r.findings_by_pattern.values())
    report("combo_case_03: pipeline completes without error", True, f"total_findings={total_findings}")
else:
    report("combo_case_03: file exists", False, "MISSING")

# ── SUMMARY ──
print("\n" + "=" * 80)
passed = sum(1 for _, ok in results if ok)
total = len(results)
print(f"HELD-OUT RESULTS: {passed}/{total} checks passed")
if passed < total:
    print("\nFAILED:")
    for label, ok in results:
        if not ok:
            print(f"  [FAIL] {label}")
print("=" * 80)
