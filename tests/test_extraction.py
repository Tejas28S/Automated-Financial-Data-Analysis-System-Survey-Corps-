"""
test_extraction.py — Automated tests for the extraction pipeline.

These tests verify that every component of the extraction phase works correctly
using the synthetic dataset in synthetic_dataset_full_mentoring/.

The tests cover:
  - File routing (does the router correctly classify each file type?)
  - Digital PDF text extraction (does pdfplumber get real text from PDFs?)
  - Excel/CSV extraction (does pandas read the structured files correctly?)
  - Standardiser output schema (do we always get the 7 standard columns?)
  - Validator checks (does it correctly flag bad rows and remove duplicates?)
  - Anonymiser privacy protection (does it replace account numbers before sending?)
  - Full pipeline integration (does the entire system work end-to-end?)

Run with: python -m pytest tests/test_extraction.py -v

Team: Survey Corps | CIDECODE Hackathon 2026 | CID Karnataka
"""

import re
import sys
from pathlib import Path
from datetime import datetime
from unittest.mock import patch

import pandas as pd
import pytest

# Add the project root to the Python path so imports work correctly.
# This is necessary when running pytest from the tests/ directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import STANDARD_COLUMNS

# ── Dataset paths ─────────────────────────────────────────────────────────────
# These point to the actual synthetic dataset files used for testing.
# The mentoring dataset is the correct dataset to use (not synthetic_dataset_full).
DATASET_DIR = PROJECT_ROOT / "synthetic_dataset_full_mentoring" / "statements"

SAMPLE_DIGITAL_PDF = DATASET_DIR / "digital_pdf" / "ACC001_SBI_Statement.pdf"
SAMPLE_EXCEL = DATASET_DIR / "excel" / "ACC026_Canara_Statement.xlsx"
SAMPLE_CSV = DATASET_DIR / "csv" / "ACC063_Kotak_Statement.csv"
SAMPLE_SCANNED_PDF = DATASET_DIR / "scanned_pdf" / "ACC051_HDFC_Statement.pdf"

# A fake XLSX, CSV, DOCX, JPG, PNG path for routing tests.
# We only test the routing logic using the file extension — the file doesn't
# need to actually exist for the router test because we test extension detection.
# However, the router checks if the file exists for PDFs, so we use real PDFs there.
FAKE_XLSX = PROJECT_ROOT / "uploads" / "test.xlsx"
FAKE_CSV = PROJECT_ROOT / "uploads" / "test.csv"
FAKE_DOCX = PROJECT_ROOT / "uploads" / "test.docx"
FAKE_JPG = PROJECT_ROOT / "uploads" / "test.jpg"
FAKE_PNG = PROJECT_ROOT / "uploads" / "test.png"


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def create_fake_files(tmp_path):
    """
    Creates temporary fake files for router tests.
    These files only need to exist with the correct extension — they can be empty.
    The router checks file existence before routing.
    """
    # Create the uploads directory if it doesn't exist
    uploads_dir = PROJECT_ROOT / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    # Create empty placeholder files so the router's existence check passes
    for fake_path in [FAKE_XLSX, FAKE_CSV, FAKE_DOCX, FAKE_JPG, FAKE_PNG]:
        fake_path.touch(exist_ok=True)

    yield

    # Clean up the fake files after each test
    for fake_path in [FAKE_XLSX, FAKE_CSV, FAKE_DOCX, FAKE_JPG, FAKE_PNG]:
        if fake_path.exists():
            fake_path.unlink()


# ── TEST GROUP 1: Router ──────────────────────────────────────────────────────

class TestRouter:
    """Tests for extraction/router.py — file type detection and routing."""

    def test_router_routes_xlsx_to_excel_csv(self):
        """
        A .xlsx file should be routed to 'excel_csv' because it is an
        Excel spreadsheet that pandas can read directly.
        """
        from extraction.router import route_file
        result = route_file(str(FAKE_XLSX))
        assert result == "excel_csv", f"Expected 'excel_csv', got '{result}'"

    def test_router_routes_csv_to_excel_csv(self):
        """
        A .csv file should also be routed to 'excel_csv' because pandas
        reads both CSV and Excel with the same extractor.
        """
        from extraction.router import route_file
        result = route_file(str(FAKE_CSV))
        assert result == "excel_csv", f"Expected 'excel_csv', got '{result}'"

    def test_router_routes_docx_to_docx(self):
        """A .docx Microsoft Word file should be routed to 'docx'."""
        from extraction.router import route_file
        result = route_file(str(FAKE_DOCX))
        assert result == "docx", f"Expected 'docx', got '{result}'"

    def test_router_routes_jpg_to_image(self):
        """A .jpg photograph of a bank statement should be routed to 'image'."""
        from extraction.router import route_file
        result = route_file(str(FAKE_JPG))
        assert result == "image", f"Expected 'image', got '{result}'"

    def test_router_routes_png_to_image(self):
        """A .png image should be routed to 'image' for OCR processing."""
        from extraction.router import route_file
        result = route_file(str(FAKE_PNG))
        assert result == "image", f"Expected 'image', got '{result}'"

    def test_router_routes_digital_pdf_correctly(self):
        """
        A computer-generated PDF (with embedded text) should be routed to
        'pdf_digital'. The SBI statement has rich text on every page.
        """
        from extraction.router import route_file

        if not SAMPLE_DIGITAL_PDF.exists():
            pytest.skip(f"Sample digital PDF not found: {SAMPLE_DIGITAL_PDF}")

        result = route_file(str(SAMPLE_DIGITAL_PDF))
        assert result == "pdf_digital", (
            f"Expected 'pdf_digital' for a computer-generated bank statement PDF, "
            f"got '{result}'"
        )

    def test_router_routes_scanned_pdf_correctly(self):
        """
        A scanned PDF (photographed or photocopied, stored as pixel images)
        should be routed to 'pdf_scanned' because it has no embedded text.
        """
        from extraction.router import route_file

        if not SAMPLE_SCANNED_PDF.exists():
            pytest.skip(f"Sample scanned PDF not found: {SAMPLE_SCANNED_PDF}")

        result = route_file(str(SAMPLE_SCANNED_PDF))
        assert result == "pdf_scanned", (
            f"Expected 'pdf_scanned' for an image-only PDF, got '{result}'"
        )

    def test_router_raises_for_unsupported_extension(self, tmp_path):
        """
        An unsupported file type (like .mp3 or .zip) should raise a ValueError.
        """
        from extraction.router import route_file

        unsupported_file = tmp_path / "test_audio.mp3"
        unsupported_file.touch()  # Create the empty file so the existence check passes

        with pytest.raises(ValueError, match="Unsupported file extension"):
            route_file(str(unsupported_file))

    def test_router_raises_for_missing_file(self, tmp_path):
        """
        A path that does not point to any real file should raise a FileNotFoundError.
        """
        from extraction.router import route_file
        missing_path = str(tmp_path / "nonexistent_file.pdf")
        with pytest.raises(FileNotFoundError):
            route_file(missing_path)


# ── TEST GROUP 2: Digital PDF Extractor ───────────────────────────────────────

class TestDigitalPDFExtractor:
    """Tests for extraction/extractor_digital_pdf.py"""

    def test_digital_pdf_extraction_returns_nonempty_string(self):
        """
        The digital PDF extractor should return a non-empty string
        containing the full text of the bank statement.
        """
        from extraction.extractor_digital_pdf import extract_text_from_digital_pdf

        if not SAMPLE_DIGITAL_PDF.exists():
            pytest.skip(f"Sample digital PDF not found: {SAMPLE_DIGITAL_PDF}")

        result = extract_text_from_digital_pdf(str(SAMPLE_DIGITAL_PDF))

        assert isinstance(result, str), f"Expected a string, got {type(result)}"
        assert len(result) > 0, "Extracted text should not be empty"
        assert len(result) > 100, (
            f"Expected more than 100 characters from a multi-page bank statement, "
            f"got {len(result)}"
        )

    def test_digital_pdf_text_contains_date_pattern(self):
        """
        The extracted text must contain at least one date-like pattern
        (DD/MM/YYYY) because every bank statement has transaction dates.
        """
        from extraction.extractor_digital_pdf import extract_text_from_digital_pdf

        if not SAMPLE_DIGITAL_PDF.exists():
            pytest.skip(f"Sample digital PDF not found: {SAMPLE_DIGITAL_PDF}")

        text = extract_text_from_digital_pdf(str(SAMPLE_DIGITAL_PDF))

        # Search for any date in DD/MM/YYYY format
        date_pattern = re.compile(r"\d{2}/\d{2}/\d{4}")
        matches = date_pattern.findall(text)

        assert len(matches) > 0, (
            f"Expected at least one date in DD/MM/YYYY format in the extracted text. "
            f"First 200 chars of text: {text[:200]}"
        )

    def test_digital_pdf_extraction_handles_missing_file_gracefully(self, tmp_path):
        """
        If the file doesn't exist, the extractor should return an empty string
        instead of raising an exception. The pipeline should never crash on
        a single bad file.
        """
        from extraction.extractor_digital_pdf import extract_text_from_digital_pdf

        result = extract_text_from_digital_pdf(str(tmp_path / "nonexistent.pdf"))
        assert result == "", "Expected empty string for missing file"


# ── TEST GROUP 3: Excel/CSV Extractor ────────────────────────────────────────

class TestExcelCSVExtractor:
    """Tests for extraction/extractor_excel_csv.py"""

    def test_excel_extraction_returns_dataframe(self):
        """
        Reading an Excel file should return a pandas DataFrame with data.
        """
        from extraction.extractor_excel_csv import extract_dataframe_from_excel_csv

        if not SAMPLE_EXCEL.exists():
            pytest.skip(f"Sample Excel file not found: {SAMPLE_EXCEL}")

        result = extract_dataframe_from_excel_csv(
            file_path=str(SAMPLE_EXCEL),
            account_id="ACC026",
            bank_name="Canara",
        )

        assert isinstance(result, pd.DataFrame), f"Expected DataFrame, got {type(result)}"
        assert len(result) > 0, "Excel file should have at least one data row"

    def test_csv_extraction_returns_dataframe(self):
        """
        Reading a CSV file should return a pandas DataFrame with data.
        """
        from extraction.extractor_excel_csv import extract_dataframe_from_excel_csv

        if not SAMPLE_CSV.exists():
            pytest.skip(f"Sample CSV file not found: {SAMPLE_CSV}")

        result = extract_dataframe_from_excel_csv(
            file_path=str(SAMPLE_CSV),
            account_id="ACC063",
            bank_name="Kotak",
        )

        assert isinstance(result, pd.DataFrame), f"Expected DataFrame, got {type(result)}"
        assert len(result) > 0, "CSV file should have at least one data row"

    def test_extractor_attaches_account_id_and_bank_name(self):
        """
        The extractor must attach Account_ID and Bank_Name to every row
        so we can always trace which account a transaction belongs to.
        """
        from extraction.extractor_excel_csv import extract_dataframe_from_excel_csv

        if not SAMPLE_CSV.exists():
            pytest.skip(f"Sample CSV file not found: {SAMPLE_CSV}")

        result = extract_dataframe_from_excel_csv(
            file_path=str(SAMPLE_CSV),
            account_id="TEST_ACCOUNT",
            bank_name="TEST_BANK",
        )

        assert "Account_ID" in result.columns
        assert "Bank_Name" in result.columns
        assert (result["Account_ID"] == "TEST_ACCOUNT").all()
        assert (result["Bank_Name"] == "TEST_BANK").all()


# ── TEST GROUP 4: Standardiser ────────────────────────────────────────────────

class TestStandardiser:
    """Tests for extraction/standardiser.py"""

    def test_standardiser_output_has_correct_columns(self):
        """
        The standardiser must ALWAYS produce a DataFrame with exactly the
        7 standard columns: Date, Narration, Debit, Credit, Balance,
        Account_ID, Bank_Name — regardless of the input format.
        """
        from extraction.standardiser import standardise_transactions

        # A minimal sample bank statement text (3 header lines + 3 transactions)
        sample_text = (
            "State Bank of India\n"
            "Account Number: 123456789\n"
            "Date Narration Debit Credit Balance\n"
            "01/08/2022 UPI/CR/123/RAMESH 0.00 5000.00 25000.00\n"
            "02/08/2022 ATM/WDL/456 2000.00 0.00 23000.00\n"
            "03/08/2022 NEFT/789/PRIYA 0.00 10000.00 33000.00\n"
        )
        # Column map that matches the header row: Date=0, Narration=1, Debit=2, Credit=3, Balance=4
        column_map = {"date": 0, "narration": 1, "debit": 2, "credit": 3, "balance": 4}

        result = standardise_transactions(
            raw_text=sample_text,
            column_map=column_map,
            account_id="TEST_ACC",
            bank_name="SBI",
        )

        assert isinstance(result, pd.DataFrame), "Standardiser must return a DataFrame"
        # Verify that all 7 standard columns are present (order may vary)
        for col in STANDARD_COLUMNS:
            assert col in result.columns, (
                f"Expected column '{col}' to be present in standardised DataFrame. "
                f"Got columns: {result.columns.tolist()}"
            )

    def test_standardiser_output_has_correct_column_order(self):
        """
        The standard columns must appear in EXACTLY the right order because
        the analysis engine (fraud detection) accesses them by position.
        """
        from extraction.standardiser import standardise_transactions

        sample_text = (
            "Date Narration Debit Credit Balance\n"
            "01/08/2022 UPI/CR/123/TEST 0.00 1000.00 5000.00\n"
        )
        column_map = {"date": 0, "narration": 1, "debit": 2, "credit": 3, "balance": 4}

        result = standardise_transactions(sample_text, column_map, "ACC001", "SBI")

        assert list(result.columns) == STANDARD_COLUMNS, (
            f"Expected column order {STANDARD_COLUMNS}, got {list(result.columns)}"
        )

    def test_standardiser_date_column_is_datetime(self):
        """
        The Date column must be pandas datetime dtype (not string or object).
        The analysis engine uses date arithmetic for time-based fraud detection.
        """
        from extraction.standardiser import standardise_transactions

        sample_text = (
            "Date Narration Debit Credit Balance\n"
            "01/08/2022 UPI/CR/123/TEST 0.00 1000.00 5000.00\n"
            "02/08/2022 NEFT/456/TEST 500.00 0.00 4500.00\n"
        )
        column_map = {"date": 0, "narration": 1, "debit": 2, "credit": 3, "balance": 4}

        result = standardise_transactions(sample_text, column_map, "ACC001", "SBI")

        if not result.empty:
            assert pd.api.types.is_datetime64_any_dtype(result["Date"]), (
                f"Date column should be datetime dtype, got {result['Date'].dtype}"
            )

    def test_standardiser_numeric_columns_are_float(self):
        """
        Debit, Credit, and Balance columns must be float64.
        String values like "1,500" or "₹5,000" must be converted to numbers.
        """
        from extraction.standardiser import standardise_transactions

        sample_text = (
            "Date Narration Debit Credit Balance\n"
            "01/08/2022 UPI/CR/123/TEST 0.00 1000.00 5000.00\n"
            "02/08/2022 NEFT/456/TEST 500.00 0.00 4500.00\n"
        )
        column_map = {"date": 0, "narration": 1, "debit": 2, "credit": 3, "balance": 4}

        result = standardise_transactions(sample_text, column_map, "ACC001", "SBI")

        if not result.empty:
            for col in ["Debit", "Credit", "Balance"]:
                assert pd.api.types.is_float_dtype(result[col]), (
                    f"Column '{col}' should be float dtype, got {result[col].dtype}"
                )

    def test_standardiser_handles_real_excel_file(self):
        """
        Integration test: The standardiser should correctly process a real
        Excel file from the synthetic dataset through standardise_dataframe_direct.
        """
        from extraction.extractor_excel_csv import extract_dataframe_from_excel_csv
        from extraction.standardiser import standardise_dataframe_direct

        if not SAMPLE_EXCEL.exists():
            pytest.skip(f"Sample Excel file not found: {SAMPLE_EXCEL}")

        raw_df = extract_dataframe_from_excel_csv(str(SAMPLE_EXCEL), "ACC026", "Canara")
        # Use a simple column map (heuristic guessing will kick in)
        column_map = {"date": "Date", "narration": "Particulars", "debit": "Debit",
                      "credit": "Credit", "balance": "Balance"}

        result = standardise_dataframe_direct(raw_df, column_map, "ACC026", "Canara")

        assert isinstance(result, pd.DataFrame)
        assert len(result) > 0, "Should extract at least one transaction"
        for col in STANDARD_COLUMNS:
            assert col in result.columns, f"Missing standard column: {col}"


# ── TEST GROUP 5: Validator ───────────────────────────────────────────────────

class TestValidator:
    """Tests for extraction/validator.py"""

    def _make_valid_dataframe(self) -> pd.DataFrame:
        """
        Helper to create a small valid transaction DataFrame for testing.
        Three transactions with correct balance arithmetic.
        """
        return pd.DataFrame({
            "Date": pd.to_datetime(["2022-08-01", "2022-08-02", "2022-08-03"]),
            "Narration": ["UPI/CR/100/TEST_CREDIT", "ATM/WDL/200/WITHDRAW", "NEFT/300/TEST"],
            "Debit": [0.0, 2000.0, 0.0],
            "Credit": [5000.0, 0.0, 10000.0],
            "Balance": [25000.0, 23000.0, 33000.0],
            "Account_ID": ["ACC001", "ACC001", "ACC001"],
            "Bank_Name": ["SBI", "SBI", "SBI"],
        })

    def test_validator_flags_balance_mismatch(self):
        """
        If the balance arithmetic fails (previous + credit - debit ≠ current),
        the row should be flagged with flag_reason = "balance_mismatch".
        """
        from extraction.validator import validate_and_clean

        df = self._make_valid_dataframe()
        # Deliberately make the balance wrong on row 2 (index 1)
        # Correct should be 25000 - 2000 = 23000, but we set it to 99000
        df.loc[1, "Balance"] = 99000.0  # Wrong balance

        clean_df, flagged_df = validate_and_clean(df)

        assert len(flagged_df) >= 1, "Should have at least 1 flagged row"
        assert "balance_mismatch" in flagged_df["flag_reason"].values, (
            f"Expected 'balance_mismatch' in flagged rows. Got: {flagged_df['flag_reason'].tolist()}"
        )

    def test_validator_tags_duplicates_without_deleting(self):
        """
        Problem 6: exact duplicate transactions must be KEPT, not deleted. Both
        rows survive; the later copy is tagged with duplicate_of pointing at the
        original's txn_id. This preserves a full audit trail.
        """
        from extraction.validator import mark_duplicates

        # Two identical transactions for the same account.
        df = pd.DataFrame([
            {"Date": pd.Timestamp("2022-08-01"), "Narration": "UPI/DR/TEST",
             "Debit": 100.0, "Credit": 0.0, "Balance": 900.0,
             "Account_ID": "ACC001", "Bank_Name": "SBI"},
            {"Date": pd.Timestamp("2022-08-01"), "Narration": "UPI/DR/TEST",
             "Debit": 100.0, "Credit": 0.0, "Balance": 900.0,
             "Account_ID": "ACC001", "Bank_Name": "SBI"},
        ])

        result = mark_duplicates(df)

        # Nothing is dropped: both copies survive.
        assert len(result) == 2, "Both copies of a duplicate must be kept (none deleted)"
        assert "txn_id" in result.columns and "duplicate_of" in result.columns

        # Exactly one copy is tagged, pointing at the original's txn_id.
        tagged = result["duplicate_of"].notna().sum()
        assert tagged == 1, f"Expected exactly 1 row tagged duplicate_of, got {tagged}"
        original_id = result.iloc[0]["txn_id"]
        dup_target = result.loc[result["duplicate_of"].notna(), "duplicate_of"].iloc[0]
        assert dup_target == original_id, "duplicate_of must point at the first occurrence's txn_id"

    def test_vault_redacts_and_restores_identifiers(self):
        """
        Problem 3: the identifier vault swaps real account number / holder / IFSC
        for ACC_TEMP / HOLDER_TEMP / IFSC_TEMP before an LLM call, and restores the
        real values afterwards. The 'LLM' here is simulated.
        """
        from extraction.identifier_vault import IdentifierVault

        vault = IdentifierVault({
            "account_number": "00000051399615291",
            "account_holder": "Ravi Kumar Sharma",
            "ifsc_code": "SBIN0393634",
        })
        text = "Holder Ravi Kumar Sharma A/C 00000051399615291 IFSC SBIN0393634"
        safe = vault.redact(text)

        # The model must see ONLY placeholders — no real identifiers leak.
        assert "00000051399615291" not in safe
        assert "Ravi Kumar Sharma" not in safe
        assert "SBIN0393634" not in safe
        assert "ACC_TEMP" in safe and "HOLDER_TEMP" in safe and "IFSC_TEMP" in safe

        # After the call, restore brings the real values back (round-trip).
        restored = vault.restore(safe)
        assert restored == text

    def test_account_extractor_reads_identity_from_text(self):
        """
        Problem 2: account identity must come from the statement CONTENT, not the
        filename. Verify the regex reader pulls holder / number / IFSC / bank.
        """
        from extraction.account_extractor import extract_account_details_from_text

        text = (
            "Account Statement\nState Bank of India\n"
            "Account Holder : Ravi Kumar Sharma   IFSC Code: SBIN0393634\n"
            "Account Number : 00000051399615291   Period: 01/08/2022 to 30/06/2024\n"
            "Branch : Pune Camp\n"
        )
        d = extract_account_details_from_text(text)
        assert d["account_holder"] == "Ravi Kumar Sharma"
        assert d["account_number"] == "00000051399615291"
        assert d["ifsc_code"] == "SBIN0393634"
        assert d["bank_name"] == "State Bank of India"
        assert d["branch"] == "Pune Camp"

    def test_validator_flags_invalid_date(self):
        """
        Rows with NaT (Not a Time) or None in the Date column should be
        flagged with flag_reason = "invalid_date".
        """
        from extraction.validator import validate_and_clean

        df = self._make_valid_dataframe()
        # Break the date on row 0 by setting it to NaT
        df.loc[0, "Date"] = pd.NaT

        clean_df, flagged_df = validate_and_clean(df)

        assert len(flagged_df) >= 1, "NaT date row should be flagged"
        assert "invalid_date" in flagged_df["flag_reason"].values, (
            "Expected 'invalid_date' in flagged rows"
        )

    def test_validator_flags_both_debit_and_credit_filled(self):
        """
        A row with both Debit > 0 and Credit > 0 indicates a column alignment
        error and should be flagged with "both_debit_credit_filled".
        """
        from extraction.validator import validate_and_clean

        df = self._make_valid_dataframe()
        # Set both debit and credit to non-zero on row 0
        df.loc[0, "Debit"] = 500.0
        df.loc[0, "Credit"] = 5000.0  # Both non-zero — this is invalid

        clean_df, flagged_df = validate_and_clean(df)

        assert len(flagged_df) >= 1
        assert "both_debit_credit_filled" in flagged_df["flag_reason"].values, (
            "Expected 'both_debit_credit_filled' flag for row with both debit and credit"
        )

    def test_validator_returns_is_reversed_column(self):
        """
        The clean DataFrame returned by validate_and_clean must have an
        'is_reversed' boolean column marking reversed/failed transactions.
        """
        from extraction.validator import validate_and_clean

        df = self._make_valid_dataframe()
        clean_df, _ = validate_and_clean(df)

        assert "is_reversed" in clean_df.columns, (
            "Clean DataFrame must have an 'is_reversed' column"
        )
        assert clean_df["is_reversed"].dtype == bool, (
            f"is_reversed should be boolean dtype, got {clean_df['is_reversed'].dtype}"
        )

    def test_validator_marks_reversal_keyword_transactions(self):
        """
        Transactions with keywords like 'REVERSAL' in the narration
        should have is_reversed = True.
        """
        from extraction.validator import validate_and_clean

        df = self._make_valid_dataframe()
        # Make row 1 a reversal (debit with reversal keyword)
        df.loc[1, "Narration"] = "UPI/REVERSAL/ATM/WDL/200/WITHDRAW"

        clean_df, _ = validate_and_clean(df)

        if not clean_df.empty:
            reversal_rows = clean_df[clean_df["is_reversed"] == True]
            assert len(reversal_rows) >= 1, (
                "At least one row should be marked as reversed due to 'REVERSAL' keyword"
            )

    def test_validator_passes_clean_dataframe_through(self):
        """
        A perfectly valid DataFrame with correct arithmetic should pass through
        the validator with all rows in clean_df and none in flagged_df.
        """
        from extraction.validator import validate_and_clean

        df = self._make_valid_dataframe()
        clean_df, flagged_df = validate_and_clean(df)

        assert len(clean_df) == len(df), (
            f"All rows should pass validation for a clean DataFrame. "
            f"Expected {len(df)} clean rows, got {len(clean_df)}"
        )
        assert len(flagged_df) == 0, (
            f"Expected 0 flagged rows for a clean DataFrame, got {len(flagged_df)}"
        )


# ── TEST GROUP 6: Anonymiser ──────────────────────────────────────────────────

class TestAnonymiser:
    """Tests for extraction/anonymiser.py"""

    def test_anonymiser_replaces_account_numbers(self):
        """
        A 12-digit account number in the text must be replaced with a
        placeholder like ACCT_1. The original number must NOT appear in
        the anonymised text — it should only be in the mapping dict.
        """
        from extraction.anonymiser import anonymise_text

        original_account = "123456789012"
        text = f"Transfer to account {original_account} completed"

        anonymised, mapping = anonymise_text(text)

        assert original_account not in anonymised, (
            "Account number must not appear in the anonymised text. "
            "This ensures real account numbers never reach Groq."
        )
        assert original_account in mapping.values(), (
            "The original account number must be saved in the mapping dict"
        )

    def test_anonymiser_replaces_ifsc_codes(self):
        """
        IFSC codes (like SBIN0393634) must be replaced with placeholders.
        """
        from extraction.anonymiser import anonymise_text

        text = "Transfer via SBIN0393634 from HDFC0001234"
        anonymised, mapping = anonymise_text(text)

        # Neither IFSC should appear in the anonymised text
        assert "SBIN0393634" not in anonymised
        assert "HDFC0001234" not in anonymised

        # The mapping should contain both original values
        assert "SBIN0393634" in mapping.values()
        assert "HDFC0001234" in mapping.values()

    def test_anonymiser_replaces_upi_ids(self):
        """
        UPI IDs (like ramesh@paytm or suresh576@okhdfc) must be replaced
        before the text is sent to any AI API.
        """
        from extraction.anonymiser import anonymise_text

        text = "UPI/CR/12345/RAMESH/ramesh@paytm balance 5000"
        anonymised, mapping = anonymise_text(text)

        assert "ramesh@paytm" not in anonymised
        assert "ramesh@paytm" in mapping.values()

    def test_anonymiser_replaces_phone_numbers(self):
        """
        10-digit mobile numbers starting with 6-9 (Indian mobile format)
        must be replaced with placeholders.
        """
        from extraction.anonymiser import anonymise_text

        text = "IMPS/P2P/8782867089 transfer received"
        anonymised, mapping = anonymise_text(text)

        assert "8782867089" not in anonymised
        assert "8782867089" in mapping.values()

    def test_anonymiser_returns_mapping_dict(self):
        """
        The anonymiser must return a tuple of (anonymised_text, mapping_dict).
        The mapping dict stores the placeholder → original value correspondence.
        """
        from extraction.anonymiser import anonymise_text

        text = "Account 9876543210 credited Rs 5000"
        result = anonymise_text(text)

        assert isinstance(result, tuple), "anonymise_text must return a tuple"
        assert len(result) == 2, "Tuple must have exactly 2 elements"
        anonymised, mapping = result
        assert isinstance(anonymised, str), "First element must be a string"
        assert isinstance(mapping, dict), "Second element must be a dict"

    def test_anonymiser_preserves_amounts_and_dates(self):
        """
        The anonymiser must NOT replace date values or monetary amounts.
        Only PII (account numbers, names, IFSC codes) should be replaced.
        """
        from extraction.anonymiser import anonymise_text

        text = "01/08/2022 CASH DEPOSIT 5000.00 25000.00"
        anonymised, mapping = anonymise_text(text)

        # Dates and amounts should pass through unchanged
        assert "01/08/2022" in anonymised, "Date should not be anonymised"
        assert "5000.00" in anonymised, "Monetary amounts should not be anonymised"


class TestValidatorReferee:
    """
    Tests for grade_parse() — the balance-reconciliation referee that decides, with
    no answer key and no knowledge of the bank, whether a parse is trustworthy or
    must escalate to the LLM. This is the heart of the Validation-Arbitrated Tiered
    Hybrid, so it is tested across the tricky real-world cases.
    """

    @staticmethod
    def _mk(rows):
        """rows: list of [debit, credit, balance] → a graded-ready DataFrame."""
        ts = pd.Timestamp("2023-04-01")
        return pd.DataFrame(
            [[ts, d, c, b] for d, c, b in rows],
            columns=["Date", "Debit", "Credit", "Balance"],
        )

    def test_correct_chain_passes_oldest_first(self):
        from extraction.validator import grade_parse
        df = self._mk([[0, 5000, 25300], [12000, 0, 13300], [0, 40000, 53300]])
        r = grade_parse(df, expected_rows=3)
        assert r["verdict"] == "PASS"
        assert r["reconciliation_rate"] == 1.0
        assert r["ordering"] == "oldest_first"

    def test_wrong_column_guess_fails(self):
        # Balance column actually holds cheque numbers → chain cannot reconcile.
        from extraction.validator import grade_parse
        df = self._mk([[0, 5000, 4471], [12000, 0, 8832], [0, 40000, 1201]])
        r = grade_parse(df, expected_rows=3)
        assert r["verdict"] == "FAIL"
        assert r["reconciliation_rate"] < 0.5
        assert len(r["failing_row_indices"]) > 0

    def test_newest_first_is_detected(self):
        # Latest transaction printed first; chain runs bottom-to-top.
        from extraction.validator import grade_parse
        df = self._mk([[0, 40000, 53300], [12000, 0, 13300], [0, 5000, 25300]])
        r = grade_parse(df, expected_rows=3)
        assert r["verdict"] == "PASS"
        assert r["ordering"] == "newest_first"

    def test_missing_opening_balance_keeps_first_row(self):
        # No prior balance to anchor row 1; it must NOT be counted as failing.
        from extraction.validator import grade_parse
        df = self._mk([[0, 0, 100000], [12000, 0, 88000], [0, 40000, 128000]])
        r = grade_parse(df, expected_rows=3)
        assert r["verdict"] == "PASS"
        assert 0 not in r["failing_row_indices"]

    def test_no_balance_column_uses_proxy(self):
        # No running balance at all (all zero) — falls back to exclusivity/date.
        from extraction.validator import grade_parse
        df = self._mk([[0, 5000, 0], [12000, 0, 0], [0, 40000, 0]])
        r = grade_parse(df, expected_rows=3)
        assert r["has_balance_column"] is False
        assert r["verdict"] == "PASS"  # clean exclusivity + valid dates

    def test_no_balance_with_misaligned_row_fails(self):
        # A row with BOTH debit and credit filled is a column-misalignment failure.
        from extraction.validator import grade_parse
        df = self._mk([[0, 5000, 0], [12000, 3000, 0], [0, 40000, 0]])
        r = grade_parse(df, expected_rows=3)
        assert r["verdict"] == "FAIL"
        assert 1 in r["failing_row_indices"]

    def test_under_extraction_fails_completeness(self):
        # Only 2 rows parsed but ~10 transaction-like lines existed → escalate.
        from extraction.validator import grade_parse
        df = self._mk([[0, 5000, 25300], [12000, 0, 13300]])
        r = grade_parse(df, expected_rows=10)
        assert r["completeness_ratio"] < 0.9
        assert r["verdict"] == "FAIL"


class TestNoOverfitting:
    """
    The Anti-Overfitting Law as an automated guard: NO extraction code path may
    branch on a specific bank's identity (e.g. `if bank == "HDFC"`). Bank behaviour
    must only ever be parameterised data/schema. Reference DATA keyed by the IFSC
    standard (BANK_KEYWORDS / IFSC_PREFIX_TO_BANK dict literals) is allowed — those
    are data, not control flow — so this guard inspects only comparison expressions.
    """

    BANK_TOKENS = {
        "hdfc", "icici", "axis", "kotak", "canara", "idbi", "indusind",
        "sbi", "pnb",
    }

    def test_no_bank_name_in_control_flow(self):
        import ast

        extraction_dir = PROJECT_ROOT / "extraction"
        offenders = []

        for py_file in sorted(extraction_dir.glob("*.py")):
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
            # Inspect every comparison expression (covers ==, !=, `in`, etc.).
            # Dict literals (the allowed reference tables) are NOT Compare nodes,
            # so they are correctly ignored.
            for node in ast.walk(tree):
                if not isinstance(node, ast.Compare):
                    continue
                consts = [node.left] + list(node.comparators)
                for c in consts:
                    if isinstance(c, ast.Constant) and isinstance(c.value, str):
                        low = c.value.lower()
                        for bank in self.BANK_TOKENS:
                            if re.search(rf"\b{re.escape(bank)}\b", low):
                                offenders.append(
                                    f"{py_file.name}:{getattr(node, 'lineno', '?')} "
                                    f"compares against bank literal '{c.value}'"
                                )

        assert not offenders, (
            "Bank-specific branching found (violates the Anti-Overfitting Law). "
            "Express bank behaviour as parameterised schema/data, not control flow:\n  - "
            + "\n  - ".join(offenders)
        )
