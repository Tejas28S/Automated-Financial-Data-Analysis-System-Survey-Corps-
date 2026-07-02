# Copied from: analysis phase cidecode hackathon/analysis phase cidecode hackathon/analysis_engine/output.py
"""Structured output: JSON serialization and human-readable console summary.

Provides three public entry-points:

* ``build_output``  – persists a consolidated *analysis_results.json*.
* ``write_analysis_summary`` – persists a concise *analysis_summary.txt*.
* ``print_summary`` – prints a concise overview to *stdout* for quick review.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .models import AnalysisResult, Finding, json_default, pattern_key, PATTERN_CATALOG

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_output(result: AnalysisResult, output_dir: Path) -> Path:
    """Serialize *result* to ``<output_dir>/analysis_results.json``.

    The output guarantees every pattern key from :data:`PATTERN_CATALOG`
    appears in ``findings_by_pattern`` even when there are zero findings
    for that pattern.

    Returns
    -------
    Path
        Absolute path to the written JSON file.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    payload = _build_payload(result)

    dest = output_dir / "analysis_results.json"
    dest.write_text(
        json.dumps(payload, indent=2, default=json_default, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("Analysis results written to %s", dest)
    return dest


def write_analysis_summary(result: AnalysisResult, output_dir: Path) -> Path:
    """Write a concise human-readable ``analysis_summary.txt`` artifact."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    input_contract = result.input_contract or {}
    baseline = result.baseline_summary or {}
    row_counts = baseline.get("row_counts", {})
    counterparty = result.counterparty_resolution or {}
    balance_quality = result.balance_validation or {}
    case_structure = result.case_structure or {}
    clusters = case_structure.get("clusters", []) or []
    connected_clusters = [cluster for cluster in clusters if not cluster.get("is_isolated")]
    isolated_clusters = [cluster for cluster in clusters if cluster.get("is_isolated")]
    graph_summary = {
        "node_count": result.graph.number_of_nodes(),
        "edge_count": result.graph.number_of_edges(),
    }
    finding_counts = {
        pattern_key(pid): len(result.findings_by_pattern.get(pattern_key(pid), []))
        for pid in PATTERN_CATALOG
    }
    excluded_counts = {
        reason: len(rows) for reason, rows in (result.excluded_rows or {}).items()
    }

    lines = [
        "Analysis Summary",
        "================",
        "",
        f"Input path: {input_contract.get('input_path', result.run_metadata.get('input_path', ''))}",
        f"Output directory: {result.run_metadata.get('output_dir', '')}",
        f"Run timestamp: {result.run_metadata.get('run_timestamp', '')}",
        f"Elapsed seconds: {result.run_metadata.get('elapsed_seconds', '')}",
        "",
        "Input Contract",
        "--------------",
        f"Clean rows: {input_contract.get('clean_row_count', 0)}",
        f"Flagged rows: {input_contract.get('flagged_row_count', 0)}",
        f"Duplicate rows: {input_contract.get('duplicate_row_count', 0)}",
        f"Metadata clean rows: {input_contract.get('metadata_clean_rows', '')}",
        f"Summary-report clean rows: {input_contract.get('summary_report_clean_rows', '')}",
        f"Count reconciliation: {input_contract.get('count_reconciliation_status', '')}",
        f"Reconciliation detail: {input_contract.get('count_reconciliation_detail', '')}",
        "",
        "Analysis Scope",
        "--------------",
        f"Total stored rows: {row_counts.get('total', 0)}",
        f"Eligible detection rows: {row_counts.get('eligible', 0)}",
        f"Excluded rows: {row_counts.get('excluded', 0)}",
        f"Accounts: {baseline.get('account_count', 0)}",
        f"Graph nodes: {graph_summary['node_count']}",
        f"Graph edges: {graph_summary['edge_count']}",
        "",
        "Counterparty Resolution",
        "-----------------------",
        f"Eligible rows: {counterparty.get('eligible_rows', 0)}",
        f"Resolved counterparty rows: {counterparty.get('resolved_counterparty_rows', 0)}",
        f"Resolution rate percent: {counterparty.get('resolution_rate_percent', 0)}",
        f"Counterparty LLM calls made: {counterparty.get('llm_call_count', 0)}",
        f"Total LLM calls made: {result.run_metadata.get('llm_call_count', 0)}",
        f"LLM status: {result.run_metadata.get('llm_status', counterparty.get('llm_status', ''))}",
        "",
        "Case Reconstruction",
        "-------------------",
        result.case_summary or "No case reconstruction summary was generated.",
        f"Clusters: {case_structure.get('cluster_count', len(clusters))}",
        f"Connected clusters: {len(connected_clusters)}",
        f"Isolated account clusters: {len(isolated_clusters)}",
        "",
        "Balance Data Quality",
        "--------------------",
        balance_quality.get("summary_line", ""),
        "",
        "Finding Counts",
        "--------------",
    ]
    lines.extend(f"{key}: {count}" for key, count in finding_counts.items())
    lines.extend(
        [
            "",
            "Excluded Row Counts",
            "-------------------",
        ]
    )
    lines.extend(f"{reason}: {count}" for reason, count in excluded_counts.items())
    lines.extend(
        [
            "",
            "Top Suspicious Accounts",
            "-----------------------",
        ]
    )
    for account in result.suspicious_accounts[:10]:
        lines.append(
            "{account_id}: total_score={total_score}, distinct_patterns={distinct_pattern_count}, total_findings={total_findings}".format(
                account_id=account.get("account_id", ""),
                total_score=account.get("total_score", 0),
                distinct_pattern_count=account.get("distinct_pattern_count", 0),
                total_findings=account.get("total_findings", 0),
            )
        )

    dest = output_dir / "analysis_summary.txt"
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("Analysis summary written to %s", dest)
    return dest


def print_summary(result: AnalysisResult) -> None:
    """Print a human-readable summary of *result* to *stdout*."""

    _print_header("Baseline Summary")
    _print_baseline(result.baseline_summary)

    _print_header("Counterparty Resolution")
    _print_counterparty(result.counterparty_resolution)

    _print_header("Excluded Rows")
    _print_exclusions(result.excluded_rows)

    _print_header("Top 5 Ranked Accounts")
    _print_top_accounts(result.suspicious_accounts, n=5)

    _print_header("Findings by Pattern")
    _print_pattern_counts(result.findings_by_pattern)

    _print_header("Detailed Findings")
    _print_finding_details(result.findings_by_pattern)


# ---------------------------------------------------------------------------
# Payload construction
# ---------------------------------------------------------------------------


def _build_payload(result: AnalysisResult) -> dict[str, Any]:
    """Construct the full JSON-safe dictionary from *result*.

    Delegates to :meth:`AnalysisResult.to_dict` and then ensures all
    catalogue pattern keys are present.
    """
    payload = result.to_dict(include_graph=True)

    # Guarantee every catalogue pattern is present (even with []).
    findings = payload.setdefault("findings_by_pattern", {})
    for pid in PATTERN_CATALOG:
        key = pattern_key(pid)
        findings.setdefault(key, [])

    return payload


# ---------------------------------------------------------------------------
# Console printing helpers
# ---------------------------------------------------------------------------

_DIVIDER_WIDTH = 70


def _print_header(title: str) -> None:
    print()
    print("=" * _DIVIDER_WIDTH)
    print(f"  {title}")
    print("=" * _DIVIDER_WIDTH)


def _print_baseline(summary: dict[str, Any]) -> None:
    """Print key baseline statistics."""
    if not summary:
        print("  (no baseline summary available)")
        return
    # Row counts
    rc = summary.get("row_counts", {})
    print(f"  Total rows               : {rc.get('total', 'N/A')}")
    print(f"  Eligible rows            : {rc.get('eligible', 'N/A')}")
    print(f"  Excluded rows            : {rc.get('excluded', 'N/A')}")
    print(f"  Accounts                 : {summary.get('account_count', 'N/A')}")
    # Date range
    dr = summary.get("date_range", {})
    print(f"  Date range               : {dr.get('start', '?')} to {dr.get('end', '?')}")
    # Transaction amounts
    ta = summary.get("transaction_amounts", {})
    print(f"  Median amount            : {ta.get('median', 'N/A')}")
    print(f"  P95 amount               : {ta.get('p95', 'N/A')}")
    print(f"  P99 amount               : {ta.get('p99', 'N/A')}")
    # Key thresholds
    th = summary.get("thresholds", {})
    if th:
        print(f"  -- Runtime Thresholds --")
        for key, value in th.items():
            label = key.replace("_", " ").title()
            if isinstance(value, float):
                print(f"    {label:<40s}: {value:.4f}")
            else:
                print(f"    {label:<40s}: {value}")


def _print_counterparty(resolution: dict[str, Any]) -> None:
    """Print counterparty resolution metrics."""
    if not resolution:
        print("  (no counterparty resolution data)")
        return
    eligible = resolution.get("eligible_rows", "N/A")
    resolved = resolution.get("resolved_counterparty_rows", "N/A")
    rate = resolution.get("resolution_rate_percent", None)
    print(f"  Eligible rows            : {eligible}")
    print(f"  Resolved counterparties  : {resolved}")
    if rate is not None:
        print(f"  Resolution rate          : {rate:.1f}%")
    method = resolution.get("method_counts", {})
    if method:
        print(f"  By method:")
        for m, count in method.items():
            print(f"    {m:<25s}: {count}")
    llm_calls = resolution.get("llm_call_count", 0)
    print(f"  LLM calls made           : {llm_calls}")
    print(f"  Ledger pairs found       : {resolution.get('ledger_pair_count', 0)}")


def _print_exclusions(excluded: dict[str, list[dict[str, Any]]]) -> None:
    """Print per-reason exclusion counts."""
    if not excluded:
        print("  (no excluded rows)")
        return
    total = 0
    for reason, rows in excluded.items():
        count = len(rows)
        total += count
        label = reason.replace("_", " ").title()
        print(f"  {label:<35s}: {count:>6d} rows")
    print(f"  {'Total':<35s}: {total:>6d} rows")


def _print_top_accounts(
    accounts: list[dict[str, Any]],
    n: int = 5,
) -> None:
    """Print the top *n* ranked suspicious accounts with score breakdowns."""
    if not accounts:
        print("  (no ranked accounts)")
        return

    top = accounts[:n]
    for rank, acct in enumerate(top, start=1):
        acct_id = acct.get("account_id", "unknown")
        score = acct.get("distinct_pattern_count", 0)
        total_score = acct.get("total_score", 0)
        total = acct.get("total_findings", 0)
        print(f"  #{rank}  Account: {acct_id}  (score: {total_score}, distinct patterns: {score}, total findings: {total})")

        # Print individual pattern breakdown
        breakdown = acct.get("pattern_breakdown", {})
        if isinstance(breakdown, dict) and breakdown:
            for pkey, count in sorted(breakdown.items()):
                print(f"        {pkey:<50s}: {count}")


def _print_pattern_counts(
    findings: dict[str, list[Finding]],
) -> None:
    """Print the number of findings for each pattern, ordered by catalogue id."""
    total = 0
    for pid in sorted(PATTERN_CATALOG.keys()):
        key = pattern_key(pid)
        items = findings.get(key, [])
        count = len(items)
        total += count
        label = PATTERN_CATALOG[pid].replace("_", " ").title()
        print(f"  {pid:>2d}. {label:<50s}: {count:>5d}")
    print(f"  {'Total findings':<54s}: {total:>5d}")


def _print_finding_details(
    findings: dict[str, list[Finding]],
) -> None:
    """Print details (involved accounts and explanations) for all non-empty findings."""
    import sys
    has_any = False
    for pid in sorted(PATTERN_CATALOG.keys()):
        key = pattern_key(pid)
        items = findings.get(key, [])
        if not items:
            continue
        has_any = True
        label = PATTERN_CATALOG[pid].replace("_", " ").title()
        print(f"\n  --- {label} ---")
        for idx, f in enumerate(items, start=1):
            acct_str = ", ".join(f.accounts)
            explanation = f.explanation.replace("\u2192", "->")
            print(f"    Finding #{idx}:")
            print(f"      Accounts : {acct_str}")
            try:
                print(f"      Explain  : {explanation}")
            except UnicodeEncodeError:
                encoding = sys.stdout.encoding or "ascii"
                clean_explain = explanation.encode(encoding, errors="replace").decode(encoding)
                print(f"      Explain  : {clean_explain}")
            if f.details:
                try:
                    print(f"      Details  : {f.details}")
                except UnicodeEncodeError:
                    encoding = sys.stdout.encoding or "ascii"
                    details_str = str(f.details)
                    clean_details = details_str.encode(encoding, errors="replace").decode(encoding)
                    print(f"      Details  : {clean_details}")
    if not has_any:
        print("  (no findings found)")
