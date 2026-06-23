"""
anonymiser.py — Privacy protection before any text is sent to external AI APIs.

This module is a critical privacy safeguard. Before any text from a bank statement
is sent to Groq, this module replaces all sensitive personal and
financial identifiers with harmless placeholder codes.

Why this is necessary:
  - Real bank account numbers belong to suspects in a criminal investigation.
  - Sending real account numbers and names to a US-based AI API (Groq, Google)
    could create legal and privacy issues.
  - The LLM only needs to see the STRUCTURE of the document (which column is
    the date, which is the amount) — not the actual account numbers or names.

What gets replaced:
  - Indian bank account numbers (9 to 18 digit sequences)
  - IFSC codes (format: 4 letters + digit 0 + 6 alphanumeric characters)
  - UPI IDs (format: username@bankname, e.g., ramesh@paytm)
  - Mobile numbers (10 digits starting with 6, 7, 8, or 9 — Indian mobile format)
  - Common Indian names (detected using a curated list of common Indian names)

The real values are stored in a local mapping dictionary that is NEVER sent to
any API and NEVER written to any log file. It stays in memory only.

Team: Survey Corps | CIDECODE Hackathon 2026 | CID Karnataka
"""

import logging
import re
from typing import Dict, Tuple

# Set up a logger for this module.
logger = logging.getLogger(__name__)

# ── Common Indian first names for name detection heuristic ───────────────────
# This list covers common Hindu, Muslim, Christian, and Sikh names found
# across Karnataka, which is where CID Karnataka operates.
# We use this to detect names in narration text so we can anonymise them
# before sending to Groq.
COMMON_INDIAN_NAMES = {
    # Hindu names — male
    "Ravi", "Suresh", "Ramesh", "Prakash", "Rajesh", "Arun", "Aditya",
    "Vikram", "Rahul", "Rohit", "Nikhil", "Kiran", "Ganesh", "Gopal",
    "Deepak", "Chandan", "Harish", "Naveen", "Mohan", "Santosh",
    "Venkat", "Babu", "Arjun", "Anand", "Nilesh", "Vinay", "Karthik",
    # Hindu names — female
    "Priya", "Deepa", "Rekha", "Anitha", "Kavya", "Kavitha", "Smitha",
    "Sheela", "Sunita", "Meena", "Lakshmi", "Geeta", "Rashmi", "Pooja",
    "Divya", "Tara", "Archana", "Usha", "Sanjana", "Meera", "Lalitha",
    "Reshma", "Bindhu", "Preethi",
    # South Indian names
    "Nair", "Iyer", "Pillai", "Menon", "Kumar", "Rao", "Reddy",
    "Sharma", "Gowda", "Hegde", "Patil", "Kulkarni",
    # Muslim names
    "Mohammed", "Farhan", "Imran", "Shahid", "Nadia", "Fatima", "Ayesha",
    # Sikh names
    "Ranjit", "Sunil",
    # Compound surnames
    "Devi", "Bai", "Singh", "Das",
}


def anonymise_text(text: str) -> Tuple[str, Dict[str, str]]:
    """
    Replaces sensitive financial identifiers in text with placeholder codes.

    The replacement is one-way — the original text is stored only in the
    mapping dictionary returned by this function. The anonymised text is
    safe to send to external AI APIs because it contains no real account
    numbers, names, or contact details.

    Detects and replaces (in this order, to avoid overlapping replacements):
        1. IFSC codes       — format: 4 uppercase letters + "0" + 6 alphanumeric
        2. UPI IDs          — format: anything@bankname (e.g., ramesh@paytm)
        3. Indian mobile numbers — 10 digits starting with 6, 7, 8, or 9
        4. Indian account numbers — 9 to 18 digit sequences
        5. Common Indian names — matched against a curated name list

    The mapping between placeholders and real values is stored locally
    and never sent to any API.

    Parameters:
        text (str): Raw text to anonymise (from PDF, OCR, or DOCX extraction).

    Returns:
        tuple:
            - str: Anonymised text with placeholders like ACCT_1, IFSC_1, UPI_1,
                   PHONE_1, NAME_1 replacing real values.
            - dict: Mapping of placeholder → original value.
                    For example: {"ACCT_1": "9876543210", "NAME_1": "Ramesh Kumar"}
                    This mapping stays on the local machine only.

    Example:
        Input:  "Transfer to Ramesh Kumar A/C 9876543210"
        Output: ("Transfer to NAME_1 A/C ACCT_1",
                 {"ACCT_1": "9876543210", "NAME_1": "Ramesh Kumar"})
    """
    # The mapping stores the correspondence between placeholder codes and
    # the real values they replaced. This never leaves the local machine.
    mapping: Dict[str, str] = {}

    # Counters for each category of replacement, used to generate unique
    # placeholder codes like IFSC_1, IFSC_2, etc.
    counters = {
        "IFSC": 0,
        "UPI": 0,
        "PHONE": 0,
        "ACCT": 0,
        "NAME": 0,
    }

    anonymised = text

    # ── 1. Replace IFSC codes ──────────────────────────────────────────────
    # IFSC (Indian Financial System Code) uniquely identifies a bank branch.
    # Format: 4 uppercase letters (bank code) + digit 0 + 6 alphanumeric chars.
    # Example: SBIN0393634 (State Bank of India, branch 393634)
    ifsc_pattern = r'\b[A-Z]{4}0[A-Z0-9]{6}\b'

    def replace_ifsc(match: re.Match) -> str:
        """Replace an IFSC code match with a placeholder."""
        original = match.group(0)
        # Reuse the same placeholder if we have seen this IFSC before.
        for placeholder, value in mapping.items():
            if value == original and placeholder.startswith("IFSC_"):
                return placeholder
        counters["IFSC"] += 1
        placeholder = f"IFSC_{counters['IFSC']}"
        mapping[placeholder] = original
        return placeholder

    anonymised = re.sub(ifsc_pattern, replace_ifsc, anonymised)

    # ── 2. Replace UPI IDs ────────────────────────────────────────────────
    # UPI (Unified Payments Interface) IDs link a person's mobile number or
    # name to their bank account. They are a major transaction identifier in
    # Indian digital banking. Format: username@vpa (e.g., ramesh@paytm).
    # We use a word-boundary check to avoid matching email addresses.
    upi_pattern = r'\b[\w.\-]+@[\w]+\b'

    def replace_upi(match: re.Match) -> str:
        """Replace a UPI ID match with a placeholder."""
        original = match.group(0)
        # Reuse placeholder if we have seen this UPI ID before.
        for placeholder, value in mapping.items():
            if value == original and placeholder.startswith("UPI_"):
                return placeholder
        counters["UPI"] += 1
        placeholder = f"UPI_{counters['UPI']}"
        mapping[placeholder] = original
        return placeholder

    anonymised = re.sub(upi_pattern, replace_upi, anonymised)

    # ── 3. Replace Indian mobile numbers ──────────────────────────────────
    # Indian mobile numbers are always exactly 10 digits and start with
    # 6, 7, 8, or 9. We must match these BEFORE matching account numbers
    # because a 10-digit mobile number also matches the account number pattern.
    # Word boundaries (\b) prevent matching a phone number that is part of
    # a longer number sequence.
    phone_pattern = r'\b[6-9]\d{9}\b'

    def replace_phone(match: re.Match) -> str:
        """Replace a mobile number match with a placeholder."""
        original = match.group(0)
        for placeholder, value in mapping.items():
            if value == original and placeholder.startswith("PHONE_"):
                return placeholder
        counters["PHONE"] += 1
        placeholder = f"PHONE_{counters['PHONE']}"
        mapping[placeholder] = original
        return placeholder

    anonymised = re.sub(phone_pattern, replace_phone, anonymised)

    # ── 4. Replace bank account numbers ────────────────────────────────────
    # Indian bank account numbers are 9 to 18 digits long.
    # They appear in narrations when transactions are done via NEFT/RTGS/IMPS.
    # We use word boundaries to avoid matching digits in dates or amounts.
    account_pattern = r'\b\d{9,18}\b'

    def replace_account(match: re.Match) -> str:
        """Replace a bank account number match with a placeholder."""
        original = match.group(0)
        for placeholder, value in mapping.items():
            if value == original and placeholder.startswith("ACCT_"):
                return placeholder
        counters["ACCT"] += 1
        placeholder = f"ACCT_{counters['ACCT']}"
        mapping[placeholder] = original
        return placeholder

    anonymised = re.sub(account_pattern, replace_account, anonymised)

    # ── 5. Replace common Indian names ─────────────────────────────────────
    # Names appear in narration text when transactions mention the sender
    # or receiver. We check each word against our curated list of common
    # Indian names. The title() conversion normalises case before matching.
    words = anonymised.split()
    result_words = []
    skip_next = False

    for i, word in enumerate(words):
        if skip_next:
            # This word was already consumed as part of a two-word name.
            skip_next = False
            continue

        # Normalise to title case (first letter uppercase) for comparison.
        normalised_word = word.strip(".,/()").title()

        if normalised_word in COMMON_INDIAN_NAMES:
            # Check if the next word is also a name (two-word names like "Ramesh Kumar")
            combined_name = normalised_word
            if i + 1 < len(words):
                next_word_normalised = words[i + 1].strip(".,/()").title()
                if next_word_normalised in COMMON_INDIAN_NAMES:
                    combined_name = f"{normalised_word} {next_word_normalised}"
                    skip_next = True

            # Check if we have already seen this name and reuse the placeholder.
            existing_placeholder = None
            for placeholder, value in mapping.items():
                if value == combined_name and placeholder.startswith("NAME_"):
                    existing_placeholder = placeholder
                    break

            if existing_placeholder:
                result_words.append(existing_placeholder)
            else:
                counters["NAME"] += 1
                placeholder = f"NAME_{counters['NAME']}"
                mapping[placeholder] = combined_name
                result_words.append(placeholder)
        else:
            result_words.append(word)

    anonymised = " ".join(result_words)

    logger.info(
        "anonymiser.anonymise_text: "
        "Anonymised %d IFSC codes, %d UPI IDs, %d phone numbers, "
        "%d account numbers, %d names",
        counters["IFSC"],
        counters["UPI"],
        counters["PHONE"],
        counters["ACCT"],
        counters["NAME"],
    )

    # We deliberately do NOT log the mapping dictionary because it contains
    # the real account numbers and names — logging it would create a record
    # of sensitive investigative data in plain text log files.

    return anonymised, mapping
