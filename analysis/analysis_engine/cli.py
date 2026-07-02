# Copied from: analysis phase cidecode hackathon/analysis phase cidecode hackathon/analysis_engine/cli.py
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
        "--investigate-accounts",
        nargs="*",
        default=[],
        help="Manual account IDs for Pattern 22 LLM-investigated anomaly review.",
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
        anomaly_account_ids=args.investigate_accounts,
    )

    try:
        result = pipeline.run()
        print_summary(result)
        return 0
    except Exception as exc:
        logging.getLogger(__name__).exception("Pipeline failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
