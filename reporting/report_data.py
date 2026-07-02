"""
report_data.py — Assemble the report context from a completed run's output.

This module ONLY reads and presents; it never re-computes or re-judges anything the
analysis phase decided (Report-phase ground rule 4). It loads:

  • analysis_results.json  — the primary data contract (suspicious_accounts, findings,
                             graph, counterparty_resolution, baseline_summary, …)
  • the extraction run's metadata.json — for per-account identity (account number,
                             holder, bank, IFSC) that the analysis output does not carry.

Everything is driven by what the files actually contain for the given run — no fixed
account counts, page counts, or score bands are assumed (ground rule 2).

The functions here are pure data (no Groq, no rendering) so the template-only dummy
pass (Section 7) uses this unchanged; the AI-narration pass later only *replaces* the
per-bullet / per-graph text, never the numbers assembled here.

Team: Survey Corps | CIDECODE Hackathon 2026 | CID Karnataka
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Score bands (Section 4.8 legend). Presented, never used to re-rank — ranking is the
# analysis phase's total_score order.
SCORE_BANDS = [
    (600.0, "strong", "Strong evidence across multiple independent pattern types — high investigative priority"),
    (300.0, "moderate", "Moderate evidence, worth follow-up but less urgent"),
    (0.0, "weak", "Weak or isolated signals, mostly for reference"),
]

# Section 4.6 — the three report graphs mapped to the images the analysis phase already
# renders into <run>/graphs/. Kept as an ordered list so a missing file degrades to a
# text note (Section 9) rather than a broken image.
REPORT_GRAPHS = [
    ("Account Interconnection Graph", "report_graphs/account_interconnection_graph.png",
     "This static exhibit is filtered for legibility: it shows the material account-to-account relationships and only the most evidential external counterparties, while the full-density network remains available in the UI graph data."),
    ("Suspicious Activity Timeline", "report_graphs/suspicious_timeline.png",
     "When flagged activity clustered in time across the investigated accounts."),
    ("Fraud-Pattern Summary", "report_graphs/fraud_pattern_summary.png",
     "Which fraud patterns fired, and how often, across the investigated accounts."),
]


def _band_for_score(score: float) -> tuple[str, str]:
    for threshold, tag, meaning in SCORE_BANDS:
        if score >= threshold:
            return tag, meaning
    return "weak", SCORE_BANDS[-1][2]


def _fmt_amount(value: Any) -> str:
    """Indian-grouped rupee formatting, defensive against strings/None."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return str(value or "")
    # Indian digit grouping (last 3, then pairs).
    s = f"{n:,.2f}"
    return f"Rs {s}"


def _load_json(path: Path) -> dict:
    return json.loads(Path(path).read_text())


def _resolve_paths(run_id: str | None, analysis_dir: str | Path | None,
                   extraction_dir: str | Path | None, repo_root: Path) -> tuple[Path, Path]:
    """Locate the analysis output dir and the extraction output dir for a run.

    Preference order: explicit dirs > run_id lookup > the run_metadata.input_path inside
    analysis_results.json (which records the exact extraction run that fed it).
    """
    if analysis_dir:
        adir = Path(analysis_dir)
    elif run_id:
        adir = repo_root / "analysis" / "outputs" / run_id
    else:
        raise ValueError("Provide run_id or analysis_dir")
    results = _load_json(adir / "analysis_results.json")
    if extraction_dir:
        edir = Path(extraction_dir)
    else:
        # run_metadata.input_path points at the exact extraction run that produced this.
        ip = (results.get("run_metadata", {}) or {}).get("input_path", "")
        edir = Path(ip) if ip and Path(ip).exists() else repo_root / "outputs" / "extractions" / (run_id or adir.name)
    return adir, edir


def _account_identity(extraction_dir: Path) -> dict[str, dict[str, str]]:
    """source_account_id -> {account_number, holder, bank, ifsc, branch, period, closing}.

    Read from the extraction metadata.json's per-file account_details (the richest,
    already-reconciled identity). Falls back to the per-statement JSON bundles.
    """
    identity: dict[str, dict[str, str]] = {}
    meta_path = extraction_dir / "metadata.json"
    if meta_path.exists():
        meta = _load_json(meta_path)
        for f in meta.get("files", []) or []:
            sid = f.get("source_account_id") or ""
            ad = f.get("account_details", {}) or {}
            if sid:
                identity[sid] = {
                    "account_number": ad.get("account_number", ""),
                    "holder": ad.get("account_holder", ""),
                    "bank": ad.get("bank_name", "") or f.get("bank_name", ""),
                    "ifsc": ad.get("ifsc_code", ""),
                    "branch": ad.get("branch", ""),
                    "period": ad.get("statement_period", ""),
                    "closing_balance": ad.get("closing_balance", ""),
                }
    return identity


def _graph_nodes_by_id(results: dict) -> dict[str, dict]:
    return {n.get("id"): n for n in (results.get("graph", {}) or {}).get("nodes", []) if n.get("id")}


def _edges_for_account(results: dict, account_id: str, limit: int = 8) -> list[dict]:
    """Top supporting transactions for an account, from the money-flow edges.

    Each edge already carries amount/date/reference and its source/target, so we can
    present a concrete evidence table (date, type, amount, counterparty, reference)
    without re-reading the ledger. Sorted by amount so the material movements show first.
    """
    nodes = _graph_nodes_by_id(results)
    edges = (results.get("graph", {}) or {}).get("edges", [])
    rows = []
    for e in edges:
        src, tgt = e.get("source"), e.get("target")
        if account_id not in (src, tgt):
            continue
        if src == account_id:
            direction, counter = "Debit", tgt
        else:
            direction, counter = "Credit", src
        cnode = nodes.get(counter, {})
        counter_label = cnode.get("account_holder") or cnode.get("bank_name") or str(counter)
        rows.append({
            "date": e.get("date", ""),
            "type": direction,
            "amount": _fmt_amount(e.get("amount")),
            "amount_value": float(e.get("amount") or 0.0),
            "counterparty": counter_label,
            "reference": e.get("reference", "") or "",
        })
    rows.sort(key=lambda r: r["amount_value"], reverse=True)
    return rows[:limit]


def _pattern_display(pattern_key: str) -> str:
    """'8_money_trail_tracing' -> 'Money Trail Tracing'."""
    parts = pattern_key.split("_", 1)
    name = parts[1] if len(parts) > 1 else pattern_key
    return name.replace("_", " ").title()


def _finding_evidence_string(finding: dict) -> str:
    """A richer authoritative evidence string for one finding: the deterministic
    narration PLUS every scalar fact in its details (amounts, dates, counts, statuses)
    and the accounts/txn ids involved. This is both the material the AI may elaborate
    on and the set its numbers/dates/accounts are validated against — so a fuller
    narration can be written without ever introducing an unsupported figure."""
    parts = [finding.get("narration") or finding.get("explanation") or ""]
    det = finding.get("details", {}) or {}
    for k, v in det.items():
        if isinstance(v, (str, int, float)) and str(v).strip():
            parts.append(f"{k}={v}")
        elif isinstance(v, dict):
            for kk, vv in v.items():
                if isinstance(vv, (str, int, float)) and str(vv).strip():
                    parts.append(f"{kk}={vv}")
    parts.append(" ".join(finding.get("accounts", []) or []))
    parts.append(" ".join(str(t) for t in (finding.get("txn_ids", []) or [])[:8]))
    return " | ".join(p for p in parts if str(p).strip())


def _account_bullets(results: dict, account_id: str) -> list[dict]:
    """One tight, concrete bullet per triggered pattern for this account.

    An account can trigger a pattern dozens of times; showing every finding would bury
    the report. So per pattern we surface the single most material finding (highest
    traced/credited/amount value in its details) and use the narration the analysis
    phase already wrote and validated. The AI pass later rewrites this text into
    investigator voice — the selection and evidence_strength stay the same.
    """
    fbp = results.get("findings_by_pattern", {}) or {}
    bullets = []
    for pkey, findings in fbp.items():
        # Skip the meta-ranking (21) and the lead-only patterns (22 LLM, 23 ML) — those
        # are not concrete per-account evidence; 22/23 are surfaced in the leads section.
        if pkey.split("_", 1)[0] in {"21", "22", "23"}:
            continue
        mine = [f for f in findings if account_id in (f.get("accounts") or [])]
        if not mine:
            continue
        # Rank this pattern's findings for the account by any numeric value in details.
        def _value(f: dict) -> float:
            det = f.get("details", {}) or {}
            for k in ("traced_amount", "credited_amount", "amount", "total_amount", "value"):
                try:
                    return float(det.get(k))
                except (TypeError, ValueError):
                    continue
            return 0.0
        top = max(mine, key=_value)
        bullets.append({
            "pattern": _pattern_display(pkey),
            "evidence_strength": top.get("evidence_strength", "weak"),
            "text": (top.get("narration") or top.get("explanation") or "").strip(),
            "evidence": _finding_evidence_string(top),
            "count": len(mine),
            "validated": top.get("narration_validation", ""),
        })
    # Strong-evidence bullets first, then by how many times the pattern fired.
    bullets.sort(key=lambda b: (b["evidence_strength"] != "strong", -b["count"]))
    return bullets


def _case_reconstruction_context(results: dict) -> dict[str, Any]:
    case_structure = results.get("case_structure", {}) or {}
    summaries = {
        item.get("cluster_id"): item
        for item in results.get("cluster_summaries", []) or []
        if item.get("cluster_id")
    }
    connected = []
    isolated = []
    for cluster in case_structure.get("clusters", []) or []:
        item = {
            "cluster_id": cluster.get("cluster_id", ""),
            "summary": (summaries.get(cluster.get("cluster_id"), {}) or {}).get("summary", ""),
            "members": cluster.get("member_accounts", []) or [],
            "account_count": cluster.get("account_count", 0),
            "edge_count": cluster.get("edge_count", 0),
            "highest_priority_account": cluster.get("highest_priority_account", ""),
            "total_score": round(float(cluster.get("total_score") or 0.0), 2),
        }
        if cluster.get("is_isolated"):
            isolated.append(item)
        else:
            connected.append(item)
    return {
        "summary": results.get("case_summary", ""),
        "cluster_count": case_structure.get("cluster_count", len(connected) + len(isolated)),
        "connected_clusters": connected,
        "isolated_clusters": isolated,
    }


def build_report_context(
    run_id: str | None = None,
    analysis_dir: str | Path | None = None,
    extraction_dir: str | Path | None = None,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    """Assemble the full, render-ready context for one completed run."""
    repo_root = Path(repo_root) if repo_root else Path(__file__).resolve().parents[1]
    adir, edir = _resolve_paths(run_id, analysis_dir, extraction_dir, repo_root)
    results = _load_json(adir / "analysis_results.json")

    run_meta = results.get("run_metadata", {}) or {}
    contract = results.get("input_contract", {}) or {}
    baseline = results.get("baseline_summary", {}) or {}
    cp = results.get("counterparty_resolution", {}) or {}
    balval = results.get("balance_validation", {}) or {}
    gsum = results.get("graph_summary", {}) or {}
    identity = _account_identity(edir)
    nodes = _graph_nodes_by_id(results)
    total_txn = contract.get("eligible_row_count", contract.get("clean_row_count", 0))

    # Number of ACTUAL uploaded statement files (from the extraction receipt), not the
    # analysis input manifest which lists the intermediate CSVs.
    files_processed = 0
    _meta_path = edir / "metadata.json"
    if _meta_path.exists():
        files_processed = int((_load_json(_meta_path).get("summary", {}) or {}).get("files_processed", 0) or 0)

    # ── Ranked findings blocks: observed accounts only (they have a statement + full
    # identity). Counterparty entries in suspicious_accounts have no holder/bank/number,
    # so they belong in the graph, not as identity blocks. Driven by observed_account,
    # never a hardcoded count. ──
    observed_ids = {nid for nid, n in nodes.items() if n.get("observed_account")}
    ranked = [a for a in results.get("suspicious_accounts", []) if a.get("account_id") in observed_ids]

    ranked_accounts = []
    for i, acc in enumerate(ranked, start=1):
        aid = acc["account_id"]
        ident = identity.get(aid, {})
        node = nodes.get(aid, {})
        holder = ident.get("holder") or node.get("account_holder") or aid
        bank = ident.get("bank") or node.get("bank_name") or ""
        score = float(acc.get("total_score", 0.0))
        tag, _meaning = _band_for_score(score)
        bullets = _account_bullets(results, aid)
        strong_n = acc.get("strong_pattern_count", 0)
        distinct_n = acc.get("distinct_pattern_count", 0)
        # Deterministic overview (used as-is unless the AI pass replaces it with a
        # validated, richer version). overview_evidence is what that AI text is checked
        # against, so it carries the score/pattern counts plus every bullet's evidence.
        overview_tmpl = (
            f"{aid} triggered {strong_n} strong pattern type(s) across {distinct_n} distinct "
            f"pattern(s), producing a composite suspicion score of {round(score)}.")
        overview_evidence = (
            f"account={aid} score={round(score)} strong_patterns={strong_n} "
            f"distinct_patterns={distinct_n} total_findings={acc.get('total_findings', 0)} || "
            + " || ".join(b["evidence"] for b in bullets))
        ranked_accounts.append({
            "rank": i,
            "account_id": aid,
            "holder": holder,
            "bank": bank,
            "account_number": ident.get("account_number", "") or node.get("account_number", ""),
            "ifsc": ident.get("ifsc") or node.get("ifsc_code", ""),
            "score": round(score),
            "band": tag,
            "distinct_pattern_count": distinct_n,
            "strong_pattern_count": strong_n,
            "total_findings": acc.get("total_findings", 0),
            "overview": overview_tmpl,
            "overview_evidence": overview_evidence,
            "bullets": bullets,
            "evidence_rows": _edges_for_account(results, aid),
            "assessment_tag": "strong" if acc.get("strong_pattern_count", 0) >= 1 else "weak",
            "key_concern": bullets[0]["pattern"] if bullets else "—",
        })

    # ── Input summary — per-account identity table (Section 4.3) ──
    input_accounts = []
    for aid in sorted(observed_ids):
        ident = identity.get(aid, {})
        node = nodes.get(aid, {})
        input_accounts.append({
            "source_account_id": aid,
            "account_number": ident.get("account_number", ""),
            "holder": ident.get("holder") or node.get("account_holder", ""),
            "bank": ident.get("bank") or node.get("bank_name", ""),
            "ifsc": ident.get("ifsc") or node.get("ifsc_code", ""),
        })

    # ── Graphs (Section 4.6) — embed existing PNGs; note if a file is missing. Each
    # graph also carries a compact STRUCTURED data summary (node/edge/event facts) so
    # the AI explanation is written from the data, never from the rendered image. ──
    graphs_dir = adir / "graphs"
    edges = (results.get("graph", {}) or {}).get("edges", [])
    top_flows = sorted(edges, key=lambda e: float(e.get("amount") or 0.0), reverse=True)[:5]
    fbp = results.get("findings_by_pattern", {}) or {}
    pattern_counts = {_pattern_display(k): len(v) for k, v in fbp.items() if v}
    graph_data = {
        "report_graphs/account_interconnection_graph.png": {
            "rendering_scope": (
                "Static report exhibit: filtered to high-value account-to-account flows, "
                "with low-value merchant and utility leaves suppressed except where a "
                "single material external counterparty is retained."
            ),
            "cluster_count": (results.get("case_structure", {}) or {}).get("cluster_count", 0),
            "ranked_accounts": [a.get("account_id") for a in results.get("suspicious_accounts", [])[:5]],
            "observed_accounts": sorted(observed_ids),
            "underlying_money_flow_graph_nodes": gsum.get("node_count", len(nodes)),
            "underlying_money_flow_graph_edges": gsum.get("edge_count", len(edges)),
            "largest_transfers": [
                {"amount": round(float(e.get("amount") or 0.0), 2),
                 "date": e.get("date", ""),
                 "from": nodes.get(e.get("source"), {}).get("account_holder") or e.get("source"),
                 "to": nodes.get(e.get("target"), {}).get("account_holder") or e.get("target")}
                for e in top_flows
            ],
        },
        "report_graphs/suspicious_timeline.png": {
            "date_start": (baseline.get("date_range", {}) or {}).get("start", ""),
            "date_end": (baseline.get("date_range", {}) or {}).get("end", ""),
            "total_transactions": total_txn,
            "flagged_excluded": (results.get("balance_validation", {}) or {}).get("balance_mismatch_excluded_count", 0),
        },
        "report_graphs/fraud_pattern_summary.png": {"pattern_counts": pattern_counts},
    }
    graphs = []
    for title, filename, template_expl in REPORT_GRAPHS:
        img = graphs_dir / filename
        graphs.append({
            "title": title,
            "image_path": str(img.resolve()) if img.exists() else None,
            "explanation": template_expl,
            "has_data": img.exists(),
            "data_summary": graph_data.get(filename, {}),
        })

    # ── AI-flagged leads (Section 4.7) — Pattern 23; omit the whole section if empty ──
    p23 = results.get("findings_by_pattern", {}).get("23_ml_ensemble_anomaly_lead", []) or []
    ai_leads = {"present": bool(p23), "count": len(p23), "findings": [
        {"text": f.get("narration") or f.get("explanation", ""),
         "accounts": ", ".join(f.get("accounts", []) or [])}
        for f in p23
    ]}

    date_range = baseline.get("date_range", {}) or {}

    context = {
        "meta": {
            "run_id": run_meta.get("run_id", adir.name),
            "extraction_run_id": run_meta.get("input_extraction_run_id", edir.name),
            "generated_at": datetime.now(timezone.utc).astimezone().strftime("%d %B %Y, %H:%M"),
            "team": "Survey Corps",
            "hackathon": "CIDECODE",
        },
        "input_summary": {
            "n_files": files_processed or len(input_accounts),
            "n_accounts": len(input_accounts),
            "n_transactions": total_txn,
            "date_start": date_range.get("start", ""),
            "date_end": date_range.get("end", ""),
            "accounts": input_accounts,
        },
        "analysis_summary": {
            "accounts_analyzed": baseline.get("account_count", len(input_accounts)),
            "total_transactions": total_txn,
            "counterparty_resolution_rate": round(float(cp.get("resolution_rate_percent", 0.0)), 1),
            "balance_note": balval.get("summary_line", ""),
            "accounts_flagged": len(ranked_accounts),
            "graph_nodes": gsum.get("node_count", 0),
            "graph_edges": gsum.get("edge_count", 0),
        },
        "case_reconstruction": _case_reconstruction_context(results),
        "ranked_accounts": ranked_accounts,
        "graphs": graphs,
        "ai_leads": ai_leads,
        "final_summary": {
            "ranked_table": [
                {"rank": a["rank"], "account": f'{a["holder"]} ({a["account_id"]})',
                 "score": a["score"], "key_concern": a["key_concern"]}
                for a in ranked_accounts
            ],
            "legend": [
                {"range": "600+", "meaning": SCORE_BANDS[0][2]},
                {"range": "300–599", "meaning": SCORE_BANDS[1][2]},
                {"range": "Below 300", "meaning": SCORE_BANDS[2][2]},
            ],
        },
    }
    return context


if __name__ == "__main__":
    import sys
    ctx = build_report_context(run_id=sys.argv[1] if len(sys.argv) > 1 else None)
    print(json.dumps(ctx, indent=2, default=str)[:4000])
