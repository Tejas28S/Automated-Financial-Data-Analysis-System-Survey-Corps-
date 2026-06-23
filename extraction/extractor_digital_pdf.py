"""
extractor_digital_pdf.py — Text extraction from computer-generated (digital) PDFs.

When a bank generates a statement on their computer system and saves it as a PDF,
the text is stored digitally inside the file as actual characters. This is very
different from a scanned PDF where the content is just a photograph stored as pixels.

This module handles the "digital PDF" case. It uses pdfplumber — a Python library
specifically designed for reading PDF files — to extract all the text from every
page of the statement and combine it into one long string of text.

That combined text string is then passed to the column identifier (column_identifier.py)
which uses an LLM to figure out which column position holds the date, narration,
debit amount, credit amount, and running balance.

Team: Survey Corps | CIDECODE Hackathon 2026 | CID Karnataka
"""

import logging
import re
from pathlib import Path

import pdfplumber

# Set up a logger for this module.
# Logging lets us track the pipeline's progress without using print() statements.
logger = logging.getLogger(__name__)

# pdfplumber emits "(cid:NN)" when a PDF uses a font glyph it cannot map to a real
# character (very common for tab stops and fancy fonts in real bank statements,
# e.g. SBI prints "Account Number :(cid:9)0000..."). Left in, these artifacts break
# both the account-detail reader and the row parser. We replace them with a space.
_CID_ARTIFACT = re.compile(r"\(cid:\d+\)")


def _clean_pdf_text(text: str) -> str:
    """Removes (cid:NN) glyph artifacts so downstream parsing sees clean text."""
    if not text:
        return text
    return _CID_ARTIFACT.sub(" ", text)


def extract_text_from_digital_pdf(file_path: str) -> str:
    """
    Extracts all text from a digital PDF bank statement.

    Uses pdfplumber to read every page and combines all page texts
    into one continuous string. This text is then passed to the
    column identifier (Component 3) to determine the column structure.

    The function is resilient — if a single page fails to extract,
    it logs the error and continues with the remaining pages rather
    than crashing the entire pipeline.

    Parameters:
        file_path (str): Absolute path to the digital PDF file.

    Returns:
        str: Full extracted text from all pages combined,
             with pages separated by newline characters.
             Returns empty string if extraction fails completely.
    """
    logger.info(
        "extractor_digital_pdf.extract_text_from_digital_pdf: "
        "Starting extraction from '%s'",
        Path(file_path).name,
    )

    try:
        # Open the PDF file using pdfplumber's context manager.
        # The 'with' statement ensures the file is automatically closed
        # when we are done, even if an error occurs.
        with pdfplumber.open(file_path) as pdf:
            total_pages = len(pdf.pages)
            logger.info(
                "extractor_digital_pdf.extract_text_from_digital_pdf: "
                "PDF has %d page(s)",
                total_pages,
            )

            # We will collect the text from each page into this list,
            # then join them all together at the end.
            page_texts = []

            for page_number, page in enumerate(pdf.pages, start=1):
                try:
                    # extract_text() reads all the text characters on this page
                    # in reading order (left-to-right, top-to-bottom).
                    page_text = page.extract_text()

                    if page_text:
                        # Only add non-empty pages to the list.
                        page_texts.append(page_text)
                        logger.debug(
                            "extractor_digital_pdf.extract_text_from_digital_pdf: "
                            "Page %d: extracted %d characters",
                            page_number,
                            len(page_text),
                        )
                    else:
                        # Some pages (like blank separator pages) have no text.
                        # This is normal — we just skip them.
                        logger.debug(
                            "extractor_digital_pdf.extract_text_from_digital_pdf: "
                            "Page %d: no text found (blank or image-only page)",
                            page_number,
                        )

                except Exception as page_error:
                    # If one page fails, log the error and continue with the next page.
                    # A single bad page should not stop us from extracting the rest.
                    logger.warning(
                        "extractor_digital_pdf.extract_text_from_digital_pdf: "
                        "Page %d extraction failed: %s. Skipping this page.",
                        page_number,
                        page_error,
                    )
                    continue

            # Join all page texts together with newline characters between them.
            # This gives us one continuous string representing the entire document.
            # Clean glyph artifacts so every downstream step sees readable text.
            combined_text = _clean_pdf_text("\n".join(page_texts))

            logger.info(
                "extractor_digital_pdf.extract_text_from_digital_pdf: "
                "Extraction complete. Total characters extracted: %d",
                len(combined_text),
            )

            return combined_text

    except FileNotFoundError:
        logger.error(
            "extractor_digital_pdf.extract_text_from_digital_pdf: "
            "File not found: %s",
            file_path,
        )
        return ""

    except Exception as error:
        # Catch-all for any unexpected errors (corrupted PDF, permission denied, etc.)
        logger.error(
            "extractor_digital_pdf.extract_text_from_digital_pdf: "
            "Unexpected error reading PDF '%s': %s",
            file_path,
            error,
        )
        return ""
