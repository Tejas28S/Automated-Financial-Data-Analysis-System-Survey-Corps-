"""Prescribed orchestration of the analysis pipeline steps."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import networkx as nx

from .balance import validate_balances
from .baseline import compute_baseline, finalize_counterparty_statistics
from .config import AnalysisConfig
from .counterparties import GroqResolver, resolve_counterparties
from .database import (
    fetch_transactions,
    initialize_database,
    load_transactions,
    persist_baseline,
)
from .detectors import (
    detect_accumulation_accounts,
    detect_circular_flows,
    detect_credit_to_cash_chains,
    detect_cross_statement_flows,
    detect_duplicate_cross_check,
    detect_high_risk_hub_ranking,
    detect_high_throughput_pass_through,
    detect_holding_accounts,
    detect_low_value_testing,
    detect_matched_internal_flow_hub,
    detect_requested_money_trails,
    detect_reversal_clusters,
    detect_reversals,
    detect_round_trips,
    detect_round_value_debits,
    detect_shared_upi_identifiers,
    detect_structuring,
    detect_top_suspicious_ranking,
    detect_transit_accounts,
)
from .graph import build_money_flow_graph
from .ingest import load_inputs
from .models import AnalysisResult, Finding, PATTERN_CATALOG, pattern_key

logger = logging.getLogger(__name__)


class AnalysisPipeline:
    """Runs the full analysis in the locked step order from ANALYSIS_INSTRUCTIONS."""

    def __init__(
        self,
        input_path: str | Path,
        output_dir: str | Path,
        config: AnalysisConfig | None = None,
        credit_txn_ids: list[str] | None = None,
    ) -> None:
        self.input_path = Path(input_path).expanduser().resolve()
        self.output_dir = Path(output_dir).expanduser().resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.config = config or AnalysisConfig()
        self.credit_txn_ids = credit_txn_ids or []
        self.db_path = self.output_dir / "analysis.db"
        self.connection: sqlite3.Connection | None = None
        self._timings: dict[str, float] = {}

    def _time(self, label: str) -> float:
        """Record a timing checkpoint."""
        now = time.monotonic()
        self._timings[label] = now
        return now

    def run(self) -> AnalysisResult:
        """Execute all steps in the prescribed order and return the result."""
        run_start = time.monotonic()
        self._time("start")

        # ── Step 1: Merge + Label ──
        logger.info("Step 1: Loading and normalizing inputs from %s", self.input_path)
        normalized = load_inputs(self.input_path)
        self._time("ingest")
        logger.info(
            "  Loaded %d rows from %d source file(s)",
            len(normalized.transactions),
            len(normalized.input_manifest),
        )

        # ── Step 2: Persist to SQLite ──
        logger.info("Step 2: Persisting to SQLite at %s", self.db_path)
        self.connection = initialize_database(self.db_path, self.config)
        load_transactions(self.connection, normalized.transactions)
        self._time("sqlite_load")
        logger.info("  SQLite loaded: %d rows", len(normalized.transactions))

        # ── Step 3: Initial baseline statistics ──
        logger.info("Step 3: Computing initial baseline statistics")
        baseline = compute_baseline(self.connection, self.config)
        self._time("baseline_initial")
        logger.info(
            "  Eligible: %d, Excluded: %d, Accounts: %d",
            baseline["row_counts"]["eligible"],
            baseline["row_counts"]["excluded"],
            baseline["account_count"],
        )

        # ── Step 3b: Balance validation gate (Pattern 3) ──
        logger.info("Step 3b: Balance consistency validation (Pattern 3)")
        balance_findings, balance_summary = validate_balances(
            self.connection, self.config
        )
        self._time("balance_validation")
        logger.info(
            "  Balance check: %d newly excluded, status: %s",
            balance_summary["newly_excluded_count"],
            json.dumps(balance_summary["status_counts"]),
        )

        # ── Step 3c: Recompute baseline after balance exclusions ──
        if balance_summary["newly_excluded_count"] > 0:
            logger.info("Step 3c: Recomputing baseline after balance exclusions")
            baseline = compute_baseline(self.connection, self.config)
            self._time("baseline_recompute")

        # ── Step 4: Counterparty resolution ──
        logger.info("Step 4: Counterparty resolution")
        llm_resolver = GroqResolver.from_environment(self.config)
        counterparty_metrics, possible_same_owner = resolve_counterparties(
            self.connection, self.config, llm_resolver
        )
        self._time("counterparty_resolution")
        logger.info(
            "  Resolution rate: %.1f%% (%d/%d), LLM calls: %d",
            counterparty_metrics["resolution_rate_percent"],
            counterparty_metrics["resolved_counterparty_rows"],
            counterparty_metrics["eligible_rows"],
            counterparty_metrics["llm_call_count"],
        )

        # ── Step 4b: Finalize counterparty statistics ──
        baseline = finalize_counterparty_statistics(
            self.connection, baseline, self.config
        )
        self._time("counterparty_stats")

        # ── Step 5a: Single-account detectors ──
        logger.info("Step 5a: Running single-account detectors (Patterns 1, 2, 7, 14, 16, 18)")
        findings: dict[str, list[Finding]] = {
            pattern_key(pid): [] for pid in PATTERN_CATALOG
        }

        # Pattern 1: Duplicate cross-check
        p1 = detect_duplicate_cross_check(self.connection, baseline, self.config)
        findings[pattern_key(1)] = p1
        logger.info("  Pattern 1 (duplicates): %d findings", len(p1))

        # Pattern 2: Reversals
        p2 = detect_reversals(self.connection, baseline, self.config)
        findings[pattern_key(2)] = p2
        logger.info("  Pattern 2 (reversals): %d findings", len(p2))

        # Pattern 3: Balance (already done in step 3b)
        findings[pattern_key(3)] = balance_findings
        logger.info("  Pattern 3 (balance): %d findings", len(balance_findings))

        # Pattern 7: Structuring
        p7 = detect_structuring(self.connection, baseline, self.config)
        findings[pattern_key(7)] = p7
        logger.info("  Pattern 7 (structuring): %d findings", len(p7))

        # Pattern 14: Large credit followed by ATM/cash withdrawal
        p14 = detect_credit_to_cash_chains(self.connection, baseline, self.config)
        findings[pattern_key(14)] = p14
        logger.info("  Pattern 14 (credit-to-cash): %d findings", len(p14))

        # Pattern 16: Repeated round-value debits
        p16 = detect_round_value_debits(self.connection, baseline, self.config)
        findings[pattern_key(16)] = p16
        logger.info("  Pattern 16 (round-value debits): %d findings", len(p16))

        # Pattern 18: Reversal clusters
        p18 = detect_reversal_clusters(self.connection, baseline, self.config)
        findings[pattern_key(18)] = p18
        logger.info("  Pattern 18 (reversal clusters): %d findings", len(p18))

        self._time("single_account_detectors")

        # ── Step 5b: Graph construction (Pattern 8) ──
        logger.info("Step 5b: Building money-flow graph (Pattern 8)")
        graph = build_money_flow_graph(self.connection, self.config)
        self._time("graph_construction")
        logger.info(
            "  Graph: %d nodes, %d edges",
            graph.number_of_nodes(),
            graph.number_of_edges(),
        )

        # ── Step 5c: Graph detectors ──
        logger.info("Step 5c: Running graph detectors (Patterns 4, 5, 6, 9, 11, 19, 20)")

        # Pattern 4: Round trips
        p4 = detect_round_trips(self.connection, baseline, graph, self.config)
        findings[pattern_key(4)] = p4
        logger.info("  Pattern 4 (round trips): %d findings", len(p4))

        # Pattern 5: Transit accounts
        p5 = detect_transit_accounts(self.connection, baseline, graph, self.config)
        findings[pattern_key(5)] = p5
        logger.info("  Pattern 5 (transit): %d findings", len(p5))

        # Pattern 6: Accumulation
        p6 = detect_accumulation_accounts(
            self.connection, baseline, graph, self.config
        )
        findings[pattern_key(6)] = p6
        logger.info("  Pattern 6 (accumulation): %d findings", len(p6))

        # Pattern 9: Circular flows
        p9 = detect_circular_flows(self.connection, baseline, graph, self.config)
        findings[pattern_key(9)] = p9
        logger.info("  Pattern 9 (circular): %d findings", len(p9))

        # Pattern 11: High-throughput pass-through
        p11 = detect_high_throughput_pass_through(
            self.connection, baseline, graph, self.config
        )
        findings[pattern_key(11)] = p11
        logger.info("  Pattern 11 (high-throughput pass-through): %d findings", len(p11))

        # Pattern 19: Low-value reciprocal testing
        p19 = detect_low_value_testing(self.connection, baseline, graph, self.config)
        findings[pattern_key(19)] = p19
        logger.info("  Pattern 19 (low-value testing): %d findings", len(p19))

        # Pattern 20: High-risk hub ranking
        p20 = detect_high_risk_hub_ranking(
            self.connection, baseline, graph, self.config
        )
        findings[pattern_key(20)] = p20
        logger.info("  Pattern 20 (hub ranking): %d findings", len(p20))

        self._time("graph_detectors")

        # ── Step 5d: Cross-account detectors (Patterns 12, 13, 15, 17) ──
        logger.info("Step 5d: Running cross-account detectors (Patterns 12, 13, 15, 17)")

        # Pattern 12: Matched internal flow hub
        p12 = detect_matched_internal_flow_hub(
            self.connection, baseline, self.config
        )
        findings[pattern_key(12)] = p12
        logger.info("  Pattern 12 (internal flow hub): %d findings", len(p12))

        # Pattern 13: Cross-statement money flow links
        p13 = detect_cross_statement_flows(self.connection, baseline, self.config)
        findings[pattern_key(13)] = p13
        logger.info("  Pattern 13 (cross-statement flows): %d findings", len(p13))

        # Pattern 15: Holding accounts
        p15 = detect_holding_accounts(self.connection, baseline, self.config)
        findings[pattern_key(15)] = p15
        logger.info("  Pattern 15 (holding accounts): %d findings", len(p15))

        # Pattern 17: Shared UPI identifiers
        p17 = detect_shared_upi_identifiers(self.connection, baseline, self.config)
        findings[pattern_key(17)] = p17
        logger.info("  Pattern 17 (shared UPI): %d findings", len(p17))

        self._time("cross_account_detectors")

        # ── Step 5e: Money trail (only for explicitly requested credits) ──
        if self.credit_txn_ids:
            logger.info(
                "Step 5e: Money trail tracing for %d credit(s)", len(self.credit_txn_ids)
            )
            p10 = detect_requested_money_trails(
                self.connection, baseline, self.config, self.credit_txn_ids
            )
            findings[pattern_key(10)] = p10
            logger.info("  Pattern 10 (money trail): %d trace(s)", len(p10))
        self._time("money_trail")

        # ── Step 5f: Meta-pattern (Pattern 21) — runs after all other detectors ──
        logger.info("Step 5f: Top suspicious account ranking (Pattern 21)")
        p21 = detect_top_suspicious_ranking(
            self.connection, findings, self.config
        )
        findings[pattern_key(21)] = p21
        logger.info("  Pattern 21 (top suspicious ranking): %d findings", len(p21))
        self._time("meta_pattern")

        # ── Step 6: Suspicion scoring ──
        logger.info("Step 6: Scoring and ranking suspicious accounts")
        from .scoring import score_accounts

        scored = score_accounts(findings)
        suspicious_accounts = [sa.to_dict() for sa in scored]
        self._time("scoring")
        logger.info("  Ranked %d accounts with findings", len(suspicious_accounts))

        # ── Step 7: Build excluded rows report ──
        logger.info("Step 7: Building structured output")
        excluded_frame = fetch_transactions(
            self.connection, "eligible_for_detection = 0"
        )
        excluded_rows: dict[str, list[dict[str, Any]]] = {
            "balance_mismatch": [],
            "duplicate": [],
            "other": [],
        }
        for _, row in excluded_frame.iterrows():
            reason = str(row.get("exclusion_reason", ""))
            entry = {
                "txn_id": str(row["txn_id"]),
                "account_id": str(row["account_id"]),
                "date": str(row.get("date", "")),
                "amount": max(
                    float(row.get("debit_amount", 0)),
                    float(row.get("credit_amount", 0)),
                ),
                "exclusion_reason": reason,
                "source_document": str(row.get("doc_id", "")),
                "source_page": str(row.get("source_page", "")),
            }
            if "balance_mismatch" in reason:
                excluded_rows["balance_mismatch"].append(entry)
            elif "duplicate" in reason:
                excluded_rows["duplicate"].append(entry)
            else:
                excluded_rows["other"].append(entry)

        run_end = time.monotonic()
        run_metadata = {
            "input_path": str(self.input_path),
            "output_dir": str(self.output_dir),
            "db_path": str(self.db_path),
            "run_timestamp": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(run_end - run_start, 2),
            "input_manifest": normalized.input_manifest,
            "config": {
                k: v
                for k, v in self.config.__dict__.items()
                if not k.startswith("_")
            },
        }

        result = AnalysisResult(
            run_metadata=run_metadata,
            baseline_summary=baseline,
            counterparty_resolution=counterparty_metrics,
            suspicious_accounts=suspicious_accounts,
            findings_by_pattern=findings,
            excluded_rows=excluded_rows,
            possible_same_owner=possible_same_owner,
            graph=graph,
            balance_validation=balance_summary,
        )
        # Attach the DB connection so the report builder can query details
        result._connection = self.connection
        self._time("output_assembly")

        # Persist JSON output
        from .output import build_output

        build_output(result, self.output_dir)
        self._time("json_persist")

        logger.info(
            "Pipeline complete in %.1f seconds. Output at %s",
            run_end - run_start,
            self.output_dir,
        )
        return result
