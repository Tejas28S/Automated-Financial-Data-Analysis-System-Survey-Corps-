# Copied from: analysis phase cidecode hackathon/analysis phase cidecode hackathon/analysis_engine/pipeline.py
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

from .anomaly_investigator import detect_llm_investigated_anomalies
from .baseline import compute_baseline, finalize_counterparty_statistics
from .config import AnalysisConfig
from .counterparties import resolve_counterparties
from .database import (
    fetch_transactions,
    initialize_database,
    load_transactions,
    persist_findings,
    persist_graph,
    persist_baseline,
)
from .llm_client import GroqKeyRotatingClient
from .llm_resolution import run_capped_counterparty_assist
from .detectors import (
    detect_accumulation_accounts,
    detect_auto_money_trails,
    detect_circular_flows,
    detect_credit_to_cash_chains,
    detect_cross_statement_flows,
    detect_dormant_reactivation,
    detect_duplicate_cross_check,
    detect_first_contact_large_transfers,
    detect_high_risk_hub_ranking,
    detect_high_throughput_pass_through,
    detect_holding_accounts,
    detect_low_value_testing,
    detect_ml_ensemble_anomaly_leads,
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
from .models import AnalysisResult, Finding, PATTERN_CATALOG, assert_confidence_tier_consistency, pattern_key
from .narration import explain_finding

logger = logging.getLogger(__name__)


class AnalysisPipeline:
    """Runs the full analysis in the locked step order from ANALYSIS_INSTRUCTIONS."""

    def __init__(
        self,
        input_path: str | Path,
        output_dir: str | Path,
        config: AnalysisConfig | None = None,
        credit_txn_ids: list[str] | None = None,
        anomaly_account_ids: list[str] | None = None,
    ) -> None:
        self.input_path = Path(input_path).expanduser().resolve()
        self.output_dir = Path(output_dir).expanduser().resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.config = config or AnalysisConfig()
        self.credit_txn_ids = credit_txn_ids or []
        self.anomaly_account_ids = anomaly_account_ids or []
        self.db_path = self.output_dir / "analysis.db"
        self.connection: sqlite3.Connection | None = None
        self._timings: dict[str, float] = {}

    def _time(self, label: str) -> float:
        """Record a timing checkpoint."""
        now = time.monotonic()
        self._timings[label] = now
        return now

    def _narrate_findings(
        self,
        findings: dict[str, list[Finding]],
        llm_client: GroqKeyRotatingClient | None,
    ) -> dict[str, Any]:
        reserved_for_pattern22 = self.config.llm_role_b_max_manual_accounts
        narration_budget = 0
        if llm_client is not None and llm_client.available:
            remaining = llm_client.remaining_call_budget()
            if remaining is None:
                narration_budget = 10**9
            else:
                narration_budget = max(0, remaining - reserved_for_pattern22)
        summary = {
            "template": 0,
            "groq": 0,
            "llm_status": llm_client.status_label() if llm_client else "disabled_by_config",
            "llm_budget_summary": {
                "run_max_calls": self.config.llm_run_max_calls,
                "reserved_for_pattern22": reserved_for_pattern22,
                "spent_on_narration": 0,
                "remaining_before_narration": llm_client.remaining_call_budget() if llm_client else 0,
                "narration_budget_after_reservation": narration_budget if narration_budget != 10**9 else "unlimited",
            },
        }
        for pattern_findings in findings.values():
            for finding in pattern_findings:
                if finding.pattern_id in {22, 23}:
                    finding.details.setdefault("explanation_source", "template")
                    finding.narration = finding.narration or finding.explanation
                    finding.narration_validation = finding.narration_validation or "verified"
                    summary["template"] += 1
                    continue
                if (
                    llm_client is None
                    or not llm_client.available
                    or llm_client.status_label() != "active"
                    or narration_budget <= 0
                ):
                    finding.details.setdefault("explanation_source", "template")
                    finding.narration = finding.narration or finding.explanation
                    finding.narration_validation = finding.narration_validation or "verified"
                    summary["template"] += 1
                    continue
                before_calls = llm_client.call_count
                explain_finding(finding, llm_client)
                spent = max(0, llm_client.call_count - before_calls)
                if spent:
                    narration_budget -= spent
                    summary["llm_budget_summary"]["spent_on_narration"] += spent
                source = str(finding.details.get("explanation_source", "template"))
                summary["groq" if source == "groq" else "template"] += 1
        summary["llm_status"] = llm_client.status_label() if llm_client else "disabled_by_config"
        summary["llm_budget_summary"]["remaining_after_narration"] = (
            llm_client.remaining_call_budget() if llm_client else 0
        )
        return summary

    def run(self) -> AnalysisResult:
        """Execute all steps in the prescribed order and return the result."""
        run_start = time.monotonic()
        self._time("start")
        llm_client = GroqKeyRotatingClient(self.config) if self.config.enable_llm_fallback else None

        # ── Step 1: Merge + Label ──
        logger.info("Step 1: Loading and normalizing inputs from %s", self.input_path)
        normalized = load_inputs(self.input_path)
        reconciliation_status = normalized.input_contract.get("count_reconciliation_status")
        if reconciliation_status == "mismatch":
            raise ValueError(
                "Extraction count reconciliation mismatch: "
                f"{normalized.input_contract.get('count_reconciliation_detail', '')}"
            )
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

        # ── Step 3b: Balance data-quality summary only ──
        balance_mismatch_count = int(
            normalized.input_contract.get("balance_mismatch_excluded_count", 0) or 0
        )
        balance_summary = {
            "source": "extraction_flagged_rows",
            "balance_mismatch_excluded_count": balance_mismatch_count,
            "summary_line": (
                f"{balance_mismatch_count} transactions were excluded from analysis due to balance "
                "inconsistencies identified during extraction."
                if balance_mismatch_count
                else "No balance inconsistencies detected in this dataset."
            ),
        }
        self._time("balance_quality_summary")

        # ── Step 4: Counterparty resolution ──
        logger.info("Step 4: Counterparty resolution")
        counterparty_metrics, possible_same_owner = resolve_counterparties(
            self.connection, self.config
        )
        role_a_summary = run_capped_counterparty_assist(
            self.config,
            llm_client,
            connection=self.connection,
        ).to_dict()
        counterparty_metrics["llm_assist_summary"] = role_a_summary
        counterparty_metrics["llm_status"] = llm_client.status_label() if llm_client else "disabled_by_config"
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
        logger.info("Step 5a: Running single-account detectors")
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

        # Pattern 5: Structuring
        p5_structuring = detect_structuring(self.connection, baseline, self.config)
        findings[pattern_key(5)] = p5_structuring
        logger.info("  Pattern 5 (structuring): %d findings", len(p5_structuring))

        # Pattern 9: Large credit followed by ATM/cash withdrawal
        p9_cash = detect_credit_to_cash_chains(self.connection, baseline, self.config)
        findings[pattern_key(9)] = p9_cash
        logger.info("  Pattern 9 (credit-to-cash): %d findings", len(p9_cash))

        # Pattern 14: Reversal clusters
        p14_reversal_clusters = detect_reversal_clusters(self.connection, baseline, self.config)
        findings[pattern_key(14)] = p14_reversal_clusters
        logger.info("  Pattern 14 (reversal clusters): %d findings", len(p14_reversal_clusters))

        # Pattern 15: Repeated round-value debits
        p15_round = detect_round_value_debits(self.connection, baseline, self.config)
        findings[pattern_key(15)] = p15_round
        logger.info("  Pattern 15 (round-value debits): %d findings", len(p15_round))

        self._time("single_account_detectors")

        # ── Step 5b: Graph construction (Pattern 6) ──
        logger.info("Step 5b: Building money-flow graph (Pattern 6)")
        graph = build_money_flow_graph(self.connection, self.config)
        persist_graph(self.connection, graph)
        self._time("graph_construction")
        logger.info(
            "  Graph: %d nodes, %d edges",
            graph.number_of_nodes(),
            graph.number_of_edges(),
        )
        edge_source_counts: dict[str, int] = {}
        for _, _, data in graph.edges(data=True):
            edge_source = str(data.get("edge_source", "deterministic") or "deterministic")
            edge_source_counts[edge_source] = edge_source_counts.get(edge_source, 0) + 1

        # ── Step 5c: Graph detectors ──
        findings[pattern_key(6)] = [
            Finding(
                pattern_id=6,
                pattern_name=PATTERN_CATALOG[6],
                accounts=[str(node) for node, data in graph.nodes(data=True) if data.get("observed_account")],
                txn_ids=[
                    txn_id
                    for _, _, data in graph.edges(data=True)
                    for txn_id in data.get("txn_ids", [data.get("txn_id", "")])
                    if txn_id
                ][:500],
                explanation=(
                    f"Money-flow graph constructed with {graph.number_of_nodes()} nodes "
                    f"and {graph.number_of_edges()} directed transaction edges."
                ),
                confidence_tier="high",
                detection_method="deterministic",
                details={
                    "node_count": graph.number_of_nodes(),
                    "edge_count": graph.number_of_edges(),
                    "edge_source_counts": {
                        "deterministic": edge_source_counts.get("deterministic", 0),
                        "llm_inferred": edge_source_counts.get("llm_inferred", 0),
                    },
                    "explanation_source": "template",
                    "source_documents": [],
                },
            )
        ]

        logger.info("Step 5c: Running graph detectors")

        # Pattern 3: Pass-through / routing accounts (merged transit + high-throughput)
        p3_routing = detect_transit_accounts(self.connection, baseline, graph, self.config)
        p3_routing.extend(
            detect_high_throughput_pass_through(self.connection, baseline, graph, self.config)
        )
        findings[pattern_key(3)] = p3_routing
        logger.info("  Pattern 3 (pass-through/routing): %d findings", len(p3_routing))

        # Pattern 4: Fund pooling
        p4_pooling = detect_accumulation_accounts(
            self.connection, baseline, graph, self.config
        )
        findings[pattern_key(4)] = p4_pooling
        logger.info("  Pattern 4 (fund pooling): %d findings", len(p4_pooling))

        # Pattern 7: Circular flows
        p7_circular = detect_circular_flows(self.connection, baseline, graph, self.config)
        findings[pattern_key(7)] = p7_circular
        logger.info("  Pattern 7 (circular): %d findings", len(p7_circular))

        # Pattern 12: High-risk hub ranking
        p12_hub = detect_high_risk_hub_ranking(
            self.connection, baseline, graph, self.config
        )
        findings[pattern_key(12)] = p12_hub
        logger.info("  Pattern 12 (hub ranking): %d findings", len(p12_hub))

        # Pattern 13: Low-value reciprocal testing
        p13_low = detect_low_value_testing(self.connection, baseline, graph, self.config)
        findings[pattern_key(13)] = p13_low
        logger.info("  Pattern 13 (low-value testing): %d findings", len(p13_low))

        # Pattern 17: Round trips
        p17_round_trips = detect_round_trips(self.connection, baseline, graph, self.config)
        findings[pattern_key(17)] = p17_round_trips
        logger.info("  Pattern 17 (round trips): %d findings", len(p17_round_trips))

        # Pattern 19: First-contact large transfer
        p19_first_contact = detect_first_contact_large_transfers(
            self.connection, baseline, graph, self.config
        )
        findings[pattern_key(19)] = p19_first_contact
        logger.info("  Pattern 19 (first-contact large transfer): %d findings", len(p19_first_contact))

        self._time("graph_detectors")

        # ── Step 5d: Cross-account detectors ──
        logger.info("Step 5d: Running cross-account detectors")

        # Pattern 10: Cross-statement/internal matched links
        p10_cross = detect_cross_statement_flows(self.connection, baseline, self.config)
        findings[pattern_key(10)] = p10_cross
        logger.info("  Pattern 10 (matched links): %d findings", len(p10_cross))

        # Pattern 11: Balance parking
        p11_holding = detect_holding_accounts(self.connection, baseline, self.config)
        findings[pattern_key(11)] = p11_holding
        logger.info("  Pattern 11 (balance parking): %d findings", len(p11_holding))

        # Pattern 16: Shared UPI identifiers
        p16_shared = detect_shared_upi_identifiers(self.connection, baseline, self.config)
        findings[pattern_key(16)] = p16_shared
        logger.info("  Pattern 16 (shared UPI): %d findings", len(p16_shared))

        # Pattern 18: Dormant reactivation
        p18_dormant = detect_dormant_reactivation(self.connection, baseline, self.config)
        findings[pattern_key(18)] = p18_dormant
        logger.info("  Pattern 18 (dormant reactivation): %d findings", len(p18_dormant))

        self._time("cross_account_detectors")

        # ── Step 5e: Money trail auto-trigger + manual requested credits ──
        money_trail_context_pattern_ids = {5, 7, 10, 16, 17, 18}
        strong_accounts = {
            account
            for key, items in findings.items()
            if int(key.split("_", 1)[0]) in money_trail_context_pattern_ids
            for finding in items
            if finding.evidence_strength == "strong"
            for account in finding.accounts
        }
        suspicious_txn_ids = {
            txn_id
            for key, items in findings.items()
            if int(key.split("_", 1)[0]) in money_trail_context_pattern_ids
            for finding in items
            for txn_id in finding.txn_ids
        }
        p8_trails = detect_auto_money_trails(
            self.connection,
            baseline,
            self.config,
            strong_accounts,
            self.credit_txn_ids,
            suspicious_txn_ids,
        )
        findings[pattern_key(8)] = p8_trails
        logger.info("  Pattern 8 (money trail): %d trace/status finding(s)", len(p8_trails))
        self._time("money_trail")

        # ── Step 5f: Meta-pattern (Pattern 21) — runs after all other detectors ──
        logger.info("Step 5f: Top suspicious account ranking (Pattern 21)")
        p21 = detect_top_suspicious_ranking(
            self.connection, findings, self.config
        )
        findings[pattern_key(21)] = p21
        logger.info("  Pattern 21 (top suspicious ranking): %d findings", len(p21))
        self._time("meta_pattern")

        # ── Step 5g: Pattern 22, after deterministic scored patterns ──
        logger.info("Step 5g: Pattern 22 LLM-investigated anomalies")
        p22 = detect_llm_investigated_anomalies(
            self.connection,
            baseline,
            findings,
            self.config,
            llm_client,
            self.anomaly_account_ids,
        )
        findings[pattern_key(22)] = p22
        logger.info("  Pattern 22 (LLM anomaly leads): %d finding/status rows", len(p22))

        logger.info("Step 5h: Pattern 23 ML ensemble anomaly leads")
        p23 = detect_ml_ensemble_anomaly_leads(
            self.connection,
            baseline,
            graph,
            self.config,
            findings,
        )
        findings[pattern_key(23)] = p23
        logger.info("  Pattern 23 (ML ensemble leads): %d finding(s)", len(p23))

        # ── Step 5i: Role C narration pass ──
        narration_summary = self._narrate_findings(findings, llm_client)
        self._time("finding_narration")
        logger.info("  Narration sources: %s", narration_summary)

        # ── Step 6: Suspicion scoring ──
        logger.info("Step 6: Scoring and ranking suspicious accounts")
        from .case_structure import build_case_structure
        from .scoring import score_accounts, weak_signal_accounts

        scored = score_accounts(findings)
        suspicious_accounts = [sa.to_dict() for sa in scored]
        weak_accounts = weak_signal_accounts(findings)
        case_structure, cluster_summaries, case_summary, network_graph_for_display = build_case_structure(
            graph,
            findings,
            suspicious_accounts,
            llm_client,
        )
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
                "source_file": str(row.get("source_file", "")),
                "source_row_number": int(row.get("source_row_number", 0) or 0),
                "flag_reason": str(row.get("flag_reason", "")),
            }
            if "balance_mismatch" in reason:
                excluded_rows["balance_mismatch"].append(entry)
            elif "duplicate" in reason:
                excluded_rows["duplicate"].append(entry)
            else:
                excluded_rows["other"].append(entry)

        run_end = time.monotonic()
        analysis_timestamp = datetime.now(timezone.utc).isoformat()
        llm_status = llm_client.final_status_label() if llm_client else "disabled_no_key"
        run_metadata = {
            "run_id": self.output_dir.name,
            "analysis_timestamp": analysis_timestamp,
            "input_extraction_run_id": self.input_path.name,
            "input_path": str(self.input_path),
            "output_dir": str(self.output_dir),
            "db_path": str(self.db_path),
            "run_timestamp": analysis_timestamp,
            "elapsed_seconds": round(run_end - run_start, 2),
            "input_manifest": normalized.input_manifest,
            "input_contract_warnings": normalized.input_contract.get("warnings", []),
            "narration_summary": narration_summary,
            "llm_budget_summary": narration_summary.get("llm_budget_summary", {}),
            "llm_status": llm_status,
            "llm_final_status": llm_status,
            "llm_call_count": llm_client.call_count if llm_client else 0,
            "llm_key_summary": llm_client.key_load_summary if llm_client else {"loaded_key_count": 0, "loaded_key_labels": []},
            "llm_rotation_events": llm_client.rotation_events if llm_client else [],
            "config": {
                k: v
                for k, v in self.config.__dict__.items()
                if not k.startswith("_")
            },
        }

        result = AnalysisResult(
            run_metadata=run_metadata,
            input_contract=normalized.input_contract,
            baseline_summary=baseline,
            counterparty_resolution=counterparty_metrics,
            suspicious_accounts=suspicious_accounts,
            findings_by_pattern=findings,
            excluded_rows=excluded_rows,
            possible_same_owner=possible_same_owner,
            graph=graph,
            balance_validation=balance_summary,
            weak_signal_accounts=weak_accounts,
            case_structure=case_structure,
            cluster_summaries=cluster_summaries,
            case_summary=case_summary,
            network_graph_for_display=network_graph_for_display,
        )
        assert_confidence_tier_consistency(findings)
        persist_findings(self.connection, findings)
        # Attach the DB connection so the report builder can query details
        result._connection = self.connection
        self._time("output_assembly")

        # Persist JSON output
        from .output import build_output, write_analysis_summary

        build_output(result, self.output_dir)
        write_analysis_summary(result, self.output_dir)
        self._time("json_persist")

        try:
            from .graph_generator import generate_investigation_graphs

            generate_investigation_graphs(result, self.output_dir)
        except Exception:
            logger.exception("Investigation graph generation failed after analysis output persistence")
        self._time("graph_png_generation")

        logger.info(
            "Pipeline complete in %.1f seconds. Output at %s",
            run_end - run_start,
            self.output_dir,
        )
        return result
