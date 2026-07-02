# Copied from: analysis phase cidecode hackathon/analysis phase cidecode hackathon/analysis_engine/utils.py
"""Small deterministic helpers shared across modules."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
import math
import re
import unicodedata
from typing import Any

import pandas as pd


SPACE_RE = re.compile(r"\s+")
NON_ALNUM_RE = re.compile(r"[^A-Z0-9]+")
NULL_ACCOUNT_TOKENS = {"", "NAN", "NONE", "NULL", "NAT", "<NA>"}


def normalize_text(value: Any) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    text = unicodedata.normalize("NFKC", str(value)).upper().strip()
    return SPACE_RE.sub(" ", text)


def canonical_account_id(value: Any) -> str:
    """Return a usable account/counterparty identifier or blank for null-like values."""
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip()
    return "" if text.upper() in NULL_ACCOUNT_TOKENS else text


def normalize_compact(value: Any) -> str:
    return NON_ALNUM_RE.sub("", normalize_text(value))


def normalize_name(value: Any) -> str:
    tokens = [
        token
        for token in NON_ALNUM_RE.split(normalize_text(value))
        if token and token not in {"MR", "MRS", "MS", "DR", "SHRI", "SMT"}
    ]
    return " ".join(tokens)


def parse_amount(value: Any) -> float:
    if value is None:
        return 0.0
    text = str(value).strip()
    if not text:
        return 0.0
    negative = text.startswith("(") and text.endswith(")")
    cleaned = re.sub(r"[^0-9.\-]", "", text)
    try:
        amount = float(cleaned)
    except (TypeError, ValueError):
        return 0.0
    return -abs(amount) if negative else amount


def parse_date(value: Any) -> str | None:
    if value is None or not str(value).strip():
        return None
    parsed = pd.to_datetime(str(value).strip(), dayfirst=True, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date().isoformat()


def parse_time(value: Any) -> str:
    text = "" if value is None else str(value).strip()
    if not text:
        return ""
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).time().isoformat()
        except ValueError:
            continue
    return text


def lowest_confidence(tiers: Iterable[str]) -> str:
    normalized = {str(tier).lower() for tier in tiers}
    return "low" if "low" in normalized or "lower_confidence" in normalized else "high"


def safe_ratio(numerator: float, denominator: float) -> float:
    return float(numerator / denominator) if denominator else 0.0


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))
