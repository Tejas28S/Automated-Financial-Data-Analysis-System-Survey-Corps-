"""
extraction/__init__.py — Package initialiser for the extraction module.

This file marks the extraction/ directory as a Python package so that
other parts of the system can import from it using:
    from extraction.router import route_file
    from extraction.extraction_pipeline import run_extraction_pipeline

The extraction package is the first processing phase of the
Multi-Accused Cross-Account Investigation Engine. It converts any
bank statement file (PDF, Excel, CSV, DOCX, image) into a single
unified pandas DataFrame ready for fraud analysis.

Team: Survey Corps | CIDECODE Hackathon 2026 | CID Karnataka
"""
