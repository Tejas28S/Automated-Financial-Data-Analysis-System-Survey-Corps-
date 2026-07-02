"""
build_report.py — Fill the HTML template with a run's data and render it to an A4 PDF.

Pipeline (Report-phase plan, Section 1):
    analysis_results.json + extraction metadata  →  report_data.build_report_context()
    →  Jinja2 fills report_template.html  →  Playwright (headless Chromium) page.pdf() → A4 PDF

This first pass is TEMPLATE-ONLY (Section 7): no Groq calls, no API keys touched. The
bullet text comes from the narration the ANALYSIS phase already wrote and validated; the
later AI pass only rewrites that text, never the numbers.

Run under the reporting venv (has jinja2 + playwright):
    .report_venv/bin/python reporting/build_report.py --run-id run_20260702_125349

Team: Survey Corps | CIDECODE Hackathon 2026 | CID Karnataka
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "reporting"))

from jinja2 import Environment, FileSystemLoader, select_autoescape  # noqa: E402

from report_data import build_report_context  # noqa: E402

TEMPLATE_DIR = REPO / "reporting"
TEMPLATE_NAME = "report_template.html"


def _embed_graph_images(context: dict) -> None:
    """Inline each graph PNG as a base64 data URI.

    We render via page.set_content(), which gives the page an about:blank origin —
    Chromium then refuses to load file:// images from it. Embedding the bytes directly
    sidesteps that entirely and makes the filled HTML fully self-contained/portable.
    """
    import base64
    for g in context.get("graphs", []):
        path = g.get("image_path")
        if g.get("has_data") and path and Path(path).exists():
            raw = Path(path).read_bytes()
            g["image_data_uri"] = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
        else:
            g["image_data_uri"] = None


def render_html(context: dict) -> str:
    _embed_graph_images(context)
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    return env.get_template(TEMPLATE_NAME).render(**context)


def render_pdf(html: str, out_pdf: Path, base_url: Path) -> dict:
    """Render HTML to an A4 PDF with real print pagination via headless Chromium.

    base_url lets the template's file:// <img> references to the run's graphs resolve.
    Returns the measured page size so the caller can assert A4 (Section 10 checklist).
    """
    from playwright.sync_api import sync_playwright

    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html, wait_until="load")
        page.emulate_media(media="print")
        page.pdf(
            path=str(out_pdf),
            format="A4",
            print_background=True,
            prefer_css_page_size=True,
            margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
        )
        browser.close()

    return _pdf_page_size_mm(out_pdf)


def _pdf_page_size_mm(pdf_path: Path) -> dict:
    """Read the first page's MediaBox (points → mm) to verify A4 = 210×297mm."""
    data = pdf_path.read_bytes()
    import re
    m = re.search(rb"/MediaBox\s*\[\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*\]", data)
    if not m:
        return {"width_mm": None, "height_mm": None, "is_a4": None}
    x0, y0, x1, y1 = (float(v) for v in m.groups())
    w_mm = round((x1 - x0) * 25.4 / 72.0, 1)
    h_mm = round((y1 - y0) * 25.4 / 72.0, 1)
    is_a4 = abs(w_mm - 210) <= 2 and abs(h_mm - 297) <= 2
    return {"width_mm": w_mm, "height_mm": h_mm, "is_a4": is_a4}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", help="run id under analysis/outputs/ and outputs/extractions/")
    ap.add_argument("--analysis-dir", help="explicit analysis output dir")
    ap.add_argument("--extraction-dir", help="explicit extraction output dir")
    ap.add_argument("--out", help="output PDF path (default: <analysis-dir>/investigation_report_dummy.pdf)")
    ap.add_argument("--save-html", action="store_true", help="also write the filled HTML next to the PDF")
    ap.add_argument("--ai", action="store_true", help="enable the GROQ6-9 narration pass (Section 3)")
    ap.add_argument("--max-llm-calls", type=int, default=None, help="cap report LLM calls (default: unlimited)")
    args = ap.parse_args()

    context = build_report_context(
        run_id=args.run_id, analysis_dir=args.analysis_dir,
        extraction_dir=args.extraction_dir, repo_root=REPO,
    )

    # ── Optional AI narration pass (Section 3/4.5/4.6). Off by default: the template-only
    # dummy touches no API keys. With --ai, the dedicated GROQ6-9 pool rewrites bullets and
    # graph explanations, each validated against evidence with per-item template fallback. ──
    narration_log = {"mode": "template_only"}
    if args.ai:
        from report_llm import ReportKeyPool
        from narration import narrate_report
        pool = ReportKeyPool(max_calls=args.max_llm_calls)
        narration_log = {"mode": "ai", **narrate_report(pool, context)}
        print(f"[build_report] llm pool status : {pool.status_label()} "
              f"(keys: {', '.join(pool.key_labels) or 'none'}, calls: {pool.call_count})")

    html = render_html(context)

    if args.out:
        out_pdf = Path(args.out)
    else:
        adir = REPO / "analysis" / "outputs" / (args.run_id or context["meta"]["run_id"])
        name = "investigation_report.pdf" if args.ai else "investigation_report_dummy.pdf"
        out_pdf = adir / name

    if args.save_html:
        (out_pdf.with_suffix(".html")).write_text(html)

    size = render_pdf(html, out_pdf, base_url=TEMPLATE_DIR)
    # Run log (Section 3) — key-pool status + per-item narration source; never in the PDF.
    import json as _json
    out_pdf.with_suffix(".runlog.json").write_text(_json.dumps(narration_log, indent=2))

    print(f"[build_report] run_id          : {context['meta']['run_id']}")
    print(f"[build_report] accounts flagged : {context['analysis_summary']['accounts_flagged']}")
    print(f"[build_report] AI-leads section : {'included' if context['ai_leads']['present'] else 'omitted (Pattern 23 empty)'}")
    print(f"[build_report] PDF written      : {out_pdf}")
    print(f"[build_report] page size        : {size['width_mm']} x {size['height_mm']} mm "
          f"({'A4 OK' if size['is_a4'] else 'NOT A4'})")
    return 0 if size.get("is_a4") else 2


if __name__ == "__main__":
    raise SystemExit(main())
