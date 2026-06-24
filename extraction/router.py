"""
router.py — File type detection and routing for the extraction pipeline.

This is the entry point of the extraction assembly line. An investigator
uploads a bank statement file. Before we can extract data from it, we need
to know WHAT KIND of file it is so we can use the right tool to read it.

Why routing matters: A scanned PDF photograph of a bank statement is very
different from a computer-generated PDF. An Excel file has structured cells.
An image file needs OCR (Optical Character Recognition). This router inspects
each file and tells the pipeline which processing path to take.

The five routing paths are:
  1. excel_csv   — Excel or CSV file → read directly with pandas
  2. docx        — Word document → extract text with python-docx
  3. image       — JPG or PNG photograph → process with OCR
  4. pdf_digital — Computer-generated PDF with embedded text → pdfplumber
  5. pdf_scanned — Photographed/scanned PDF with no embedded text → OCR

Team: Survey Corps | CIDECODE Hackathon 2026 | CID Karnataka
"""

import logging
from pathlib import Path
from typing import Optional

import pdfplumber

from config.settings import DIGITAL_PDF_CHAR_THRESHOLD

# Set up a logger for this module. Log messages will include the module name
# so investigators can trace exactly which file produced each log line.
logger = logging.getLogger(__name__)


def route_file(file_path: str) -> str:
    """
    Inspects the uploaded file and returns a routing label that tells
    the pipeline which extraction method to use.

    The routing logic first checks the file extension. For PDF files,
    it goes one step further and actually opens the file to count how
    many characters of text are embedded inside it. A computer-generated
    PDF will have lots of text. A scanned/photographed PDF will have
    almost no text because the content is stored as pixels, not characters.

    Returns one of:
        "excel_csv"   — Excel or CSV file, read directly with pandas
        "docx"        — Word document, extract text with python-docx
        "image"       — JPG or PNG image, process with OCR pipeline
        "pdf_digital" — Computer-generated PDF with embedded text
        "pdf_scanned" — Scanned PDF with no embedded text, needs OCR

    Parameters:
        file_path (str): Absolute path to the uploaded file.

    Returns:
        str: One of the five routing labels above.

    Raises:
        ValueError: If the file extension is not in the supported list.
        FileNotFoundError: If the file does not exist at the given path.
    """
    # Convert the string path to a pathlib Path object so we can use
    # convenient methods like .suffix (which gives the file extension).
    path = Path(file_path)

    # Make sure the file actually exists before trying to open it.
    if not path.exists():
        logger.error("router.route_file: File not found at path: %s", file_path)
        raise FileNotFoundError(f"File not found: {file_path}")

    # Get the file extension in lowercase (e.g., ".PDF" becomes ".pdf")
    # so the comparison below works regardless of how the investigator named the file.
    extension = path.suffix.lower()

    logger.info("router.route_file: Routing file '%s' with extension '%s'", path.name, extension)

    # ── Excel and CSV files ───────────────────────────────────────────────────
    # These files already contain structured tabular data in rows and columns.
    # Pandas can read them directly without any text extraction or OCR.
    if extension in (".xlsx", ".xls", ".csv"):
        logger.info("router.route_file: Routed to 'excel_csv'")
        return "excel_csv"

    # ── Microsoft Word DOCX files ─────────────────────────────────────────────
    # Some banks provide statements as Word documents. We use python-docx
    # to extract text from paragraphs and tables.
    if extension == ".docx":
        logger.info("router.route_file: Routed to 'docx'")
        return "docx"

    # ── Plain-text statements (.txt) ──────────────────────────────────────────
    # Some banks export a fixed-width / space-aligned plain-text statement. It is
    # read as raw text and handed to the SAME deterministic text parser the digital
    # PDFs use, so it inherits the header/footer/id-column handling for free.
    if extension == ".txt":
        logger.info("router.route_file: Routed to 'text'")
        return "text"

    # ── Image files (photographs of bank statements) ──────────────────────────
    # An investigator may photograph a printed bank statement with their phone.
    # These go through the OCR pipeline (Tesseract → Groq Vision fallback).
    if extension in (".jpg", ".jpeg", ".png"):
        logger.info("router.route_file: Routed to 'image'")
        return "image"

    # ── PDF files — need deeper inspection ───────────────────────────────────
    # A PDF can be either:
    #   (a) Computer-generated (digital): Text is stored as actual characters
    #       inside the file. pdfplumber can extract this text directly.
    #   (b) Scanned/photographed: The content is stored as a pixel image.
    #       There is no embedded text. We must use OCR to read the pixels.
    #
    # To tell the difference, we open the PDF and count how many characters
    # of text we can extract from the first 3 pages. If we get more than
    # DIGITAL_PDF_CHAR_THRESHOLD (100) characters on average per page,
    # the PDF has real embedded text and is a digital PDF.
    if extension == ".pdf":
        return _classify_pdf(file_path)

    # ── Unsupported file type ─────────────────────────────────────────────────
    logger.error(
        "router.route_file: Unsupported file extension '%s' for file '%s'",
        extension,
        path.name,
    )
    raise ValueError(
        f"Unsupported file extension '{extension}'. "
        f"Supported types: .pdf, .xlsx, .xls, .csv, .docx, .txt, .jpg, .jpeg, .png"
    )


def _classify_pdf(file_path: str) -> str:
    """
    Opens a PDF file and determines whether it is a digital (computer-generated)
    PDF or a scanned (photographed/printed-and-scanned) PDF.

    The method: open the PDF, extract text from the first 3 pages, and calculate
    the average number of characters per page. If the average is above the
    threshold in settings.py, the PDF has real text and is classified as digital.
    If below the threshold, the PDF is a scanned image and needs OCR.

    Parameters:
        file_path (str): Absolute path to the PDF file.

    Returns:
        str: "pdf_digital" if text is embedded, "pdf_scanned" if no text found.
    """
    try:
        with pdfplumber.open(file_path) as pdf:
            # Examine up to the first 3 pages to decide the PDF type.
            # We use the first 3 pages because some PDFs have a cover page
            # with very little text before the transaction table begins.
            pages_to_check = min(3, len(pdf.pages))

            if pages_to_check == 0:
                # Empty PDF — treat as scanned since there is nothing to extract
                logger.warning(
                    "router._classify_pdf: PDF has no pages: %s", file_path
                )
                return "pdf_scanned"

            total_chars = 0
            for i in range(pages_to_check):
                page_text = pdf.pages[i].extract_text()
                # extract_text() returns None if the page has no embedded text,
                # so we use an empty string as the fallback to avoid errors.
                total_chars += len(page_text) if page_text else 0

            # Calculate the average number of extractable characters per page.
            average_chars_per_page = total_chars / pages_to_check

            logger.info(
                "router._classify_pdf: PDF '%s' has %.1f average chars/page across %d pages",
                Path(file_path).name,
                average_chars_per_page,
                pages_to_check,
            )

            # Compare to the threshold defined in settings.py (default: 100).
            # Above threshold → digital PDF with real embedded text.
            # At or below threshold → scanned image, needs OCR.
            if average_chars_per_page > DIGITAL_PDF_CHAR_THRESHOLD:
                logger.info("router._classify_pdf: Classified as 'pdf_digital'")
                return "pdf_digital"
            else:
                logger.info("router._classify_pdf: Classified as 'pdf_scanned'")
                return "pdf_scanned"

    except Exception as error:
        # If we cannot open the PDF at all, log the error and default to
        # the scanned path. The OCR pipeline can handle even badly formed PDFs
        # better than the digital text extractor can handle a non-text PDF.
        logger.error(
            "router._classify_pdf: Failed to inspect PDF '%s': %s. Defaulting to 'pdf_scanned'.",
            file_path,
            error,
        )
        return "pdf_scanned"
