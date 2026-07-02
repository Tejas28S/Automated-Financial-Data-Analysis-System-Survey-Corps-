"""Reproducible command-line entry point for the analysis pipeline."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import AnalysisConfig
from .output import print_summary
from .pipeline import AnalysisPipeline


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the analysis pipeline."""
    parser = argparse.ArgumentParser(
        prog="analysis_engine",
        description="Financial crime analysis phase — deterministic pattern detection.",
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Path to a single CSV or a directory containing the extraction triplet.",
    )
    parser.add_argument(
        "--output-dir",
        default="./outputs",
        help="Directory for SQLite database and JSON results (default: ./outputs).",
    )
    parser.add_argument(
        "--trace-credits",
        nargs="*",
        default=[],
        help="Credit transaction IDs for Pattern 10 money-trail tracing.",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Disable optional LLM fallback for counterparty resolution.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable detailed logging output.",
    )
    args = parser.parse_args(argv)

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    config = AnalysisConfig(enable_llm_fallback=not args.no_llm)
    pipeline = AnalysisPipeline(
        input_path=args.input,
        output_dir=args.output_dir,
        config=config,
        credit_txn_ids=args.trace_credits,
    )

    try:
        result = pipeline.run()
        print_summary(result)

        # Write rich structured JSON report to analysis_op/<case_name>/report.json
        from .rich_report import write_rich_report
        project_root = Path(args.input).resolve().parent
        # Walk up until we find the analysis_engine folder (i.e. project root)
        search = Path(args.input).resolve().parent
        for _ in range(6):
            if (search / "analysis_engine").is_dir():
                project_root = search
                break
            search = search.parent
        report_path = write_rich_report(result, args.input, project_root)
        print(f"\n  [Rich Report] Written to: {report_path}")

        # Also copy report.json and report.txt to output_dir
        import shutil
        out_dir = Path(args.output_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(report_path, out_dir / "report.json")
        report_txt_src = report_path.parent / "report.txt"
        if report_txt_src.exists():
            shutil.copy2(report_txt_src, out_dir / "report.txt")

        return 0
    except Exception as exc:
        logging.getLogger(__name__).exception("Pipeline failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
