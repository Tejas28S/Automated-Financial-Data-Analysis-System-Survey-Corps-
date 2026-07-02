"""Pattern 19: first-contact large transfer."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
import re
import sqlite3

import networkx as nx

from ..config import AnalysisConfig
from .common import edge_confidence_details, make_finding


def _counterparty_structure_matches(left: str, right: str) -> bool:
    left_digits = re.sub(r"\D", "", str(left or ""))
    right_digits = re.sub(r"\D", "", str(right or ""))
    if len(left_digits) < 6 or len(right_digits) < 6:
        return False
    return abs(len(left_digits) - len(right_digits)) <= 2 and left_digits[:2] == right_digits[:2]


def _similar_amount(left: float, right: float, tolerance: float) -> bool:
    basis = max(abs(left), abs(right), 1.0)
    return abs(left - right) / basis <= tolerance


def _day_of_month_matches(left: date, right: date, tolerance_days: int) -> bool:
    return abs(left.day - right.day) <= tolerance_days


def _candidate_group_key(source: str, target: str, graph: nx.MultiDiGraph) -> tuple[str, str, str]:
    source_observed = bool(graph.nodes.get(source, {}).get("observed_account"))
    target_observed = bool(graph.nodes.get(target, {}).get("observed_account"))
    if target_observed and not source_observed:
        return target, "incoming", source
    if source_observed and not target_observed:
        return source, "outgoing", target
    return target, "incoming", source


def _recurring_candidate_ids(candidates: list[dict], config: AnalysisConfig) -> set[int]:
    by_account_direction: defaultdict[tuple[str, str], list[dict]] = defaultdict(list)
    for candidate in candidates:
        by_account_direction[(candidate["focal_account"], candidate["direction"])].append(candidate)

    recurring_ids: set[int] = set()
    for group in by_account_direction.values():
        ordered = sorted(group, key=lambda item: (item["date"], item["txn_id"]))
        used: set[int] = set()
        for start in ordered:
            start_id = start["candidate_id"]
            if start_id in used:
                continue
            cluster = [start]
            previous = start
            for candidate in ordered:
                candidate_id = candidate["candidate_id"]
                if candidate_id == start_id or candidate_id in used:
                    continue
                if candidate["date"] <= previous["date"]:
                    continue
                gap = (candidate["date"] - previous["date"]).days
                if not (
                    config.first_contact_recurring_min_gap_days
                    <= gap
                    <= config.first_contact_recurring_max_gap_days
                ):
                    continue
                if not _similar_amount(
                    candidate["amount"],
                    start["amount"],
                    config.first_contact_recurring_amount_tolerance,
                ):
                    continue
                if not _counterparty_structure_matches(candidate["counterparty"], start["counterparty"]):
                    continue
                if not _day_of_month_matches(
                    candidate["date"],
                    start["date"],
                    config.first_contact_recurring_day_tolerance,
                ):
                    continue
                cluster.append(candidate)
                previous = candidate
            if len(cluster) >= config.first_contact_recurring_min_events:
                cluster_ids = {item["candidate_id"] for item in cluster}
                recurring_ids.update(cluster_ids)
                used.update(cluster_ids)
    return recurring_ids


def detect_first_contact_large_transfers(
    connection: sqlite3.Connection,
    baseline: dict,
    graph: nx.MultiDiGraph,
    config: AnalysisConfig,
) -> list:
    threshold = float(baseline.get("transaction_amounts", {}).get("p90") or 0.0)
    edge_rows = []
    for source, target, _, data in graph.edges(keys=True, data=True):
        if not data.get("date"):
            continue
        amount = float(data.get("amount", 0.0) or 0.0)
        if amount <= config.money_epsilon:
            continue
        edge_rows.append((date.fromisoformat(str(data["date"])), str(source), str(target), amount, data))
    edge_rows.sort(key=lambda item: (item[0], item[4].get("time", ""), item[4].get("txn_id", "")))

    seen_pairs: set[tuple[str, str]] = set()
    pair_history: defaultdict[tuple[str, str], int] = defaultdict(int)
    candidates = []
    for txn_date, source, target, amount, data in edge_rows:
        pair = tuple(sorted((source, target)))
        is_first_contact = pair_history[pair] == 0
        pair_history[pair] += 1
        if not is_first_contact or amount < threshold:
            continue
        pair_key = (source, target, str(data.get("txn_id", "")))
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)
        focal_account, direction, counterparty = _candidate_group_key(source, target, graph)
        candidates.append(
            {
                "candidate_id": len(candidates),
                "date": txn_date,
                "source": source,
                "target": target,
                "amount": amount,
                "data": data,
                "txn_id": str(data.get("txn_id", "")),
                "focal_account": focal_account,
                "direction": direction,
                "counterparty": counterparty,
            }
        )

    recurring_ids = _recurring_candidate_ids(candidates, config)
    findings = []
    for candidate in candidates:
        if candidate["candidate_id"] in recurring_ids:
            continue
        txn_date = candidate["date"]
        source = candidate["source"]
        target = candidate["target"]
        amount = candidate["amount"]
        data = candidate["data"]
        txn_ids = [str(txn_id) for txn_id in data.get("txn_ids", [data.get("txn_id", "")]) if str(txn_id)]
        findings.append(
            make_finding(
                connection,
                19,
                [source, target],
                txn_ids,
                f"{source} and {target} had no prior observed relationship before a large transfer of {amount:.2f} on {txn_date.isoformat()}.",
                {
                    "source": source,
                    "target": target,
                    "amount": amount,
                    "date": txn_date.isoformat(),
                    "prior_relationship_count": 0,
                    "recurring_series_suppressed": False,
                    "focal_account": candidate["focal_account"],
                    "direction": candidate["direction"],
                    "counterparty": candidate["counterparty"],
                    **edge_confidence_details([data]),
                    "runtime_thresholds": {
                        "large_transfer_min": threshold,
                        "recurring_min_events": config.first_contact_recurring_min_events,
                        "recurring_min_gap_days": config.first_contact_recurring_min_gap_days,
                        "recurring_max_gap_days": config.first_contact_recurring_max_gap_days,
                        "recurring_amount_tolerance": config.first_contact_recurring_amount_tolerance,
                        "recurring_day_tolerance": config.first_contact_recurring_day_tolerance,
                    },
                },
                config=config,
            )
        )
        if len(findings) >= config.maximum_findings_per_pattern:
            break
    return findings
