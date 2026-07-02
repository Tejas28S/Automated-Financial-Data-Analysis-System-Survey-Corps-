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
from typing import Any, Dict, List, Tuple

import pandas as pd

from config.settings import (
    BALANCE_TOLERANCE,
    ACCEPT_RECONCILE_RATE,
    MIN_COMPLETENESS_RATIO,
)

# Set up a logger for this module.
logger = logging.getLogger(__name__)

# Keywords in narration that suggest a transaction was reversed or failed
REVERSAL_KEYWORDS = ["reversal", "failed", "return", "reversed", "failure", "bounce", "dishonour"]
SOURCE_ACCOUNT_ID_COLUMN = "source_account_id"


def _account_group_column(df: pd.DataFrame) -> str:
    return SOURCE_ACCOUNT_ID_COLUMN if SOURCE_ACCOUNT_ID_COLUMN in df.columns else "Account_ID"


# ─────────────────────────────────────────────────────────────────────────────
# THE REFEREE — grade_parse()
#
# This is the heart of the Validation-Arbitrated Tiered Hybrid. It does NOT clean
# the data; it GRADES a candidate parse so the pipeline can decide whether the
# cheap deterministic parse was good enough or must escalate to the LLM.
#
# The key idea: a bank statement prints a RUNNING BALANCE, so for every row
#     balance(this row) == balance(previous row) + credit - debit
# This is arithmetic that is true for EVERY bank on earth, so the check never
# needs to know which bank produced the statement (no overfitting). If the
# column/direction guesses are wrong, the chain breaks and we detect it with no
# answer key. grade_parse measures how much of the chain holds and returns a
# verdict the pipeline uses to escalate. It is provider-independent.
# ─────────────────────────────────────────────────────────────────────────────


def _is_valid_date(date_value: Any) -> bool:
    """True if the value is a real, plausible transaction date (2000–2035)."""
    if pd.isna(date_value):
        return False
    if not isinstance(date_value, pd.Timestamp):
        return False
    return 2000 <= date_value.year <= 2035


def _as_float(value: Any) -> float:
    """Coerce a Debit/Credit/Balance cell to float; missing/non-numeric → 0.0."""
    if pd.isna(value) or value is None:
        return 0.0
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0.0


def _reconcile_for_order(
    balances: List[float], debits: List[float], credits: List[float],
    order: List[int], tol: float,
) -> Tuple[float, set, int]:
    """
    Walks the rows in the given ORDER and checks the running-balance chain.

    For each row after the first (the anchor), verifies:
        balance[i] ≈ balance[i-1] + credit[i] - debit[i]   (within `tol`)

    `order` is a list of ORIGINAL row positions in the sequence to walk — passing
    the reversed order is how we test a newest-first statement (its chronological
    order is bottom-to-top). The first row in the order is the anchor: it has no
    predecessor to check, so it is never counted as failing (this is also how a
    statement with no printed opening balance is handled — row 1 is simply the
    anchor and validation runs from row 2, and row 1 is never dropped).

    Returns: (reconciliation_rate, set_of_failing_ORIGINAL_positions, rows_checked).
    """
    failing: set = set()
    checked = 0
    for k in range(1, len(order)):
        prev_pos, cur_pos = order[k - 1], order[k]
        prev_b, cur_b = balances[prev_pos], balances[cur_pos]
        checked += 1
        expected = prev_b + credits[cur_pos] - debits[cur_pos]
        if abs(expected - cur_b) > tol:
            failing.add(cur_pos)
    rate = (checked - len(failing)) / checked if checked else 0.0
    return rate, failing, checked


def grade_parse(df: pd.DataFrame, expected_rows: int = None) -> Dict[str, Any]:
    """
    Grades a candidate parsed DataFrame so the pipeline can decide whether to
    accept it or escalate to the LLM. Does NOT modify the DataFrame.

    Returns a dict:
        {
          "reconciliation_rate": float,   # fraction of balance-chain rows that hold
          "completeness_ratio": float,    # parsed_rows / expected_rows (1.0 if unknown)
          "ordering": "oldest_first" | "newest_first" | "unknown",
          "has_balance_column": bool,
          "failing_row_indices": [int],   # ORIGINAL positions to repair / flag
          "rows_checked": int,
          "verdict": "PASS" | "FAIL",
        }

    PASS (cheap parse trusted, no LLM needed) requires, for a statement that has a
    running balance: reconciliation_rate ≥ ACCEPT_RECONCILE_RATE AND completeness ≥
    MIN_COMPLETENESS_RATIO. For a statement with NO balance column (some Excel
    exports), we cannot reconcile, so we fall back to a weaker proxy — debit/credit
    exclusivity + valid dates — and only PASS if that proxy is clean; otherwise we
    escalate, because the strongest check is unavailable.
    """
    n = len(df)
    result: Dict[str, Any] = {
        "reconciliation_rate": 0.0,
        "completeness_ratio": 1.0,
        "ordering": "unknown",
        "has_balance_column": False,
        "failing_row_indices": [],
        "rows_checked": 0,
        "verdict": "FAIL",
    }
    if df is None or n == 0:
        return result

    # Completeness: how many of the transaction-like lines did we actually capture?
    if expected_rows and expected_rows > 0:
        result["completeness_ratio"] = min(1.0, n / float(expected_rows))

    # Coerce the numeric columns defensively (they are normally already floats).
    balances = [_as_float(v) for v in df["Balance"]] if "Balance" in df.columns else [0.0] * n
    debits = [_as_float(v) for v in df["Debit"]] if "Debit" in df.columns else [0.0] * n
    credits = [_as_float(v) for v in df["Credit"]] if "Credit" in df.columns else [0.0] * n

    # A real running balance varies row to row. If the Balance column is absent,
    # all-zero, or constant, treat the statement as having NO usable balance column.
    distinct_nonzero = len({round(b, 2) for b in balances if abs(b) > 0.0})
    has_balance = ("Balance" in df.columns) and distinct_nonzero > 1
    result["has_balance_column"] = has_balance

    # Rows that are structurally broken regardless of balance: both debit AND
    # credit filled (column misalignment), or an unparseable date. These are
    # always failing rows and always candidates for repair.
    structural_fail: set = set()
    for i in range(n):
        if debits[i] > 0.01 and credits[i] > 0.01:
            structural_fail.add(i)
        if "Date" in df.columns and not _is_valid_date(df["Date"].iloc[i]):
            structural_fail.add(i)

    if has_balance:
        # Try BOTH directions and keep whichever reconciles better — this is how a
        # newest-first statement (chain runs bottom-to-top) is detected.
        fwd_rate, fwd_fail, fwd_checked = _reconcile_for_order(
            balances, debits, credits, list(range(n)), BALANCE_TOLERANCE)
        rev_rate, rev_fail, rev_checked = _reconcile_for_order(
            balances, debits, credits, list(range(n - 1, -1, -1)), BALANCE_TOLERANCE)
        if rev_rate > fwd_rate:
            result["ordering"] = "newest_first"
            rate, failing, checked = rev_rate, rev_fail, rev_checked
        else:
            result["ordering"] = "oldest_first"
            rate, failing, checked = fwd_rate, fwd_fail, fwd_checked
        result["reconciliation_rate"] = rate
        result["rows_checked"] = checked
        failing = failing | structural_fail
        result["failing_row_indices"] = sorted(failing)
        result["verdict"] = (
            "PASS" if rate >= ACCEPT_RECONCILE_RATE
            and result["completeness_ratio"] >= MIN_COMPLETENESS_RATIO
            else "FAIL"
        )
    else:
        # No running balance to verify against — fall back to a weaker proxy so we
        # do not needlessly escalate a clean Excel that simply omits the balance:
        # PASS only if debit/credit exclusivity and dates are clean.
        proxy_rate = (n - len(structural_fail)) / n if n else 0.0
        result["reconciliation_rate"] = proxy_rate  # reported as best-available confidence
        result["failing_row_indices"] = sorted(structural_fail)
        result["verdict"] = (
            "PASS" if proxy_rate >= ACCEPT_RECONCILE_RATE
            and result["completeness_ratio"] >= MIN_COMPLETENESS_RATIO
            else "FAIL"
        )

    logger.info(
        "validator.grade_parse: rows=%d checked=%d reconcile=%.3f complete=%.3f "
        "ordering=%s balance_col=%s failing=%d verdict=%s",
        n, result["rows_checked"], result["reconciliation_rate"],
        result["completeness_ratio"], result["ordering"], has_balance,
        len(result["failing_row_indices"]), result["verdict"],
    )
    return result


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

    # ── Drop statement SUMMARY / TOTALS rows (not transactions) ───────────────
    # Some templates print a "Total Debits / Total Credits" row at the end with a
    # placeholder impossible date (e.g. 01/01/6999) and a narration that is just a
    # row count ("2382"). These are statement metadata, not transactions — remove them
    # so they are neither counted nor surfaced as invalid_date flags. The test is
    # deliberately strict (impossible year AND a purely-numeric narration), so a real
    # transaction can never match. Logged, never silently relevant to a real row.
    working_df = _drop_summary_rows(working_df)
    if working_df.empty:
        empty = df.iloc[0:0].copy()
        return empty, empty

    # Initialise the "flag_reason" column for tracking why rows are flagged
    working_df["flag_reason"] = None  # None means "not flagged yet"
    # Companion diagnostic column: for a balance_mismatch row it carries the PROBABLE
    # extraction cause (missing_amount / direction_inverted / missing_transaction …)
    # so the investigator and the analysis phase can act on it. Blank for every other
    # row. flag_reason itself is left untouched (the output contract the tests and the
    # analysis phase depend on).
    working_df["mismatch_diagnosis"] = ""

    # ── Check 1: Date Validity ────────────────────────────────────────────────
    working_df = _check_date_validity(working_df)

    # ── Check 2: Balance Arithmetic ───────────────────────────────────────────
    # Only run this check on rows that passed Check 1 (have valid dates).
    # Running balance arithmetic on rows with invalid dates would give meaningless results.
    working_df = _check_balance_arithmetic(working_df)

    # ── Check 3: Debit/Credit Exclusivity ─────────────────────────────────────
    working_df = _check_debit_credit_exclusivity(working_df)

    # ── Check 4: Narration-bloat safety net (multiple transactions in one row) ──
    working_df = _check_narration_bloat(working_df)

    # ── Split into clean and flagged ──────────────────────────────────────────
    flagged_mask = working_df["flag_reason"].notna()
    flagged_df = working_df[flagged_mask].copy()
    clean_df = working_df[~flagged_mask].copy()

    # Remove the flag_reason column from the clean DataFrame (it's always None there).
    # The mismatch_diagnosis companion is also clean-only noise here (always blank),
    # so it stays on flagged rows and is dropped from the clean table.
    clean_df = clean_df.drop(columns=["flag_reason"])
    if "mismatch_diagnosis" in clean_df.columns:
        clean_df = clean_df.drop(columns=["mismatch_diagnosis"])

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


import re as _re


def _drop_summary_rows(df: pd.DataFrame) -> pd.DataFrame:
    """
    Removes statement summary/totals rows (impossible placeholder date AND a narration
    that is purely a row-count number). Strict by design — a real transaction never has
    both an impossible year and a numeric-only narration — so no transaction is lost.
    """
    if df.empty or "Date" not in df.columns:
        return df

    def _is_summary(row) -> bool:
        dt = row["Date"]
        if not isinstance(dt, pd.Timestamp) or pd.isna(dt):
            return False
        if 1950 <= dt.year <= 2100:
            return False  # plausible year → a real (or normally-flagged) row
        narr = str(row.get("Narration", "")).strip()
        return bool(_re.fullmatch(r"\d{1,7}", narr))  # narration is only a count

    mask = df.apply(_is_summary, axis=1)
    n = int(mask.sum())
    if n:
        logger.info("validator._drop_summary_rows: removed %d statement summary/totals "
                    "row(s) (impossible date + numeric-only narration).", n)
        return df[~mask].reset_index(drop=True)
    return df


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


def _classify_balance_mismatch(
    prev_balance: float, curr_balance: float, debit: float, credit: float,
    tol: float = BALANCE_TOLERANCE,
) -> str:
    """
    Best-effort classification of WHY a balance row failed to reconcile, so the
    validator becomes an extraction-debugging tool rather than a bare arithmetic
    checker (Issues 3 & 7). Pure row-local arithmetic — no bank knowledge, so it
    generalises to every statement.

    The running balance is internally consistent in a real export, so a mismatch is
    almost always an EXTRACTION defect. The shape of the discrepancy points at which
    defect it is:

      • missing_amount        — the balance moved but BOTH debit and credit are zero:
                                the transaction amount was never captured. This is the
                                signature of a lost continuation line, a page-break
                                split, or an OCR omission on the amount column.
      • direction_inverted    — swapping debit<->credit reconciles the row: the
                                direction was read the wrong way (a credit parsed as a
                                debit or vice-versa).
      • missing_transaction   — this row's own amount is applied correctly but a gap
                                remains: a whole transaction BETWEEN the previous row
                                and this one was not extracted (page-break drop / OCR
                                omission / parser row loss).
      • balance_jump_no_change— the balance changed while this row records no movement
                                and no amount: a structural row loss.

    Returns a short diagnosis string (never raises).
    """
    expected = prev_balance + credit - debit
    diff = curr_balance - expected
    actual_delta = curr_balance - prev_balance

    if debit <= tol and credit <= tol:
        return "missing_amount" if abs(actual_delta) > tol else "balance_jump_no_change"

    # Would swapping the direction reconcile it?
    if abs((prev_balance + debit - credit) - curr_balance) <= tol:
        return "direction_inverted"

    if abs(diff) > tol:
        return "missing_transaction"

    return "balance_mismatch"


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

    group_col = _account_group_column(df)
    for account_id in df[group_col].unique():
        # Process each account's transactions independently
        account_mask = (df[group_col] == account_id) & unflagged_mask
        account_indices = df.index[account_mask].tolist()

        if len(account_indices) < 2:
            # Need at least 2 rows to check balance arithmetic
            continue

        # Skip the running-balance check entirely when this account has NO usable
        # balance column — a real running balance varies row to row, so an absent /
        # all-zero / constant Balance means the source simply did not print one (e.g.
        # a core-banking export with only an amount + Dr/Cr flag). Reconciling against
        # a non-existent balance would wrongly flag EVERY transaction. Mirrors the
        # has_balance test in grade_parse so the two agree.
        acct_balances = [_as_float(df.loc[ix, "Balance"]) for ix in account_indices]
        if len({round(b, 2) for b in acct_balances if abs(b) > 0.0}) <= 1:
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
                # Classify the PROBABLE extraction cause so the flagged row is a
                # debugging lead, not just "mismatch" (Issues 3 & 7).
                df.loc[curr_idx, "mismatch_diagnosis"] = _classify_balance_mismatch(
                    prev_balance, curr_balance, debit, credit)
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


# A date anywhere in a narration — used only to count how many transactions a bloated
# narration has swallowed. Generic shapes; no bank/format assumption.
_DATE_IN_NARRATION = __import__("re").compile(
    r"\d{1,2}[/\-][0-9A-Za-z]{2,9}[/\-]\d{2,4}|\d{4}[/\-]\d{1,2}[/\-]\d{1,2}")


def _check_narration_bloat(df: pd.DataFrame) -> pd.DataFrame:
    """
    Detection safety net for the narration-bloat defect (multiple transactions glued
    into one row's Narration when the row-start detector misses a template's row shape
    — Bug Group F). It does NOT fix parsing; it makes the failure VISIBLE and countable
    instead of hiding inside the generic balance-mismatch bucket.

    Deliberately strict so it never reclassifies a legitimately verbose single
    narration: a row is flagged only if its Narration is very long (>400 chars) AND
    contains ≥4 date-like substrings — a shape no real single UPI/NEFT/IMPS narration
    in this dataset comes close to. Only applies to rows not already flagged.
    """
    if "Narration" not in df.columns:
        return df
    narr = df["Narration"].astype(str)
    n_dates = narr.apply(lambda s: len(_DATE_IN_NARRATION.findall(s)))
    bloated = (narr.str.len() > 400) & (n_dates >= 4) & df["flag_reason"].isna()
    df.loc[bloated, "flag_reason"] = "narration_contains_multiple_transactions"
    cnt = int(bloated.sum())
    if cnt:
        logger.info("validator._check_narration_bloat: flagged %d row(s) whose narration "
                    "swallowed multiple transactions.", cnt)
    return df


def mark_duplicates(df: pd.DataFrame) -> pd.DataFrame:
    """
    Tags exact duplicate transactions WITHOUT deleting any of them (Problem 6).

    A duplicate is a row where Date, Narration, Debit, Credit, and the reliable
    per-source account key are all identical to an earlier row. This can happen
    when a multi-page PDF repeats a header row, a file is uploaded twice, or two
    statements overlap in period.

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

    group_col = _account_group_column(df)

    # Give every row a stable id (account/source + position) so we can reference it.
    df["txn_id"] = [
        f"{acc}_{i:06d}" for i, acc in enumerate(df[group_col].tolist())
    ]
    df["duplicate_of"] = None

    key_cols = ["Date", "Narration", "Debit", "Credit", group_col]
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
    # Coerce to str first: a statement may have a blank/NaN narration cell, and a
    # float NaN is not iterable — without this the keyword scan crashes the whole run.
    narration_lower = df["Narration"].astype(str).str.lower()
    keyword_reversal_mask = narration_lower.apply(
        lambda narration: any(keyword in narration for keyword in REVERSAL_KEYWORDS)
    )
    df.loc[keyword_reversal_mask, "is_reversed"] = True

    # ── Mark reversals by debit-followed-by-equal-credit pattern ─────────────
    # For each debit row, check if the next row is a credit for the same amount
    # with a reversal keyword. This catches cases where the narration says
    # "REVERSAL" on the credit side but not the debit side.
    group_col = _account_group_column(df)
    for i in range(len(df) - 1):
        curr_row = df.iloc[i]
        next_row = df.iloc[i + 1]

        # Check: current row is a debit, next row is a credit for same amount
        if (
            curr_row["Debit"] > 0 and
            next_row["Credit"] > 0 and
            abs(curr_row["Debit"] - next_row["Credit"]) < BALANCE_TOLERANCE and
            curr_row[group_col] == next_row[group_col]
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
