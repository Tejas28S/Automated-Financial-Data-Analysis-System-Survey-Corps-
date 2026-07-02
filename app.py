"""
app.py — Simple LOCAL testing interface for the Survey Corps extraction pipeline.

PURPOSE (deliberately simple):
    A small Streamlit page the team uses to confirm extraction works: upload one or
    more bank-statement files, run the SAME pipeline the backend uses, then inspect
    the clean transactions, the flagged rows, the per-file audit receipt, and
    download the CSV + JSON outputs. This is a validation tool, not the production UI.

WHERE YOUR FILES GO (also shown in the app after a run):
    uploads/<session_id>/                       <- the files you upload
    outputs/extractions/<session_id>/
        clean_transactions.csv                  <- clean, verified rows
        flagged_transactions.csv                <- rows held for manual review (+reason)
        metadata.json                           <- the run receipt (tier, reconcile, OCR)
        statements/<holder>_<account>.json       <- one structured file per statement
    storage/llm_cache/                           <- cached LLM/vision replies (re-runs cost 0 tokens)

RUN IT (from the project root):
    streamlit run app.py

Team: Survey Corps | CIDECODE Hackathon 2026 | CID Karnataka
"""

import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import streamlit as st

_REPO = Path(__file__).resolve().parent


def _generate_investigation_report(session_id: str, analysis_output_dir: Path) -> tuple[Path | None, str]:
    """Build the court-ready PDF for a run and return (pdf_path, status_message).

    The report generator runs under its own virtualenv (.report_venv) because it needs
    Playwright/Chromium, which the app's Python does not carry — so we invoke it as a
    subprocess. It uses the dedicated GROQ6-9 key pool for narration and falls back to
    template text automatically if those keys are unavailable, so this never fails hard.
    """
    venv_py = _REPO / ".report_venv" / "bin" / "python"
    if not venv_py.exists():
        return None, ("Report renderer not installed. One-time setup:\n"
                      "  python3 -m venv .report_venv && .report_venv/bin/pip install "
                      "playwright jinja2 groq && .report_venv/bin/python -m playwright install chromium")
    out_pdf = analysis_output_dir / "investigation_report.pdf"
    cmd = [str(venv_py), str(_REPO / "reporting" / "build_report.py"),
           "--run-id", session_id, "--ai", "--out", str(out_pdf)]
    try:
        proc = subprocess.run(cmd, cwd=str(_REPO), capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return None, "Report generation timed out."
    if proc.returncode == 0 and out_pdf.exists():
        return out_pdf, "Report generated."
    return (out_pdf if out_pdf.exists() else None), (proc.stderr or proc.stdout or "unknown error")[-800:]


def _fmt_duration(seconds: float) -> str:
    """Human-friendly duration: '8.4 s' or '2 min 5 s'."""
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.1f} s"
    m, s = divmod(int(round(seconds)), 60)
    return f"{m} min {s} s"


def _split_comma_values(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _render_finding_card(finding: dict) -> None:
    narration = finding.get("narration") or finding.get("explanation", "")
    st.write(f"**Accounts:** {', '.join(finding.get('accounts', [])) or '-'}")
    st.write(f"**Transactions:** {', '.join(finding.get('txn_ids', [])[:12]) or '-'}")
    st.write(narration)
    st.caption(
        f"Evidence: {finding.get('evidence_strength', '-')} | "
        f"Narration: {finding.get('narration_validation', '-')}"
    )
    with st.expander("Raw evidence", expanded=False):
        st.json(
            {
                "finding_id": finding.get("finding_id", ""),
                "confidence_tier": finding.get("confidence_tier", ""),
                "detection_method": finding.get("detection_method", ""),
                "explanation": finding.get("explanation", ""),
                "details": finding.get("details", {}),
            }
        )


def _render_case_reconstruction(analysis_payload: dict) -> None:
    case_structure = analysis_payload.get("case_structure", {}) or {}
    clusters = case_structure.get("clusters", []) or []
    summaries = {
        item.get("cluster_id"): item
        for item in analysis_payload.get("cluster_summaries", []) or []
        if item.get("cluster_id")
    }
    case_summary = analysis_payload.get("case_summary", "")

    if not case_summary and not clusters:
        return

    st.subheader("Case reconstruction")
    with st.container(border=True):
        if case_summary:
            st.write(case_summary)
        connected = [cluster for cluster in clusters if not cluster.get("is_isolated")]
        isolated = [cluster for cluster in clusters if cluster.get("is_isolated")]
        c1, c2, c3 = st.columns(3)
        c1.metric("Clusters", case_structure.get("cluster_count", len(clusters)))
        c2.metric("Connected clusters", len(connected))
        c3.metric("Isolated accounts", sum(len(c.get("member_accounts", [])) for c in isolated))

        for cluster in connected:
            cluster_id = cluster.get("cluster_id", "cluster")
            summary = summaries.get(cluster_id, {})
            title = (
                f"{cluster_id}: {cluster.get('account_count', 0)} account(s), "
                f"{cluster.get('edge_count', 0)} edge(s)"
            )
            with st.expander(title, expanded=True):
                st.write(summary.get("summary") or "No narrative summary was generated for this cluster.")
                st.caption(
                    f"Highest-priority account: {cluster.get('highest_priority_account', '-')}; "
                    f"total score: {cluster.get('total_score', 0)}"
                )
                members = cluster.get("member_accounts", []) or []
                st.write("**Member accounts:**")
                st.write(", ".join(str(member) for member in members[:80]) or "-")
                if len(members) > 80:
                    st.caption(f"{len(members) - 80} additional member(s) omitted from this preview.")
                st.caption("Graph reference: Account Interconnection Graph in the generated report graphs.")

        if isolated:
            with st.expander("Accounts with no established connections in this case", expanded=False):
                for cluster in isolated:
                    members = cluster.get("member_accounts", []) or []
                    summary = summaries.get(cluster.get("cluster_id"), {})
                    st.write(f"**{cluster.get('cluster_id', 'cluster')}**: {', '.join(map(str, members)) or '-'}")
                    if summary.get("summary"):
                        st.caption(summary["summary"])


from config.settings import UPLOAD_DIR, EXTRACTIONS_DIR, SUPPORTED_EXTENSIONS
from extraction.extraction_pipeline import run_extraction_pipeline
from analysis.analysis_engine.config import AnalysisConfig
from analysis.analysis_engine.pipeline import AnalysisPipeline

st.set_page_config(page_title="Survey Corps — Extraction Tester", layout="wide")
st.title("Survey Corps — Extraction Tester")
st.caption(
    "Local testing only. Upload statements, run the tiered extraction pipeline, and "
    "inspect the clean table, flagged rows, the audit receipt, and CSV/JSON outputs."
)

# ── Sidebar: bounded-run controls + where outputs land ───────────────────────
with st.sidebar:
    st.header("Run settings")
    max_ocr_pages = st.number_input(
        "Max OCR pages per scanned PDF",
        min_value=1, max_value=100, value=3,
        help="Caps OCR work so the laptop stays cool. Raise it to read a full scanned PDF.",
    )
    manual_anomaly_accounts = st.text_input(
        "Manual analysis account IDs",
        value="",
        help="Optional comma-separated account IDs for Pattern 22 investigation.",
    )
    trace_credit_txn_ids = st.text_input(
        "Money trail credit txn IDs",
        value="",
        help="Optional comma-separated credit transaction IDs for Pattern 8 tracing.",
    )
    st.divider()
    st.subheader("Analysis only")
    analysis_csv_upload = st.file_uploader(
        "Final extracted CSV for analysis",
        type=["csv"],
        key="analysis_csv_upload",
        help="Upload a clean/final extracted transaction CSV and run analysis without rerunning extraction.",
    )
    run_csv_analysis = st.button(
        "Run analysis on CSV",
        disabled=analysis_csv_upload is None,
    )
    st.divider()
    st.markdown("**Outputs are saved under the project root:**")
    st.code(
        "uploads/<session_id>/\n"
        "outputs/extractions/<session_id>/\n"
        "  clean_transactions.csv\n"
        "  flagged_transactions.csv\n"
        "  metadata.json\n"
        "  statements/*.json\n"
        "analysis/outputs/<session_id>/\n"
        "  analysis_results.json\n"
        "  analysis_summary.txt\n"
        "  analysis.db\n"
        "storage/llm_cache/   (cached, re-runs cost 0 tokens)",
        language="text",
    )

# ── Upload ───────────────────────────────────────────────────────────────────
uploaded = st.file_uploader(
    "Upload bank statement files (PDF / Excel / CSV / DOCX / image)",
    type=[e.lstrip(".") for e in SUPPORTED_EXTENSIONS],
    accept_multiple_files=True,
)

files_meta = []
if uploaded:
    st.subheader("Files to process")
    st.caption(
        "The account/bank boxes are only HINTS — the pipeline reads the real "
        "account identity from each statement's own content."
    )
    for i, uf in enumerate(uploaded):
        c1, c2, c3 = st.columns([3, 2, 2])
        c1.write(f"📄 {uf.name}")
        acc = c2.text_input("Account hint", value=Path(uf.name).stem, key=f"acc_{i}")
        bank = c3.text_input("Bank hint", value="", key=f"bank_{i}")
        files_meta.append((uf, acc, bank))

run = st.button("▶ Run extraction", type="primary", disabled=not uploaded)

# ── Run the pipeline ─────────────────────────────────────────────────────────
if run and files_meta:
    session_id = datetime.now().strftime("run_%Y%m%d_%H%M%S")
    session_upload_dir = Path(UPLOAD_DIR) / session_id
    session_upload_dir.mkdir(parents=True, exist_ok=True)

    files = []
    for uf, acc, bank in files_meta:
        dest = session_upload_dir / uf.name
        dest.write_bytes(uf.getbuffer())  # persist the upload so the pipeline can read it
        files.append({
            "file_path": str(dest),
            "account_id": acc or Path(uf.name).stem,
            "bank_name": bank or "Unknown Bank",
        })

    with st.spinner(f"Running extraction on {len(files)} file(s)…"):
        start = time.perf_counter()
        try:
            result = run_extraction_pipeline(
                files=files,
                session_id=session_id,
                ingest_to_chromadb=False,          # RAG phase — kept off here
                max_ocr_pages=int(max_ocr_pages),  # bounded so the laptop stays cool
                persist=True,
            )
        except Exception as exc:  # surface the error instead of a blank page
            st.error(f"Extraction failed after {_fmt_duration(time.perf_counter() - start)}: {exc}")
            st.stop()
        elapsed = time.perf_counter() - start

    analysis_result = None
    analysis_elapsed = 0.0
    extraction_output = Path(result.get("storage_paths", {}).get("folder", Path(EXTRACTIONS_DIR) / session_id))
    if not result.get("files_failed") and extraction_output.exists():
        with st.spinner("Running analysis on extracted transactions..."):
            analysis_start = time.perf_counter()
            try:
                analysis_pipeline = AnalysisPipeline(
                    input_path=extraction_output,
                    output_dir=Path("analysis") / "outputs" / session_id,
                    config=AnalysisConfig(),
                    credit_txn_ids=_split_comma_values(trace_credit_txn_ids),
                    anomaly_account_ids=_split_comma_values(manual_anomaly_accounts),
                )
                analysis_result = analysis_pipeline.run()
            except Exception as exc:
                st.error(f"Analysis failed after {_fmt_duration(time.perf_counter() - analysis_start)}: {exc}")
                analysis_result = None
            analysis_elapsed = time.perf_counter() - analysis_start

    # Stash in session_state so the result survives Streamlit's re-run on a
    # download-button click (Streamlit reruns the whole script each interaction).
    st.session_state["last_result"] = result
    st.session_state["last_analysis_result"] = analysis_result.to_dict() if analysis_result is not None else None
    st.session_state["last_session"] = session_id
    st.session_state["last_elapsed"] = elapsed
    st.session_state["last_analysis_elapsed"] = analysis_elapsed
    st.session_state["last_analysis_input_path"] = str(extraction_output)
    st.session_state["last_analysis_output_dir"] = str(Path("analysis") / "outputs" / session_id)

if run_csv_analysis and analysis_csv_upload is not None:
    session_id = datetime.now().strftime("run_%Y%m%d_%H%M%S")
    session_upload_dir = Path(UPLOAD_DIR) / session_id
    session_upload_dir.mkdir(parents=True, exist_ok=True)

    csv_path = session_upload_dir / analysis_csv_upload.name
    csv_path.write_bytes(analysis_csv_upload.getbuffer())
    analysis_output_dir = Path("analysis") / "outputs" / session_id

    with st.spinner("Running analysis on uploaded CSV..."):
        analysis_start = time.perf_counter()
        try:
            analysis_pipeline = AnalysisPipeline(
                input_path=csv_path,
                output_dir=analysis_output_dir,
                config=AnalysisConfig(),
                credit_txn_ids=_split_comma_values(trace_credit_txn_ids),
                anomaly_account_ids=_split_comma_values(manual_anomaly_accounts),
            )
            analysis_result = analysis_pipeline.run()
        except Exception as exc:
            st.error(f"CSV analysis failed after {_fmt_duration(time.perf_counter() - analysis_start)}: {exc}")
            st.stop()
        analysis_elapsed = time.perf_counter() - analysis_start

    st.session_state["last_result"] = None
    st.session_state["last_analysis_result"] = analysis_result.to_dict()
    st.session_state["last_session"] = session_id
    st.session_state["last_elapsed"] = 0.0
    st.session_state["last_analysis_elapsed"] = analysis_elapsed
    st.session_state["last_analysis_input_path"] = str(csv_path)
    st.session_state["last_analysis_output_dir"] = str(analysis_output_dir)

# ── Show the results ─────────────────────────────────────────────────────────
result = st.session_state.get("last_result")
analysis_payload = st.session_state.get("last_analysis_result")
if result or analysis_payload:
    session_id = st.session_state.get("last_session", "")
    paths = (result.get("storage_paths", {}) or {}) if result else {}
    out_folder = paths.get("folder", str(Path(EXTRACTIONS_DIR) / session_id))

    # ── Investigation report — always visible in the LEFT SIDEBAR once analysis has run ──
    if analysis_payload:
        _analysis_dir = Path(
            st.session_state.get("last_analysis_output_dir", Path("analysis") / "outputs" / session_id)
        )
        _report_pdf = _analysis_dir / "investigation_report.pdf"
        st.sidebar.divider()
        st.sidebar.subheader("📄 Investigation report")
        st.sidebar.caption(f"Run {session_id}")
        if _report_pdf.exists():
            st.sidebar.download_button(
                "⬇ Download report (PDF)",
                _report_pdf.read_bytes(),
                file_name=f"investigation_report_{session_id}.pdf",
                mime="application/pdf",
                key=f"sb_dl_report_{session_id}",
                use_container_width=True,
            )
            if st.sidebar.button("Regenerate report", key=f"sb_regen_{session_id}", use_container_width=True):
                _report_pdf.unlink(missing_ok=True)
                st.rerun()
        else:
            if st.sidebar.button("Generate report (PDF)", key=f"sb_gen_{session_id}",
                                 type="primary", use_container_width=True):
                with st.spinner("Generating court-ready PDF report..."):
                    _pdf_path, _msg = _generate_investigation_report(session_id, _analysis_dir)
                if _pdf_path and _pdf_path.exists():
                    st.sidebar.success("Report ready — click Download above.")
                    st.rerun()
                else:
                    st.sidebar.error(f"Report generation failed: {_msg}")

    if result:
        st.success(f"Done — session **{session_id}**")
        st.write(f"**Output folder:** `{out_folder}`")
        if result.get("files_failed"):
            st.warning(f"Files that failed completely: {result['files_failed']}")

        m1, m2, m3, m4, m5, m6 = st.columns(6)
        m1.metric("Clean rows", result.get("clean_rows", 0))
        m2.metric("Flagged rows", result.get("flagged_rows", 0))
        m3.metric("Files processed", result.get("files_processed", 0))
        m4.metric("Total LLM calls", sum(r.get("llm_calls", 0) for r in result.get("per_file", [])))
        m5.metric("Time taken", _fmt_duration(st.session_state.get("last_elapsed", 0.0)))
        if analysis_payload:
            m6.metric("Analysis time", _fmt_duration(st.session_state.get("last_analysis_elapsed", 0.0)))
        else:
            m6.metric("Analysis", "not run")
    else:
        st.success(f"Analysis done — session **{session_id}**")
        st.write(f"**Analysis input:** `{st.session_state.get('last_analysis_input_path', '')}`")
        st.write(
            f"**Analysis output folder:** "
            f"`{st.session_state.get('last_analysis_output_dir', str(Path('analysis') / 'outputs' / session_id))}`"
        )

        baseline = analysis_payload.get("baseline_summary", {}) if analysis_payload else {}
        counterparty = analysis_payload.get("counterparty_resolution", {}) if analysis_payload else {}
        row_counts = baseline.get("row_counts", {})
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Transactions analyzed", row_counts.get("eligible", 0))
        m2.metric("Accounts", baseline.get("account_count", 0))
        m3.metric("Counterparty resolution", f"{counterparty.get('resolution_rate_percent', 0):.1f}%")
        m4.metric("Analysis time", _fmt_duration(st.session_state.get("last_analysis_elapsed", 0.0)))

    if result:
        tab_analysis, tab_clean, tab_flagged, tab_receipt, tab_dl = st.tabs(
            ["Analysis", "Clean transactions", "Flagged rows", "Per-file receipt", "Downloads"]
        )
    else:
        tab_analysis, tab_dl = st.tabs(["Analysis", "Downloads"])

    with tab_analysis:
        if not analysis_payload:
            st.info("Analysis did not complete for this run.")
        else:
            baseline = analysis_payload.get("baseline_summary", {})
            counterparty = analysis_payload.get("counterparty_resolution", {})
            a1, a2, a3, a4 = st.columns(4)
            row_counts = baseline.get("row_counts", {})
            a1.metric("Transactions analyzed", row_counts.get("eligible", 0))
            a2.metric("Accounts", baseline.get("account_count", 0))
            a3.metric("Counterparty resolution", f"{counterparty.get('resolution_rate_percent', 0):.1f}%")
            a4.metric("LLM status", analysis_payload.get("run_metadata", {}).get("llm_status", "-"))

            _render_case_reconstruction(analysis_payload)

            st.subheader("Suspicious accounts")
            suspicious_accounts = analysis_payload.get("suspicious_accounts", [])
            if suspicious_accounts:
                visible_accounts = []
                for account in suspicious_accounts:
                    breakdown = account.get("score_breakdown", {}) or {}
                    visible_accounts.append(
                        {
                            "account_id": account.get("account_id", ""),
                            "total_score": account.get("total_score", 0),
                            "distinct_pattern_count": account.get("distinct_pattern_count", 0),
                            "strong_pattern_count": account.get("strong_pattern_count", 0),
                            "weak_pattern_count": account.get("weak_pattern_count", 0),
                            "total_findings": account.get("total_findings", 0),
                            "breadth_component": breakdown.get("breadth_component", 0),
                            "weighted_severity": breakdown.get("weighted_severity", 0),
                            "value_component": breakdown.get("value_component", 0),
                            "centrality_bonus": breakdown.get("centrality_bonus", 0),
                            "pattern_breakdown": account.get("pattern_breakdown", {}),
                        }
                    )
                st.dataframe(visible_accounts, use_container_width=True, hide_index=True)
            else:
                st.info("No ranked suspicious accounts.")

            findings_by_pattern = analysis_payload.get("findings_by_pattern", {})
            priority_keys = [
                "17_round_trip_detection",
                "8_money_trail_tracing",
                "15_round_value_debit_patterns",
            ]
            st.subheader("Priority findings")
            for key in priority_keys:
                findings = findings_by_pattern.get(key, [])
                with st.expander(f"{key}: {len(findings)} finding(s)", expanded=bool(findings)):
                    if findings:
                        for finding in findings:
                            _render_finding_card(finding)
                    else:
                        st.caption("0 findings")

            st.subheader("Scored patterns")
            for key, findings in findings_by_pattern.items():
                if key in priority_keys or key.startswith(("22_", "23_")):
                    continue
                with st.expander(f"{key}: {len(findings)} finding(s)"):
                    if findings:
                        for finding in findings[:50]:
                            _render_finding_card(finding)
                    else:
                        st.caption("0 findings")

            st.subheader("Additional leads - not part of scored ranking")
            p22 = findings_by_pattern.get("22_llm_investigated_anomalies", [])
            for finding in p22:
                st.write(f"**Status:** {finding.get('details', {}).get('trigger_status', '-')}")
                _render_finding_card(finding)
            p23 = findings_by_pattern.get("23_ml_ensemble_anomaly_lead", [])
            for finding in p23:
                _render_finding_card(finding)

    if result:
        with tab_clean:
            clean_df = result.get("clean_df")
            if clean_df is not None and not clean_df.empty:
                st.caption(
                    "Reference_Number and Cheque_Number are kept as separate fields; "
                    "either is blank when the statement does not provide it."
                )
                st.dataframe(clean_df, use_container_width=True, hide_index=True)
            else:
                st.info("No clean transactions were produced.")

        with tab_flagged:
            flagged_df = result.get("flagged_df")
            if flagged_df is not None and not flagged_df.empty:
                st.caption("Flagged rows are never dropped — each carries a flag_reason.")
                st.dataframe(flagged_df, use_container_width=True, hide_index=True)
            else:
                st.info("No flagged rows.")

        with tab_receipt:
            st.caption("Per-file audit: route, OCR engine, tier reached, reconciliation rate, LLM calls.")
            for rec in result.get("per_file", []):
                header = (f"{rec.get('file', '?')}  —  tier={rec.get('tier', '?')}, "
                          f"reconcile={rec.get('reconciliation_rate', '?')}, "
                          f"llm_calls={rec.get('llm_calls', 0)}")
                with st.expander(header):
                    st.json(rec)

    with tab_dl:
        analysis_output_dir = Path(
            st.session_state.get("last_analysis_output_dir", Path("analysis") / "outputs" / session_id)
        )

        # ── Court-ready investigation report (Report phase, Section 8) ──
        if analysis_payload and analysis_output_dir.exists():
            st.caption("Investigation report:")
            report_pdf = analysis_output_dir / "investigation_report.pdf"
            if report_pdf.exists():
                st.download_button(
                    "⬇ Download investigation report (PDF)",
                    report_pdf.read_bytes(),
                    file_name=f"investigation_report_{session_id}.pdf",
                    mime="application/pdf",
                    key=f"dl_report_{session_id}",
                )
                if st.button("Regenerate report", key=f"regen_report_{session_id}"):
                    report_pdf.unlink(missing_ok=True)
                    st.rerun()
            else:
                if st.button("Generate investigation report (PDF)", key=f"gen_report_{session_id}"):
                    with st.spinner("Generating court-ready PDF report..."):
                        pdf_path, msg = _generate_investigation_report(session_id, analysis_output_dir)
                    if pdf_path and pdf_path.exists():
                        st.success(msg)
                        st.rerun()
                    else:
                        st.error(f"Report generation failed: {msg}")
            st.divider()

        if analysis_output_dir.exists():
            st.caption("Analysis outputs:")
            for label, filename, mime in [
                ("analysis_results.json", "analysis_results.json", "application/json"),
                ("analysis_summary.txt", "analysis_summary.txt", "text/plain"),
                ("analysis.db", "analysis.db", "application/octet-stream"),
            ]:
                p = analysis_output_dir / filename
                if p.exists():
                    st.download_button(
                        f"Download {label}",
                        p.read_bytes(),
                        file_name=p.name,
                        mime=mime,
                        key=f"dl_analysis_{session_id}_{filename}",
                    )
        if result:
            st.caption("Extraction outputs:")
            for label, key, mime in [
                ("clean_transactions.csv", "clean_csv", "text/csv"),
                ("flagged_transactions.csv", "flagged_csv", "text/csv"),
                ("metadata.json", "metadata_json", "application/json"),
            ]:
                p = paths.get(key)
                if p and Path(p).exists():
                    st.download_button(
                        f"Download {label}", Path(p).read_bytes(),
                        file_name=Path(p).name, mime=mime, key=f"dl_{key}",
                    )
            stmt_dir = paths.get("statements_dir")
            if stmt_dir and Path(stmt_dir).exists():
                json_files = sorted(Path(stmt_dir).glob("*.json"))
                if json_files:
                    st.caption("Per-statement JSON (real identity + that statement's transactions):")
                    for p in json_files:
                        st.download_button(
                            f"Download {p.name}",
                            p.read_bytes(),
                            file_name=p.name,
                            mime="application/json",
                            key=f"dl_stmt_{p.name}",
                        )
