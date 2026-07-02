# Copied from: analysis phase cidecode hackathon/analysis phase cidecode hackathon/analysis_engine/counterparties.py
"""Deterministic narration parsing with a cached, optional LLM fallback."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import sqlite3

import pandas as pd

from .config import AnalysisConfig
from .utils import normalize_compact, normalize_name, normalize_text


IFSC_RE = re.compile(r"(?<![A-Z0-9])([A-Z]{4}0[A-Z0-9]{6})(?![A-Z0-9])", re.IGNORECASE)
VPA_RE = re.compile(r"(?<![A-Z0-9._-])([A-Z0-9._-]{2,}@[A-Z][A-Z0-9._-]{1,})(?![A-Z0-9._-])", re.IGNORECASE)
ACCOUNT_RE = re.compile(r"(?<!\d)(\d{6,18})(?!\d)")
NAME_AFTER_DIRECTION_RE = re.compile(
    r"(?:^|[/\-])(?:DR|CR|TO|FROM)[: ]*/?([A-Z][A-Z .]{2,40})(?:/|$)", re.IGNORECASE
)
VOCABULARY_RE = re.compile(r"\b(?:NEFT|IMPS|RTGS|UPI|ATM|POS|CHQ|CLG|ECS|NACH)\b", re.IGNORECASE)
GENERIC_RE = re.compile(
    r"^(?:OPENING BALANCE|CLOSING BALANCE|BALANCE FORWARD|INTEREST(?: CREDIT)?|"
    r"HALF YEAR INTEREST|SMS ALERT CHARGES|MAB CHARGES|GST|CASH WITHDRAWAL|ATM WDL|"
    r"NACH DR|ECSRTNCHGS)",
    re.IGNORECASE,
)


@dataclass
class Resolution:
    counterparty_account: str = ""
    counterparty_ifsc: str = ""
    counterparty_name_raw: str = ""
    counterparty_resolution_method: str = "unresolved"
    counterparty_resolution_confidence: float = 0.0

    @property
    def found_anything(self) -> bool:
        return bool(self.counterparty_account or self.counterparty_ifsc or self.counterparty_name_raw)


def _known_entities(connection: sqlite3.Connection) -> tuple[set[str], dict[str, list[tuple[str, str]]]]:
    accounts = pd.read_sql_query(
        "SELECT account_id, account_holder FROM accounts WHERE COALESCE(account_id, '') != ''",
        connection,
    )
    known_accounts = set(accounts["account_id"].astype(str))
    holders: dict[str, list[tuple[str, str]]] = {}
    for row in accounts.itertuples(index=False):
        normalized = normalize_name(row.account_holder)
        if normalized:
            holders.setdefault(normalized, []).append((str(row.account_id), str(row.account_holder)))
    return known_accounts, holders


def _deterministic_resolution(
    row: pd.Series,
    known_accounts: set[str],
    holders: dict[str, list[tuple[str, str]]],
) -> Resolution:
    narration = str(row["narration"] or "")
    normalized = normalize_text(narration)
    compact = normalize_compact(narration)
    own_account = str(row["account_id"] or "")
    own_ifsc = normalize_text(row["ifsc_code"] or "")
    reference_values = {
        normalize_compact(row.get("reference", "")),
        normalize_compact(row.get("reference_alt", "")),
    }

    account_matches = [
        account for account in known_accounts
        if account != own_account and normalize_compact(account) and normalize_compact(account) in compact
    ]
    account_matches = list(dict.fromkeys(account_matches))
    holder_matches: list[tuple[str, str]] = []
    for normalized_holder, entities in holders.items():
        if len(normalized_holder.replace(" ", "")) < 5:
            continue
        if normalized_holder.replace(" ", "") in compact:
            holder_matches.extend(entity for entity in entities if entity[0] != own_account)
    holder_accounts = list(dict.fromkeys(account for account, _ in holder_matches))
    holder_names = list(dict.fromkeys(name for _, name in holder_matches))

    ifscs = [value.upper() for value in IFSC_RE.findall(narration)]
    ifscs = [value for value in dict.fromkeys(ifscs) if value != own_ifsc]
    vpas = list(dict.fromkeys(value.lower() for value in VPA_RE.findall(narration)))
    vpas = [value for value in vpas if normalize_compact(own_account) not in normalize_compact(value)]

    numeric_candidates = []
    for candidate in ACCOUNT_RE.findall(narration):
        normalized_candidate = normalize_compact(candidate)
        if candidate == own_account or normalized_candidate in reference_values:
            continue
        if any(normalized_candidate in normalize_compact(ifsc) for ifsc in ifscs):
            continue
        numeric_candidates.append(candidate)
    numeric_candidates = list(dict.fromkeys(numeric_candidates))

    account = ""
    name = ""
    confidence = 0.0
    method = "unresolved"
    if len(account_matches) == 1:
        account = account_matches[0]
        confidence = 1.0
        method = "exact_reference_or_account_match"
    elif len(holder_accounts) == 1:
        account = holder_accounts[0]
        confidence = 0.65
        method = "narration_similarity_match"
    elif len(vpas) == 1:
        account = vpas[0]
        confidence = 1.0
        method = "exact_reference_or_upi_match"
    elif len(numeric_candidates) == 1:
        account = numeric_candidates[0]
        confidence = 0.65
        method = "narration_similarity_match"

    if len(holder_names) == 1:
        name = holder_names[0]
    else:
        name_match = NAME_AFTER_DIRECTION_RE.search(normalized)
        if name_match:
            name = name_match.group(1).strip(" /-")

    result = Resolution(
        counterparty_account=account,
        counterparty_ifsc=ifscs[0] if len(ifscs) == 1 else "",
        counterparty_name_raw=name,
        counterparty_resolution_method=method if account or ifscs or name else "unresolved",
        counterparty_resolution_confidence=confidence if account or ifscs or name else 0.0,
    )
    return result


def _pair_ledger_rows(frame: pd.DataFrame, config: AnalysisConfig) -> list[tuple[int, int, str, str, float]]:
    candidates = frame[
        (frame["eligible_for_detection"] == 1)
        & frame["date"].notna()
    ]
    pairs: list[tuple[int, int, str, str, float]] = []
    used: set[int] = set()
    referenced = candidates[candidates["reference"].fillna("").ne("")]
    for reference, group in referenced.groupby("reference", sort=False):
        debits = group[group["debit_amount"] > config.money_epsilon]
        credits = group[group["credit_amount"] > config.money_epsilon]
        for debit in debits.itertuples():
            possible = credits[
                (credits["account_id"] != debit.account_id)
                & (~credits["row_id"].isin(used))
            ].copy()
            if possible.empty:
                continue
            possible["amount_difference"] = (possible["credit_amount"] - debit.debit_amount).abs()
            tolerance = max(
                config.money_epsilon,
                abs(float(debit.debit_amount)) * config.duplicate_amount_relative_tolerance,
            )
            possible = possible[possible["amount_difference"] <= tolerance]
            if possible.empty:
                continue
            possible["date_difference"] = (
                pd.to_datetime(possible["date"]) - pd.to_datetime(debit.date)
            ).dt.days.abs()
            possible = possible[possible["date_difference"] <= config.duplicate_date_window_days]
            if possible.empty:
                continue
            credit = possible.sort_values(["date_difference", "amount_difference", "source_order"]).iloc[0]
            pair_id = f"ledger::{debit.row_id}::{int(credit.row_id)}::{normalize_compact(reference)}"
            pairs.append(
                (
                    int(debit.row_id),
                    int(credit.row_id),
                    pair_id,
                    "exact_reference_or_upi_match",
                    1.0,
                )
            )
            used.add(int(debit.row_id))
            used.add(int(credit.row_id))

    debits = candidates[
        (candidates["debit_amount"] > config.money_epsilon)
        & (~candidates["row_id"].isin(used))
    ]
    credits = candidates[
        (candidates["credit_amount"] > config.money_epsilon)
        & (~candidates["row_id"].isin(used))
    ]
    for debit in debits.itertuples():
        possible = credits[
            (credits["account_id"] != debit.account_id)
            & (~credits["row_id"].isin(used))
        ].copy()
        if possible.empty:
            continue
        possible["amount_difference"] = (possible["credit_amount"] - debit.debit_amount).abs()
        tolerance = max(
            config.money_epsilon,
            abs(float(debit.debit_amount)) * config.duplicate_amount_relative_tolerance,
        )
        possible = possible[possible["amount_difference"] <= tolerance]
        if possible.empty:
            continue
        possible["date_difference"] = (
            pd.to_datetime(possible["date"]) - pd.to_datetime(debit.date)
        ).dt.days.abs()
        possible = possible[possible["date_difference"] <= config.duplicate_date_window_days]
        if possible.empty:
            continue
        credit = possible.sort_values(["date_difference", "amount_difference", "source_order"]).iloc[0]
        pair_id = f"mirror::{debit.row_id}::{int(credit.row_id)}"
        pairs.append(
            (
                int(debit.row_id),
                int(credit.row_id),
                pair_id,
                "amount_date_mirror_match",
                0.85,
            )
        )
        used.add(int(debit.row_id))
        used.add(int(credit.row_id))
    return pairs


def resolve_counterparties(
    connection: sqlite3.Connection,
    config: AnalysisConfig,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    frame = pd.read_sql_query("SELECT * FROM transactions ORDER BY source_order, row_id", connection)
    known_accounts, holders = _known_entities(connection)
    updates: dict[int, Resolution] = {}

    for _, row in frame[frame["eligible_for_detection"] == 1].iterrows():
        updates[int(row["row_id"])] = _deterministic_resolution(row, known_accounts, holders)

    pairs = _pair_ledger_rows(frame, config)
    for debit_row_id, credit_row_id, pair_id, method, confidence in pairs:
        debit = frame.loc[frame["row_id"] == debit_row_id].iloc[0]
        credit = frame.loc[frame["row_id"] == credit_row_id].iloc[0]
        debit_resolution = updates[debit_row_id]
        credit_resolution = updates[credit_row_id]
        debit_has_stronger_resolution = (
            method == "amount_date_mirror_match"
            and debit_resolution.found_anything
            and debit_resolution.counterparty_resolution_method != "unresolved"
        )
        credit_has_stronger_resolution = (
            method == "amount_date_mirror_match"
            and credit_resolution.found_anything
            and credit_resolution.counterparty_resolution_method != "unresolved"
        )
        if not debit_has_stronger_resolution:
            debit_resolution.counterparty_account = str(credit["account_id"])
            debit_resolution.counterparty_name_raw = (
                debit_resolution.counterparty_name_raw or str(credit["account_holder"] or "")
            )
            debit_resolution.counterparty_ifsc = (
                debit_resolution.counterparty_ifsc or str(credit["ifsc_code"] or "")
            )
            debit_resolution.counterparty_resolution_method = method
            debit_resolution.counterparty_resolution_confidence = confidence
        if not credit_has_stronger_resolution:
            credit_resolution.counterparty_account = str(debit["account_id"])
            credit_resolution.counterparty_name_raw = (
                credit_resolution.counterparty_name_raw or str(debit["account_holder"] or "")
            )
            credit_resolution.counterparty_ifsc = (
                credit_resolution.counterparty_ifsc or str(debit["ifsc_code"] or "")
            )
            credit_resolution.counterparty_resolution_method = method
            credit_resolution.counterparty_resolution_confidence = confidence
        connection.execute(
            "UPDATE transactions SET ledger_pair_id = ? WHERE row_id IN (?, ?)",
            (pair_id, debit_row_id, credit_row_id),
        )

    llm_calls = 0

    connection.executemany(
        """
        UPDATE transactions
        SET counterparty_account = ?, counterparty_ifsc = ?,
            counterparty_name_raw = ?, counterparty_resolution_method = ?,
            counterparty_resolution_confidence = ?
        WHERE row_id = ?
        """,
        [
            (
                result.counterparty_account or None,
                result.counterparty_ifsc or None,
                result.counterparty_name_raw or None,
                result.counterparty_resolution_method,
                float(result.counterparty_resolution_confidence),
                row_id,
            )
            for row_id, result in updates.items()
        ],
    )

    resolved_rows = pd.read_sql_query(
        """
        SELECT txn_id, counterparty_account, counterparty_name_raw
        FROM transactions
        WHERE eligible_for_detection = 1
          AND COALESCE(counterparty_account, '') != ''
        """,
        connection,
    )
    name_groups: dict[str, dict[str, object]] = {}
    for row in resolved_rows.itertuples(index=False):
        normalized = normalize_name(row.counterparty_name_raw)
        if not normalized:
            continue
        group = name_groups.setdefault(
            normalized,
            {"names": set(), "accounts": set(), "txn_ids": []},
        )
        group["names"].add(str(row.counterparty_name_raw))
        group["accounts"].add(str(row.counterparty_account))
        group["txn_ids"].append(str(row.txn_id))

    possible_same_owner: list[dict[str, object]] = []
    connection.execute("DELETE FROM possible_same_owner")
    for normalized, group in name_groups.items():
        accounts = sorted(group["accounts"])
        if len(accounts) < 2:
            continue
        record = {
            "normalized_name": normalized,
            "counterparty_names_raw": sorted(group["names"]),
            "account_numbers": accounts,
            "txn_ids": list(dict.fromkeys(group["txn_ids"])),
            "explanation": "Different account identifiers share the same extracted holder name; review as possibly the same owner without merging them.",
        }
        possible_same_owner.append(record)
        connection.execute(
            """
            INSERT INTO possible_same_owner(
                normalized_name, counterparty_name_raw, account_numbers_json, transaction_ids_json
            ) VALUES (?, ?, ?, ?)
            """,
            (
                normalized,
                " | ".join(record["counterparty_names_raw"]),
                json.dumps(accounts),
                json.dumps(record["txn_ids"]),
            ),
        )

    eligible_count = int((frame["eligible_for_detection"] == 1).sum())
    resolved_count = len(resolved_rows)
    method_counts = {
        row[0]: int(row[1])
        for row in connection.execute(
            """
            SELECT COALESCE(counterparty_resolution_method, 'unresolved'), COUNT(*)
            FROM transactions WHERE eligible_for_detection = 1
            GROUP BY COALESCE(counterparty_resolution_method, 'unresolved')
            """
        ).fetchall()
    }
    connection.commit()
    metrics: dict[str, object] = {
        "eligible_rows": eligible_count,
        "resolved_counterparty_rows": resolved_count,
        "resolution_rate_percent": (100.0 * resolved_count / eligible_count) if eligible_count else 0.0,
        "method_counts": method_counts,
        "ledger_pair_count": len(pairs),
        "llm_call_count": llm_calls,
        "llm_available": False,
        "cache_entry_count": connection.execute("SELECT COUNT(*) FROM counterparty_cache").fetchone()[0],
    }
    return metrics, possible_same_owner
