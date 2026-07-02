# New tests for the root analysis phase implementation.
"""Adversarial edge checks for priority patterns 4, 10, and 19."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analysis_engine.config import AnalysisConfig
from analysis_engine.models import Finding
from analysis_engine.pipeline import AnalysisPipeline
from analysis_engine.scoring import score_accounts


def _write_case(path: Path, rows: list[dict]) -> Path:
    columns = [
        "account_number", "account_holder", "ifsc_code", "Date", "Time", "Narration",
        "Transaction_ID", "Reference_Number", "Transaction_Reference", "Cheque_Number",
        "Debit", "Credit", "Balance", "Transaction_Type", "Bank_Name", "txn_id",
        "duplicate_of", "is_reversed",
    ]
    pd.DataFrame(rows, columns=columns).to_csv(path, index=False)
    return path


def _row(
    account: str,
    idx: int,
    date: str,
    narration: str,
    debit: float,
    credit: float,
    balance: float,
    *,
    reference: str = "",
    duplicate_of: str = "",
) -> dict:
    return {
        "account_number": account,
        "account_holder": f"Holder {account}",
        "ifsc_code": "TEST0000001",
        "Date": date,
        "Time": "",
        "Narration": narration,
        "Transaction_ID": "",
        "Reference_Number": reference,
        "Transaction_Reference": reference or f"REF{account}{idx:03d}",
        "Cheque_Number": "",
        "Debit": debit,
        "Credit": credit,
        "Balance": balance,
        "Transaction_Type": "debit" if debit else "credit",
        "Bank_Name": "Test Bank",
        "txn_id": f"{account}_{idx:06d}",
        "duplicate_of": duplicate_of,
        "is_reversed": False,
    }


def _run(csv_path: Path, name: str, credits: list[str] | None = None):
    out = Path(tempfile.mkdtemp(prefix=f"analysis_priority_{name}_"))
    pipeline = AnalysisPipeline(csv_path, out, AnalysisConfig(enable_llm_fallback=False), credits or [])
    return pipeline.run()


def _assert_confidence_consistent(result) -> None:
    for findings in result.findings_by_pattern.values():
        for finding in findings:
            if finding.confidence_score >= 0.85:
                expected = "high"
            elif finding.confidence_score >= 0.50:
                expected = "medium"
            else:
                expected = "low"
            assert finding.confidence_tier == expected
            assert 0.0 <= finding.confidence_score <= 1.0
    for finding in result.findings_by_pattern.get("10_cross_statement_links", []):
        details = finding.details
        if details.get("corroboration_path") == "reference_match":
            ref_details = details.get("reference_match_details", {})
            assert ref_details.get("shared_references") or ref_details.get("shared_vpas")
        if details.get("corroboration_path") == "narration_match":
            narration_details = details.get("narration_match_details", {})
            assert narration_details.get("narration_similarity_score", 0.0) >= 0.45
            assert narration_details.get("shared_narration_tokens")


def test_priority_round_trip_positive_and_noncycle_negative() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        cycle = _write_case(
            tmp_path / "cycle.csv",
            [
                _row("100001", 0, "01/01/2025", "OPENING", 0, 1000, 1000),
                _row("100001", 1, "02/01/2025", "TRANSFER TO 200002", 100, 0, 900),
                _row("100001", 2, "04/01/2025", "TRANSFER FROM 300003", 0, 95, 995),
                _row("200002", 0, "02/01/2025", "TRANSFER FROM 100001", 0, 100, 100),
                _row("200002", 1, "03/01/2025", "TRANSFER TO 300003", 98, 0, 2),
                _row("300003", 0, "03/01/2025", "TRANSFER FROM 200002", 0, 98, 98),
                _row("300003", 1, "04/01/2025", "TRANSFER TO 100001", 95, 0, 3),
            ],
        )
        result = _run(cycle, "cycle")
        assert len(result.findings_by_pattern["17_round_trip_detection"]) >= 1
        _assert_confidence_consistent(result)

        noncycle = _write_case(
            tmp_path / "noncycle.csv",
            [
                _row("400004", 0, "01/01/2025", "OPENING", 0, 1000, 1000),
                _row("400004", 1, "02/01/2025", "TRANSFER TO 500005", 100, 0, 900),
                _row("500005", 0, "02/01/2025", "TRANSFER FROM 400004", 0, 100, 100),
            ],
        )
        result = _run(noncycle, "noncycle")
        assert len(result.findings_by_pattern["17_round_trip_detection"]) == 0


def test_priority_money_trail_partial_trace() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = _write_case(
            Path(tmp) / "trail.csv",
            [
                _row("600006", 0, "01/01/2025", "OPENING", 0, 1000, 1000),
                _row("600006", 1, "02/01/2025", "LARGE CREDIT", 0, 500, 1500),
                _row("600006", 2, "03/01/2025", "DEBIT TO 700007", 200, 0, 1300),
                _row("600006", 3, "04/01/2025", "DEBIT TO 800008", 100, 0, 1200),
            ],
        )
        result = _run(csv_path, "trail", ["600006_000001"])
        findings = result.findings_by_pattern["8_money_trail_tracing"]
        assert len(findings) == 1
        assert findings[0].details["trace_status"] == "partially_traced"
        assert findings[0].details["traced_amount"] == 300
        _assert_confidence_consistent(result)


def test_common_amount_decoy_does_not_create_amount_only_links() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        rows = []
        for idx in range(8):
            rows.append(_row(f"A{idx:05d}", 0, "01/01/2025", f"UNRELATED PAYMENT OUT {idx}", 850, 0, 1000))
            rows.append(_row(f"B{idx:05d}", 0, "01/01/2025", f"UNRELATED PAYMENT IN {idx}", 0, 850, 1850))
        rows.extend(
            [
                _row("900001", 1, "02/01/2025", "UNRELATED OUT", 850, 0, 150),
                _row("900002", 1, "02/01/2025", "UNRELATED IN", 0, 850, 1850),
                _row("900002", 2, "03/01/2025", "ANOTHER UNRELATED OUT", 850, 0, 1000),
                _row("900001", 2, "03/01/2025", "ANOTHER UNRELATED IN", 0, 850, 1000),
            ]
        )
        csv_path = _write_case(Path(tmp) / "decoy_common_amount.csv", rows)
        result = _run(csv_path, "decoy_common_amount")
        assert len(result.findings_by_pattern["10_cross_statement_links"]) == 0
        assert len(result.findings_by_pattern["17_round_trip_detection"]) == 0
        _assert_confidence_consistent(result)


def test_common_amount_reference_link_still_fires() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        rows = []
        for idx in range(8):
            rows.append(_row(f"C{idx:05d}", 0, "01/01/2025", f"BACKGROUND OUT {idx}", 850, 0, 1000))
            rows.append(_row(f"D{idx:05d}", 0, "01/01/2025", f"BACKGROUND IN {idx}", 0, 850, 1850))
        rows.extend(
            [
                _row("910001", 1, "02/01/2025", "TRANSFER TO 910002", 850, 0, 150, reference="LINK1"),
                _row("910002", 1, "02/01/2025", "TRANSFER FROM 910001", 0, 850, 1850, reference="LINK1"),
                _row("910002", 2, "03/01/2025", "TRANSFER TO 910001", 850, 0, 1000, reference="LINK2"),
                _row("910001", 2, "03/01/2025", "TRANSFER FROM 910002", 0, 850, 1000, reference="LINK2"),
            ]
        )
        csv_path = _write_case(Path(tmp) / "reference_common_amount.csv", rows)
        result = _run(csv_path, "reference_common_amount")
        assert len(result.findings_by_pattern["10_cross_statement_links"]) >= 2
        assert len(result.findings_by_pattern["17_round_trip_detection"]) >= 1
        assert all(
            f.details.get("corroboration_path") != "blocked_common_amount_mirror_match"
            for f in result.findings_by_pattern["17_round_trip_detection"]
        )
        _assert_confidence_consistent(result)


def test_fake_reference_and_narration_methods_do_not_correlate_common_amounts() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        rows = []
        for idx in range(12):
            rows.append(_row(f"F{idx:05d}", 0, "01/01/2025", f"BACKGROUND OUT {idx}", 850, 0, 1000))
            rows.append(_row(f"G{idx:05d}", 0, "01/01/2025", f"BACKGROUND IN {idx}", 0, 850, 1850))
        rows.extend(
            [
                _row("940001", 1, "02/01/2025", "UPI/DR/27014275/SURESH BABU/suresh166@ybl", 850, 0, 150, reference="27014275"),
                _row("940002", 1, "02/01/2025", "IMPS/P2P/577356133501/POO", 0, 850, 1850, reference="577356133501"),
                _row("940003", 1, "03/01/2025", "CHQ NO 396798 CLEARING", 850, 0, 150, reference="396798"),
                _row("940004", 1, "03/01/2025", "CASH DEPOSIT BRANCH COUNTER", 0, 850, 1850, reference="CASHDEP1"),
            ]
        )
        csv_path = _write_case(Path(tmp) / "fake_corrob_common_amount.csv", rows)
        result = _run(csv_path, "fake_corrob_common_amount")
        assert len(result.findings_by_pattern["10_cross_statement_links"]) == 0
        _assert_confidence_consistent(result)


def test_money_trail_ordinary_spending_is_downgraded() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = _write_case(
            Path(tmp) / "ordinary_spending_trail.csv",
            [
                _row("920001", 0, "01/01/2025", "OPENING", 0, 1000, 1000),
                _row("920001", 1, "02/01/2025", "SALARY CREDIT", 0, 5000, 6000),
                _row("920001", 2, "03/01/2025", "ATM WITHDRAWAL", 1000, 0, 5000),
                _row("920001", 3, "04/01/2025", "POS GROCERY", 1200, 0, 3800),
                _row("920001", 4, "05/01/2025", "MAB CHARGES", 100, 0, 3700),
                _row("920001", 5, "06/01/2025", "BILL PAYMENT", 1300, 0, 2400),
                _row("920001", 6, "07/01/2025", "FUEL PAYMENT", 900, 0, 1500),
                _row("920001", 7, "08/01/2025", "ATM WITHDRAWAL", 500, 0, 1000),
            ],
        )
        result = _run(csv_path, "ordinary_spending_trail", ["920001_000001"])
        trails = result.findings_by_pattern["8_money_trail_tracing"]
        assert trails
        assert trails[0].evidence_strength in {"weak", "lead"}
        assert trails[0].confidence_tier != "high"
        assert trails[0].details["allocation_summary"]["chain_specificity"] == "ordinary_spending_like"
        _assert_confidence_consistent(result)


def test_auto_money_trail_decoy_without_prior_findings_is_not_high_confidence() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = _write_case(
            Path(tmp) / "auto_trail_decoy.csv",
            [
                _row("950001", 0, "01/01/2025", "OPENING", 0, 1000, 1000),
                _row("950001", 1, "02/01/2025", "BONUS CREDIT", 0, 50000, 51000),
                _row("950001", 2, "03/01/2025", "ATM WITHDRAWAL", 10000, 0, 41000),
                _row("950001", 3, "04/01/2025", "POS GROCERY", 12000, 0, 29000),
                _row("950001", 4, "05/01/2025", "BILL PAYMENT", 11000, 0, 18000),
                _row("950001", 5, "06/01/2025", "FUEL PAYMENT", 8000, 0, 10000),
            ],
        )
        result = _run(csv_path, "auto_trail_decoy")
        account_trails = [
            finding
            for finding in result.findings_by_pattern["8_money_trail_tracing"]
            if "950001" in finding.accounts
        ]
        assert not account_trails or all(finding.evidence_strength in {"weak", "lead"} for finding in account_trails)
        _assert_confidence_consistent(result)


def test_credit_to_cash_high_ratio_survives_recurring_cash_habit_filter() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        rows = [_row("960001", 0, "01/01/2025", "OPENING", 0, 100000, 100000)]
        for idx, day in enumerate([5, 12, 19], start=1):
            rows.append(_row("960001", idx, f"{day:02d}/01/2025", "ATM WITHDRAWAL ROUTINE", 1000, 0, 99000 - idx * 1000))
        rows.extend(
            [
                _row("960001", 10, "01/02/2025", "UPI CREDIT CUSTOMER", 0, 18500, 115500),
                _row("960001", 11, "02/02/2025", "ATM WITHDRAWAL", 3500, 0, 112000),
                _row("960001", 12, "02/02/2025", "ATM WITHDRAWAL", 3500, 0, 108500),
                _row("960001", 13, "03/02/2025", "ATM WITHDRAWAL", 7800, 0, 100700),
            ]
        )
        csv_path = _write_case(Path(tmp) / "cash_high_ratio.csv", rows)
        result = _run(csv_path, "cash_high_ratio")
        findings = result.findings_by_pattern["9_credit_to_cash_out_chains"]
        assert any(f.details.get("withdrawal_ratio", 0) >= 0.70 for f in findings)
        _assert_confidence_consistent(result)


def test_shared_service_upi_is_suppressed_but_personal_subset_still_fires() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        rows = []
        for idx, account in enumerate(["970001", "970002", "970003", "970004"], start=1):
            rows.append(_row(account, 0, "01/01/2025", "OPENING", 0, 10000, 10000))
            rows.append(
                _row(
                    account,
                    idx,
                    "02/01/2025",
                    "UPI/DR/airtel.recharge@airtel/MOBILE RECHARGE",
                    399,
                    0,
                    9601,
                )
            )
        csv_path = _write_case(Path(tmp) / "shared_service_vpa.csv", rows)
        result = _run(csv_path, "shared_service_vpa")
        assert len(result.findings_by_pattern["16_shared_upi_identifiers"]) == 0

        personal_rows = [
            _row("971001", 0, "01/01/2025", "OPENING", 0, 10000, 10000),
            _row("971002", 0, "01/01/2025", "OPENING", 0, 10000, 10000),
            _row("971003", 0, "01/01/2025", "OPENING", 0, 10000, 10000),
            _row("971001", 1, "02/01/2025", "UPI/DR/ravi61@ybl/TRANSFER", 1000, 0, 9000),
            _row("971002", 1, "03/01/2025", "UPI/DR/ravi61@ybl/TRANSFER", 1200, 0, 8800),
        ]
        csv_path = _write_case(Path(tmp) / "shared_personal_vpa.csv", personal_rows)
        result = _run(csv_path, "shared_personal_vpa")
        findings = result.findings_by_pattern["16_shared_upi_identifiers"]
        assert len(findings) == 1
        assert findings[0].details["upi_identifier"] == "ravi61@ybl"
        _assert_confidence_consistent(result)


def test_recurring_large_first_contact_series_is_suppressed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        rows = [
            _row("980001", 0, "01/01/2025", "OPENING", 0, 10000, 10000),
            _row(
                "980001",
                1,
                "14/01/2025",
                "NEFT:ICICN2501146026106:CARMEN TRINITY VENTURE",
                0,
                59909,
                69909,
                reference="2501146026106",
            ),
            _row(
                "980001",
                2,
                "12/02/2025",
                "NEFT:ICICN2502120464624:CARMEN TRINITY VENTURE",
                0,
                67404,
                137313,
                reference="2502120464624",
            ),
            _row(
                "980001",
                3,
                "15/03/2025",
                "NEFT:ICICN2503150175544:CARMEN TRINITY VENTURE",
                0,
                61000,
                198313,
                reference="2503150175544",
            ),
            _row(
                "980001",
                4,
                "14/04/2025",
                "NEFT:ICICN2504141112223:CARMEN TRINITY VENTURE",
                0,
                62500,
                260813,
                reference="2504141112223",
            ),
        ]
        csv_path = _write_case(Path(tmp) / "recurring_first_contact.csv", rows)
        result = _run(csv_path, "recurring_first_contact")
        assert len(result.findings_by_pattern["19_first_contact_large_transfer"]) == 0
        _assert_confidence_consistent(result)


def test_scoring_requires_fraud_specific_pattern_for_ranking() -> None:
    noisy_only = {
        "16_shared_upi_identifiers": [
            Finding(16, "shared_upi_identifiers", ["990001"], ["t1"], "shared vpa", "")
        ],
        "19_first_contact_large_transfer": [
            Finding(19, "first_contact_large_transfer", ["990001"], ["t2"], "first contact", "")
        ],
    }
    assert score_accounts(noisy_only) == []

    with_fraud_signal = {
        **noisy_only,
        "8_money_trail_tracing": [
            Finding(8, "money_trail_tracing", ["990001"], ["t3"], "money trail", "")
        ],
    }
    ranked = score_accounts(with_fraud_signal)
    assert len(ranked) == 1
    assert ranked[0].account_id == "990001"


def test_duplicate_confirmation_and_false_positive_split() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        rows = [
            _row("930001", 0, "01/01/2025", "NEFT RENT PAYMENT", 1000, 0, 9000, reference="DUPREF"),
            _row("930001", 1, "01/01/2025", "NEFT RENT PAYMENT", 1000, 0, 9000, reference="DUPREF", duplicate_of="930001_000000"),
            _row("930001", 2, "02/01/2025", "UNRELATED CARD PAYMENT", 2222, 0, 6778, duplicate_of="source-row::99"),
        ]
        csv_path = _write_case(Path(tmp) / "duplicates_split.csv", rows)
        result = _run(csv_path, "duplicates_split")
        categories = [
            finding.details.get("reconciliation_category")
            for finding in result.findings_by_pattern["1_duplicate_verification"]
        ]
        assert "confirmed_extraction_duplicate" in categories
        assert "possible_extraction_false_positive" in categories
        _assert_confidence_consistent(result)


def test_priority_round_value_repeated_positive_and_coincidental_negative() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        positive = _write_case(
            tmp_path / "round_positive.csv",
            [
                _row("900009", 0, "01/01/2025", "OPENING", 0, 10000, 10000),
                _row("900009", 1, "02/01/2025", "PAYOUT", 1000, 0, 9000),
                _row("900009", 2, "03/01/2025", "PAYOUT", 1000, 0, 8000),
                _row("900009", 3, "04/01/2025", "PAYOUT", 1000, 0, 7000),
            ],
        )
        result = _run(positive, "round_positive")
        assert len(result.findings_by_pattern["15_round_value_debit_patterns"]) >= 1

        negative = _write_case(
            tmp_path / "round_negative.csv",
            [
                _row("910010", 0, "01/01/2025", "OPENING", 0, 10000, 10000),
                _row("910010", 1, "02/01/2025", "PAYOUT", 1000, 0, 9000),
                _row("910010", 2, "03/01/2025", "PAYOUT", 1337, 0, 7663),
            ],
        )
        result = _run(negative, "round_negative")
        assert len(result.findings_by_pattern["15_round_value_debit_patterns"]) == 0


if __name__ == "__main__":
    test_priority_round_trip_positive_and_noncycle_negative()
    test_priority_money_trail_partial_trace()
    test_common_amount_decoy_does_not_create_amount_only_links()
    test_common_amount_reference_link_still_fires()
    test_fake_reference_and_narration_methods_do_not_correlate_common_amounts()
    test_money_trail_ordinary_spending_is_downgraded()
    test_auto_money_trail_decoy_without_prior_findings_is_not_high_confidence()
    test_credit_to_cash_high_ratio_survives_recurring_cash_habit_filter()
    test_shared_service_upi_is_suppressed_but_personal_subset_still_fires()
    test_recurring_large_first_contact_series_is_suppressed()
    test_scoring_requires_fraud_specific_pattern_for_ranking()
    test_duplicate_confirmation_and_false_positive_split()
    test_priority_round_value_repeated_positive_and_coincidental_negative()
    print("priority edge-case tests passed")
