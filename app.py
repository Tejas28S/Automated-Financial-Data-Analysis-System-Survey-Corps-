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

import time
from datetime import datetime
from pathlib import Path

import streamlit as st


def _fmt_duration(seconds: float) -> str:
    """Human-friendly duration: '8.4 s' or '2 min 5 s'."""
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.1f} s"
    m, s = divmod(int(round(seconds)), 60)
    return f"{m} min {s} s"

from config.settings import UPLOAD_DIR, EXTRACTIONS_DIR, SUPPORTED_EXTENSIONS
from extraction.extraction_pipeline import run_extraction_pipeline

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
    st.divider()
    st.markdown("**Outputs are saved under the project root:**")
    st.code(
        "uploads/<session_id>/\n"
        "outputs/extractions/<session_id>/\n"
        "  clean_transactions.csv\n"
        "  flagged_transactions.csv\n"
        "  metadata.json\n"
        "  statements/*.json\n"
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

    # Stash in session_state so the result survives Streamlit's re-run on a
    # download-button click (Streamlit reruns the whole script each interaction).
    st.session_state["last_result"] = result
    st.session_state["last_session"] = session_id
    st.session_state["last_elapsed"] = elapsed

# ── Show the results ─────────────────────────────────────────────────────────
result = st.session_state.get("last_result")
if result:
    session_id = st.session_state.get("last_session", "")
    paths = result.get("storage_paths", {}) or {}
    out_folder = paths.get("folder", str(Path(EXTRACTIONS_DIR) / session_id))

    st.success(f"Done — session **{session_id}**")
    st.write(f"**Output folder:** `{out_folder}`")
    if result.get("files_failed"):
        st.warning(f"Files that failed completely: {result['files_failed']}")

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Clean rows", result.get("clean_rows", 0))
    m2.metric("Flagged rows", result.get("flagged_rows", 0))
    m3.metric("Files processed", result.get("files_processed", 0))
    m4.metric("Total LLM calls", sum(r.get("llm_calls", 0) for r in result.get("per_file", [])))
    m5.metric("Time taken", _fmt_duration(st.session_state.get("last_elapsed", 0.0)))

    tab_clean, tab_flagged, tab_receipt, tab_dl = st.tabs(
        ["Clean transactions", "Flagged rows", "Per-file receipt", "Downloads"]
    )

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
            st.info("No flagged rows. 🎉")

    with tab_receipt:
        st.caption("Per-file audit: route, OCR engine, tier reached, reconciliation rate, LLM calls.")
        for rec in result.get("per_file", []):
            header = (f"{rec.get('file', '?')}  —  tier={rec.get('tier', '?')}, "
                      f"reconcile={rec.get('reconciliation_rate', '?')}, "
                      f"llm_calls={rec.get('llm_calls', 0)}")
            with st.expander(header):
                st.json(rec)

    with tab_dl:
        for label, key, mime in [
            ("clean_transactions.csv", "clean_csv", "text/csv"),
            ("flagged_transactions.csv", "flagged_csv", "text/csv"),
            ("metadata.json", "metadata_json", "application/json"),
        ]:
            p = paths.get(key)
            if p and Path(p).exists():
                st.download_button(
                    f"⬇ {label}", Path(p).read_bytes(),
                    file_name=Path(p).name, mime=mime, key=f"dl_{key}",
                )
        stmt_dir = paths.get("statements_dir")
        if stmt_dir and Path(stmt_dir).exists():
            json_files = sorted(Path(stmt_dir).glob("*.json"))
            if json_files:
                st.caption("Per-statement JSON (real identity + that statement's transactions):")
                for p in json_files:
                    st.download_button(
                        f"⬇ {p.name}", p.read_bytes(),
                        file_name=p.name, mime="application/json", key=f"dl_stmt_{p.name}",
                    )
