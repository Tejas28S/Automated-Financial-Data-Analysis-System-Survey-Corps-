"""
validator.py — Data quality validation and cleaning for extracted bank transactions.

After the standardiser produces a unified DataFrame, the validator runs three
quality checks on every row:

  CHECK 1 — Date Validity:
      Every transaction must have a valid, parseable date. If the date column
      contains a number like "90000" or text like "TOTAL", the row was misread
      and must be flagged. CID investigators cannot use a transaction without a date.

  CHECK 2 — Balance Arithmetic:
      In a valid bank statement, each row's balance must equal the previous row's
      balance plus any credit minus any debit. If this arithmetic doesn't hold
      (beyond a small tolerance for rounding), the row is likely a misread or
      a fraudulent alteration to the statement.

  CHECK 3 — Debit/Credit Exclusivity:
      In a normal transaction, money either goes IN (credit) or goes OUT (debit).
      It cannot do both simultaneously. If both Debit and Credit are non-zero for
      the same row, this indicates a column alignment error during extraction.

ADDITIONAL CLEANING:
  - Exact duplicate transactions (same Date + Narration + Amount) are removed.
    Duplicates can appear when a multi-page PDF has overlapping headers.
  - Reversed/failed transactions are detected and marked with the is_reversed flag.
    These are kept in the dataset but the fraud analysis engine uses is_reversed
    to exclude them from cumulative calculations.

Team: Survey Corps | CIDECODE Hackathon 2026 | CID Karnataka
"""

import logging
from typing import Tuple

import pandas as pd

from config.settings import BALANCE_TOLERANCE

# Set up a logger for this module.
logger = logging.getLogger(__name__)

# Keywords in narration that suggest a transaction was reversed or failed
REVERSAL_KEYWORDS = ["reversal", "failed", "return", "reversed", "failure", "bounce", "dishonour"]


def validate_and_clean(df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Runs three validation checks on every row of the standardised DataFrame.

    Check 1 — Date Validity
        The Date field must be a valid parseable date.
        A value like 90000 in the date column indicates a misread row.
        Failed rows get flag_reason = "invalid_date"

    Check 2 — Balance Arithmetic
        For consecutive rows in the same account:
        previous_balance + credit - debit should equal current_balance
        within a tolerance of ±1.0 rupee (to handle rounding).
        Failed rows get flag_reason = "balance_mismatch"

    Check 3 — Debit/Credit Exclusivity
        In a valid transaction, exactly one of Debit or Credit
        should be non-zero. Both being non-zero indicates a
        column alignment error during extraction.
        Failed rows get flag_reason = "both_debit_credit_filled"

    Additional cleaning performed on passing rows:
        - Remove exact duplicate transactions (same Date + Narration + Amount)
        - Detect failed/reversed transactions:
          A debit immediately followed by an equal credit with
          "reversal" or "failed" or "return" in narration → flagged
          as "reversed_transaction" (kept in dataset but marked)

    Parameters:
        df (pd.DataFrame): Standardised DataFrame from standardiser.py
                           Must have columns: Date, Narration, Debit, Credit,
                           Balance, Account_ID, Bank_Name.

    Returns:
        tuple:
            - pd.DataFrame: Clean rows that passed all checks.
                            Has an extra boolean column "is_reversed" to mark
                            transactions that were reversed or failed.
                            Ready for the analysis engine.
            - pd.DataFrame: Flagged rows with an extra string column
                            "flag_reason" explaining why each row failed.
                            Shown to the investigator as a warning.
    """
    if df is None or df.empty:
        logger.warning(
            "validator.validate_and_clean: "
            "Received empty DataFrame. Returning two empty DataFrames."
        )
        empty = df.copy() if df is not None else pd.DataFrame()
        return empty, empty

    logger.info(
        "validator.validate_and_clean: "
        "Starting validation on %d rows.",
        len(df),
    )

    # Work on a fresh copy to avoid modifying the original DataFrame
    working_df = df.copy()

    # Initialise the "flag_reason" column for tracking why rows are flagged
    working_df["flag_reason"] = None  # None means "not flagged yet"

    # ── Check 1: Date Validity ────────────────────────────────────────────────
    working_df = _check_date_validity(working_df)

    # ── Check 2: Balance Arithmetic ───────────────────────────────────────────
    # Only run this check on rows that passed Check 1 (have valid dates).
    # Running balance arithmetic on rows with invalid dates would give meaningless results.
    working_df = _check_balance_arithmetic(working_df)

    # ── Check 3: Debit/Credit Exclusivity ─────────────────────────────────────
    working_df = _check_debit_credit_exclusivity(working_df)

    # ── Split into clean and flagged ──────────────────────────────────────────
    flagged_mask = working_df["flag_reason"].notna()
    flagged_df = working_df[flagged_mask].copy()
    clean_df = working_df[~flagged_mask].copy()

    # Remove the flag_reason column from the clean DataFrame (it's always None there)
    clean_df = clean_df.drop(columns=["flag_reason"])

    logger.info(
        "validator.validate_and_clean: "
        "After validation: %d clean, %d flagged.",
        len(clean_df),
        len(flagged_df),
    )

    # ── Mark (do NOT delete) exact duplicate transactions ─────────────────────
    # Problem 6: nothing is ever dropped. Duplicates are kept and tagged with
    # duplicate_of so the investigator has a full audit trail.
    clean_df = mark_duplicates(clean_df)

    # ── Detect and mark reversed/failed transactions ──────────────────────────
    clean_df = _mark_reversals(clean_df)

    logger.info(
        "validator.validate_and_clean: "
        "Final result: %d clean rows, %d flagged rows.",
        len(clean_df),
        len(flagged_df),
    )

    return clean_df.reset_index(drop=True), flagged_df.reset_index(drop=True)


def _check_date_validity(df: pd.DataFrame) -> pd.DataFrame:
    """
    Flags rows where the Date column does not contain a valid date.

    Valid dates are pandas Timestamp objects (not NaT, not None, not numbers
    that happen to be in a date column due to OCR misreading).

    The check identifies:
      - NaT (Not a Time) — parsing failed during standardisation
      - None — no date was found
      - Rows where the date is suspiciously far in the future or past
        (before 1990 or after 2030 are likely extraction errors)

    Parameters:
        df (pd.DataFrame): DataFrame with a "Date" column and "flag_reason" column.

    Returns:
        pd.DataFrame: Same DataFrame with flag_reason set for invalid date rows.
    """
    # A valid date in this context is a pandas Timestamp (not NaT or None)
    def is_invalid_date(date_value) -> bool:
        """Returns True if the date value is not a valid date."""
        if pd.isna(date_value):
            return True
        if not isinstance(date_value, pd.Timestamp):
            return True
        # Plausibility check: Indian bank statements from investigations
        # should fall between 2000 and 2035
        if date_value.year < 2000 or date_value.year > 2035:
            return True
        return False

    # Apply the check to every row
    invalid_date_mask = df["Date"].apply(is_invalid_date)

    # Flag the invalid rows (only if they haven't already been flagged)
    df.loc[invalid_date_mask & df["flag_reason"].isna(), "flag_reason"] = "invalid_date"

    flagged_count = invalid_date_mask.sum()
    if flagged_count > 0:
        logger.info(
            "validator._check_date_validity: "
            "Flagged %d rows with invalid dates.",
            flagged_count,
        )

    return df


def _check_balance_arithmetic(df: pd.DataFrame) -> pd.DataFrame:
    """
    Flags rows where the running balance does not match the expected arithmetic.

    For any consecutive pair of rows in the same account:
        expected_balance = previous_balance + credit - debit
    This should equal current_balance within ±BALANCE_TOLERANCE (1.0 rupee).

    If the arithmetic fails, it usually means:
      1. An OCR reading error that misread an amount or balance
      2. A missing row (a transaction that was not extracted from the document)
      3. A fraudulent alteration of the bank statement balance

    This check is run PER ACCOUNT because different accounts may be
    interleaved in the unified DataFrame.

    PROBLEM 5 — DEFERRED TO THE ANALYSIS PHASE (by design):
        Failed / rejected / reversed / pending transactions must NOT count toward
        balance-mismatch decisions — only completed, successful transactions should
        drive the running-balance check. The statements in this extraction phase do
        not carry a reliable per-row status column, so that exclusion is owned by
        the analysis phase (which has the txn_status / reversal data). Extraction
        keeps surfacing balance mismatches as "flagged for manual review"; the
        analysis phase is responsible for not treating a known failed/reversed
        transaction as a real mismatch.

    Parameters:
        df (pd.DataFrame): DataFrame with flag_reason column.
                           Rows already flagged (from Check 1) are skipped.

    Returns:
        pd.DataFrame: Same DataFrame with flag_reason set for balance mismatch rows.
    """
    # Only perform arithmetic on rows with valid dates (those not already flagged)
    unflagged_mask = df["flag_reason"].isna()

    for account_id in df["Account_ID"].unique():
        # Process each account's transactions independently
        account_mask = (df["Account_ID"] == account_id) & unflagged_mask
        account_indices = df.index[account_mask].tolist()

        if len(account_indices) < 2:
            # Need at least 2 rows to check balance arithmetic
            continue

        for i in range(1, len(account_indices)):
            prev_idx = account_indices[i - 1]
            curr_idx = account_indices[i]

            # Skip this check if either row has already been flagged
            if df.loc[prev_idx, "flag_reason"] is not None:
                continue
            if df.loc[curr_idx, "flag_reason"] is not None:
                continue

            prev_balance = df.loc[prev_idx, "Balance"]
            curr_balance = df.loc[curr_idx, "Balance"]
            debit = df.loc[curr_idx, "Debit"]
            credit = df.loc[curr_idx, "Credit"]

            # Skip if any value is NaN (can't do arithmetic with missing values)
            if any(pd.isna(v) for v in [prev_balance, curr_balance, debit, credit]):
                continue

            # Calculate the expected balance after this transaction
            expected_balance = prev_balance + credit - debit

            # Check if the actual balance is within the allowed tolerance
            if abs(expected_balance - curr_balance) > BALANCE_TOLERANCE:
                df.loc[curr_idx, "flag_reason"] = "balance_mismatch"
                logger.debug(
                    "validator._check_balance_arithmetic: "
                    "Balance mismatch at row %d for account '%s': "
                    "expected %.2f, got %.2f (debit=%.2f, credit=%.2f)",
                    curr_idx,
                    account_id,
                    expected_balance,
                    curr_balance,
                    debit,
                    credit,
                )

    balance_mismatch_count = (df["flag_reason"] == "balance_mismatch").sum()
    if balance_mismatch_count > 0:
        logger.info(
            "validator._check_balance_arithmetic: "
            "Flagged %d rows with balance arithmetic mismatches.",
            balance_mismatch_count,
        )

    return df


def _check_debit_credit_exclusivity(df: pd.DataFrame) -> pd.DataFrame:
    """
    Flags rows where both Debit AND Credit are non-zero.

    In a valid bank transaction, money either flows IN (credit) or OUT (debit).
    A row with both non-zero means either:
      1. A column alignment error during text extraction (two numbers landed
         in the wrong columns)
      2. A data entry error in the original document

    This check only applies to unflagged rows (rows that passed Checks 1 and 2).

    Parameters:
        df (pd.DataFrame): DataFrame with flag_reason column.

    Returns:
        pd.DataFrame: Same DataFrame with flag_reason set for rows with
                      both debit and credit filled.
    """
    # A transaction has "both filled" if BOTH Debit > 0 AND Credit > 0
    # A small threshold of 0.01 handles floating-point representation issues
    both_filled_mask = (
        (df["Debit"] > 0.01) &
        (df["Credit"] > 0.01) &
        df["flag_reason"].isna()  # Only check unflagged rows
    )

    df.loc[both_filled_mask, "flag_reason"] = "both_debit_credit_filled"

    both_filled_count = both_filled_mask.sum()
    if both_filled_count > 0:
        logger.info(
            "validator._check_debit_credit_exclusivity: "
            "Flagged %d rows where both Debit and Credit are non-zero.",
            both_filled_count,
        )

    return df


def mark_duplicates(df: pd.DataFrame) -> pd.DataFrame:
    """
    Tags exact duplicate transactions WITHOUT deleting any of them (Problem 6).

    A duplicate is a row where Date, Narration, Debit, Credit, and Account_ID are
    all identical to an earlier row. This can happen when a multi-page PDF repeats
    a header row, a file is uploaded twice, or two statements overlap in period.

    Instead of dropping them (which loses evidence), we KEEP every row and add a
    `duplicate_of` column: the first occurrence has duplicate_of = None, and each
    later copy gets the `txn_id` of that first occurrence. This gives a complete
    audit trail — nothing is ever silently removed.

    A stable `txn_id` is assigned to every row so duplicate_of can point at the
    original. Balance is intentionally excluded from the match (the running
    balance is identical on the same row even when captured twice).

    Parameters:
        df (pd.DataFrame): Clean DataFrame after validation checks.

    Returns:
        pd.DataFrame: same rows, plus `txn_id` and `duplicate_of` columns.
    """
    df = df.reset_index(drop=True).copy()
    if df.empty:
        df["txn_id"] = []
        df["duplicate_of"] = []
        return df

    # Give every row a stable id (account + position) so we can reference it.
    df["txn_id"] = [
        f"{acc}_{i:06d}" for i, acc in enumerate(df["Account_ID"].tolist())
    ]
    df["duplicate_of"] = None

    key_cols = ["Date", "Narration", "Debit", "Credit", "Account_ID"]
    first_seen = {}  # fingerprint -> txn_id of the first row with that fingerprint
    dup_count = 0
    for idx in df.index:
        fingerprint = tuple(df.loc[idx, c] for c in key_cols)
        if fingerprint in first_seen:
            # This is a later copy — keep it, but point it at the original.
            df.at[idx, "duplicate_of"] = first_seen[fingerprint]
            dup_count += 1
        else:
            first_seen[fingerprint] = df.at[idx, "txn_id"]

    if dup_count > 0:
        logger.info(
            "validator.mark_duplicates: tagged %d duplicate row(s) with duplicate_of "
            "(kept all rows — none deleted).",
            dup_count,
        )
    return df


def _mark_reversals(df: pd.DataFrame) -> pd.DataFrame:
    """
    Identifies and marks reversed or failed transactions with an is_reversed flag.

    A reversed transaction is one where:
      1. A debit transaction was reversed — money was debited and then returned
      2. A payment failed and was credited back to the account

    Detection criteria:
      - The narration contains keywords like "reversal", "failed", "return",
        "reversed", "failure", "bounce", or "dishonour"
      - OR a debit row is immediately followed by a credit row for the same
        amount with one of the above keywords in the narration

    Reversed transactions are KEPT in the dataset (they happened and are
    part of the account history) but are marked with is_reversed=True.
    The fraud analysis engine uses this flag to exclude reversed transactions
    from cumulative calculations (e.g., total money sent to a suspect).

    Parameters:
        df (pd.DataFrame): Clean DataFrame after duplicate removal.

    Returns:
        pd.DataFrame: Same DataFrame with a boolean "is_reversed" column added.
    """
    # Initialise all transactions as NOT reversed
    df["is_reversed"] = False

    if df.empty:
        return df

    # ── Mark reversals by keyword detection ──────────────────────────────────
    narration_lower = df["Narration"].str.lower()
    keyword_reversal_mask = narration_lower.apply(
        lambda narration: any(keyword in narration for keyword in REVERSAL_KEYWORDS)
    )
    df.loc[keyword_reversal_mask, "is_reversed"] = True

    # ── Mark reversals by debit-followed-by-equal-credit pattern ─────────────
    # For each debit row, check if the next row is a credit for the same amount
    # with a reversal keyword. This catches cases where the narration says
    # "REVERSAL" on the credit side but not the debit side.
    for i in range(len(df) - 1):
        curr_row = df.iloc[i]
        next_row = df.iloc[i + 1]

        # Check: current row is a debit, next row is a credit for same amount
        if (
            curr_row["Debit"] > 0 and
            next_row["Credit"] > 0 and
            abs(curr_row["Debit"] - next_row["Credit"]) < BALANCE_TOLERANCE and
            curr_row["Account_ID"] == next_row["Account_ID"]
        ):
            # Check if the next row's narration contains a reversal keyword
            next_narration = str(next_row["Narration"]).lower()
            if any(keyword in next_narration for keyword in REVERSAL_KEYWORDS):
                df.iloc[i, df.columns.get_loc("is_reversed")] = True
                df.iloc[i + 1, df.columns.get_loc("is_reversed")] = True

    reversal_count = df["is_reversed"].sum()
    if reversal_count > 0:
        logger.info(
            "validator._mark_reversals: "
            "Marked %d reversed/failed transactions.",
            reversal_count,
        )

    return df
