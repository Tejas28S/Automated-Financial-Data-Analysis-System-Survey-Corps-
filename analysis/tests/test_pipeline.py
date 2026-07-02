# Copied from: analysis phase cidecode hackathon/analysis phase cidecode hackathon/tests/test_pipeline.py
"""Run the full analysis pipeline on a synthetic test case and validate output."""

import json
import sys
import logging
from pathlib import Path

# Setup path
PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from analysis_engine.config import AnalysisConfig
from analysis_engine.pipeline import AnalysisPipeline
from analysis_engine.output import print_summary

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


def run_synthetic_case(case_path: Path, output_name: str, credit_txn_ids=None):
    """Run pipeline on a single synthetic CSV file."""
    output_dir = PROJECT_DIR / "outputs" / output_name
    config = AnalysisConfig(enable_llm_fallback=False)
    pipeline = AnalysisPipeline(
        input_path=str(case_path),
        output_dir=str(output_dir),
        config=config,
        credit_txn_ids=credit_txn_ids or [],
    )
    result = pipeline.run()
    print_summary(result)
    return result


def main():
    synthetic_dir = PROJECT_DIR / "synthetic_cases"
    
    # ── Test 1: Round Trip case_01 ──
    print("\n" + "="*80)
    print("  TEST: Round Trip Case 01 (Pattern 17)")
    print("="*80)
    rt01 = synthetic_dir / "pattern_01_round_trip" / "rt_case_01.csv"
    if rt01.exists():
        result = run_synthetic_case(rt01, "test_rt_case_01")
        p4_key = "17_round_trip_detection"
        p4_findings = result.findings_by_pattern.get(p4_key, [])
        print(f"\n  Round-trip findings: {len(p4_findings)}")
        for f in p4_findings:
            print(f"    Accounts: {f.accounts}, TxnIDs: {f.txn_ids[:4]}...")
    else:
        print(f"  SKIP: {rt01} not found")

    # ── Test 2: Transit/Layering case_01 ──
    print("\n" + "="*80)
    print("  TEST: Transit/Layering Case 01 (Pattern 3)")
    print("="*80)
    tl01 = synthetic_dir / "pattern_02_transit_layering" / "tl_case_01.csv"
    if tl01.exists():
        result = run_synthetic_case(tl01, "test_tl_case_01")
        p5_key = "3_pass_through_routing_account"
        p5_findings = result.findings_by_pattern.get(p5_key, [])
        print(f"\n  Transit findings: {len(p5_findings)}")
        for f in p5_findings:
            print(f"    Account: {f.accounts}, throughput: {f.details.get('throughput_ratio', 'N/A')}")
    else:
        print(f"  SKIP: {tl01} not found")

    # ── Test 3: Duplicates case_01 ──
    print("\n" + "="*80)
    print("  TEST: Duplicates Case 01 (Patterns 1 & 2)")
    print("="*80)
    dup01 = synthetic_dir / "pattern_06_duplicates" / "dup_case_01.csv"
    if dup01.exists():
        result = run_synthetic_case(dup01, "test_dup_case_01")
        p1_key = "1_duplicate_verification"
        p2_key = "2_failed_reversed_transaction_detection"
        p1 = result.findings_by_pattern.get(p1_key, [])
        p2 = result.findings_by_pattern.get(p2_key, [])
        print(f"\n  Duplicate findings: {len(p1)}")
        print(f"  Reversal findings: {len(p2)}")
        for f in p2:
            print(f"    Reversal: {f.txn_ids}")
    else:
        print(f"  SKIP: {dup01} not found")
    
    # ── Test 4: Structuring case_01 ──
    print("\n" + "="*80)
    print("  TEST: Structuring Case 01 (Pattern 5)")
    print("="*80)
    st01 = synthetic_dir / "pattern_04_structuring" / "st_case_01.csv"
    if st01.exists():
        result = run_synthetic_case(st01, "test_st_case_01")
        p7_key = "5_structuring_smurfing_detection"
        p7 = result.findings_by_pattern.get(p7_key, [])
        print(f"\n  Structuring findings: {len(p7)}")
        for f in p7:
            print(f"    Account: {f.accounts}, date: {f.details.get('date')}, count: {f.details.get('transaction_count')}")
    else:
        print(f"  SKIP: {st01} not found")

    # ── Test 5: Money Trail case_01 ──
    print("\n" + "="*80)
    print("  TEST: Money Trail Case 01 (Pattern 8)")
    print("="*80)
    mt01 = synthetic_dir / "pattern_07_money_trail" / "mt_case_01.csv"
    if mt01.exists():
        result = run_synthetic_case(mt01, "test_mt_case_01", 
                                     credit_txn_ids=["3545244589369467_000026"])
        p10_key = "8_money_trail_tracing"
        p10 = result.findings_by_pattern.get(p10_key, [])
        print(f"\n  Money trail findings: {len(p10)}")
        for f in p10:
            print(f"    Credit: {f.details.get('source_credit_txn_id')}")
            print(f"    Traced: {f.details.get('traced_amount'):.2f} / {f.details.get('credited_amount'):.2f}")
            print(f"    Status: {f.details.get('trace_status')}")
    else:
        print(f"  SKIP: {mt01} not found")

    print("\n" + "="*80)
    print("  ALL TESTS COMPLETE")
    print("="*80)


if __name__ == "__main__":
    main()
