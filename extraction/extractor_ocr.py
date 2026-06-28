"""
extractor_ocr.py — OCR-based text extraction from scanned PDFs and photographed images.

This is the most complex component in the extraction pipeline. It handles bank
statements that are NOT stored as digital text — instead, the content exists only
as a photograph or scan:

  1. A bank sends a printed statement and the investigator scans it on a photocopier.
  2. An investigator photographs a physical bank passbook with their mobile phone.
  3. An older bank generates a scanned PDF from a printed document.

In all these cases, the "text" we see is actually just pixels in an image.
To read it, we must use OCR (Optical Character Recognition) — a technology
that looks at the shape of letters and words in an image and converts them to
actual text characters.

TWO-TIER OCR APPROACH:
    This module uses two OCR engines in sequence:

    TIER 1 — Tesseract OCR (fast, local, no cost):
        Tesseract is an open-source OCR engine developed by Google. It runs
        entirely on the local machine with no API calls and no cost. It is
        very accurate for clean, well-lit, straight scans of text documents.
        Tesseract gives each recognised word a confidence score (0–100).
        If the average confidence is ≥ 80 (the threshold in settings.py),
        the Tesseract output is accepted.

    TIER 2 — Groq Llama 4 Scout Vision (fallback for low-quality images):
        If Tesseract's average confidence falls below 80, the image is likely
        blurry, photographed at an angle, has shadows, or has poor lighting.
        In these cases, we fall back to Groq's vision model — which can read
        text from difficult images. Groq is called via API (requires internet)
        and is only used when Tesseract fails. This uses the GROQ2 key — its own
        key, kept separate from the GROQ1 text key so the two never compete for
        the same rate limit.

PRIVACY NOTE:
    Sending an image to Groq Vision is the ONE place raw bank data leaves the
    machine, and it is unavoidable (the model must see the image to read it).
    We therefore limit it strictly to the low-confidence fallback. Clean images
    stay fully local via Tesseract and never leave the machine.

WHY THIS APPROACH:
    - Most properly scanned statements will pass Tesseract with confidence ≥ 80.
      These are processed locally with no API cost.
    - Only the hard cases (photographed, blurry, angled) go to Groq Vision.
    - This balances cost, speed, accuracy, and privacy.

IMAGE PREPROCESSING (before Tesseract):
    Before running Tesseract, we apply several image improvement steps using
    OpenCV (a computer vision library):
        1. Convert to grayscale (remove colour information that confuses OCR)
        2. Check lighting uniformity (to choose the right binarisation method)
        3. Apply thresholding (convert grey pixels to black or white)
        4. Deskew if the image is tilted more than 0.5 degrees
        5. Denoise using bilateral filtering (removes grain while preserving edges)
        6. Sharpen if the image is blurry

Team: Survey Corps | CIDECODE Hackathon 2026 | CID Karnataka
"""

import base64
import logging
import os
import time
import tempfile
from pathlib import Path
from typing import Dict

import cv2
import numpy as np
import pytesseract
from groq import Groq

from config.settings import (
    TESSERACT_CMD,
    TESSERACT_CONFIDENCE_THRESHOLD,
    GROQ2_KEY,  # Vision OCR fallback uses the GROQ2 key only (see INSTRUCTIONS.md §7)
    GROQ_VISION_MODEL,
)

# Set up a logger for this module.
logger = logging.getLogger(__name__)

# ── Configure Tesseract executable path ──────────────────────────────────────
# pytesseract needs to know where the Tesseract binary is installed.
# On macOS (Homebrew): /opt/homebrew/bin/tesseract
# On Windows: C:\Program Files\Tesseract-OCR\tesseract.exe
# On Linux: /usr/bin/tesseract
# The path is set in settings.py, not hardcoded here.
pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

# ── Groq Vision retry settings ────────────────────────────────────────────────
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 2

# ── Image-size bounds (keep OCR fast, in-memory, and within Groq's limits) ────
# A scanned page rendered at 300 DPI can be enormous (e.g. 14617×10338 px ≈ 150 MP),
# which (a) makes Tesseract's float64 preprocessing run out of memory, (b) is far
# slower than necessary, and (c) overflows Groq Vision's request-size limit (413
# "Request Entity Too Large"). We render at a moderate DPI and cap the longest side.
# This is the single biggest win for scanned-PDF speed and reliability.
OCR_RENDER_DPI = 200          # render DPI for scanned pages (300 was overkill)
OCR_MAX_DIM = 2200            # cap longest side (px) before Tesseract preprocessing
VISION_MAX_DIM = 1600         # downscale further before sending to Groq Vision
VISION_JPEG_QUALITY = 80      # JPEG (not PNG) shrinks the request payload a lot


def _is_nonretryable(err) -> bool:
    """
    Errors that will never succeed on retry — stop immediately instead of burning
    the retry budget (and the user's daily token quota) on them.

    413 = payload too large (body size fixed, re-send identical body → identical fail).
    429 TPD = daily token quota exhausted (resets tomorrow, not in 2 seconds).
    Note: 429 TPM (per-minute) IS retryable after a wait, but the message text says
    "tokens per day" for TPD — we detect that specifically so minute-rate 429s still retry.
    """
    s = str(err).lower()
    if "413" in s or "too large" in s or "entity too large" in s or "request_too_large" in s:
        return True
    # Daily quota exhausted — retrying in 2 s changes nothing; wait until tomorrow.
    if "429" in s and ("per day" in s or "tokens per day" in s or "tpd" in s):
        return True
    return False


def _bounded_jpeg_data_url(image_path: str, max_dim: int = VISION_MAX_DIM,
                           quality: int = VISION_JPEG_QUALITY) -> str:
    """
    Loads an image, downscales it so its longest side is <= max_dim, and returns a
    base64 JPEG data URL small enough to stay under Groq Vision's request limit.
    Falls back to the raw file bytes if OpenCV cannot read/encode it.
    """
    try:
        img = cv2.imread(image_path)
        if img is not None:
            h, w = img.shape[:2]
            longest = max(h, w)
            if longest > max_dim:
                scale = max_dim / float(longest)
                img = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))),
                                 interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
            if ok:
                return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode("utf-8")
    except Exception as e:
        logger.warning("extractor_ocr._bounded_jpeg_data_url: downscale failed (%s); using raw bytes.", e)
    with open(image_path, "rb") as f:
        return "data:image/jpeg;base64," + base64.b64encode(f.read()).decode("utf-8")

# ── Groq Vision prompt ─────────────────────────────────────────────────────────
# This is the exact instruction sent to Groq when we use it as OCR fallback.
# It instructs the model to behave as a pure OCR tool, not as a conversational AI.
GROQ_OCR_PROMPT = (
    "You are a bank statement OCR engine. "
    "Extract all text from this bank statement image exactly as it appears. "
    "Preserve the table structure. "
    "Include every row — date, narration/description, debit amount, credit amount, and balance. "
    "Do not add any commentary, headings, or explanation. "
    "Output only the raw extracted text."
)


def run_tesseract_on_image(image_path: str) -> Dict:
    """
    Runs Tesseract OCR on a single image file.

    Uses pytesseract with LSTM neural engine (--oem 1) for best accuracy.
    Applies multiple preprocessing steps before OCR:
        1. Convert to grayscale
        2. Check if lighting is uniform (histogram bimodal check)
        3. Apply Otsu thresholding if uniform, adaptive thresholding if not
        4. Deskew if skew angle > 0.5 degrees
        5. Denoise using bilateral filtering
        6. Sharpen if image is blurry (Laplacian variance < 100)

    Parameters:
        image_path (str): Absolute path to the image file.

    Returns:
        dict: {
            "text": str,           # extracted text
            "confidence": float,   # average word-level confidence 0-100
            "word_count": int      # number of words extracted
        }
    """
    logger.info(
        "extractor_ocr.run_tesseract_on_image: Processing '%s'",
        Path(image_path).name,
    )

    try:
        # ── Load the image using OpenCV ────────────────────────────────────
        # cv2.imread loads the image as a NumPy array of BGR pixel values.
        image = cv2.imread(image_path)
        if image is None:
            raise ValueError(f"OpenCV could not read image at '{image_path}'")

        # ── Step 0: Cap the size ────────────────────────────────────────────
        # Defensive OOM guard: the downstream float64 filters (Laplacian, bilateral)
        # allocate arrays the size of the image, so a 150 MP scan would need >1 GB
        # each. Cap the longest side so preprocessing stays fast and in-memory.
        h, w = image.shape[:2]
        if max(h, w) > OCR_MAX_DIM:
            scale = OCR_MAX_DIM / float(max(h, w))
            image = cv2.resize(image, (max(1, int(w * scale)), max(1, int(h * scale))),
                               interpolation=cv2.INTER_AREA)
            logger.info("extractor_ocr.run_tesseract_on_image: downscaled %dx%d -> %dx%d for OCR.",
                        w, h, image.shape[1], image.shape[0])

        # ── Step 1: Convert to grayscale ────────────────────────────────────
        # OCR works on the brightness of pixels, not their colour.
        # Converting to grayscale reduces the data from 3 channels (BGR)
        # to 1 channel (brightness), which improves both speed and accuracy.
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

        # ── Step 2: Check lighting uniformity using histogram analysis ──────
        # A bimodal histogram (two peaks: one for dark text, one for light background)
        # means the lighting is uniform — Otsu's method works best here.
        # A non-bimodal histogram (uneven lighting, shadows) means we should
        # use adaptive thresholding instead.
        histogram = cv2.calcHist([gray], [0], None, [256], [0, 256])
        # Calculate the standard deviation of the histogram to measure how spread out
        # the pixel intensities are. High spread = more uniform lighting.
        hist_std = np.std(histogram)
        is_uniform_lighting = hist_std > 1000  # Empirically derived threshold

        # ── Step 3: Apply thresholding (convert grey → black and white) ────
        # Thresholding converts each pixel to either pure black or pure white.
        # This is necessary for OCR because Tesseract works best on binary images.
        if is_uniform_lighting:
            # Otsu's method automatically finds the optimal threshold value
            # for images with two clear clusters of pixel values (dark text on light background).
            _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            logger.debug(
                "extractor_ocr.run_tesseract_on_image: "
                "Used Otsu thresholding (uniform lighting detected)"
            )
        else:
            # Adaptive thresholding calculates a different threshold for each
            # small region of the image, handling uneven lighting and shadows.
            binary = cv2.adaptiveThreshold(
                gray, 255,
                cv2.ADAPTIVE_THRESH_GAUSSIAN_C,  # Uses Gaussian-weighted neighbourhood
                cv2.THRESH_BINARY,
                11,   # Size of the neighbourhood block (must be odd)
                2,    # Constant subtracted from the mean (fine-tunes the threshold)
            )
            logger.debug(
                "extractor_ocr.run_tesseract_on_image: "
                "Used adaptive thresholding (non-uniform lighting detected)"
            )

        # ── Step 4: Deskew if the image is tilted ───────────────────────────
        # A tilted image (even slightly — 2-3 degrees) significantly reduces
        # Tesseract's accuracy. We detect the skew angle using the Hough
        # Transform (a method for detecting lines) and rotate the image to
        # straighten it if the skew is more than 0.5 degrees.
        binary = _deskew_image(binary)

        # ── Step 5: Denoise using bilateral filtering ───────────────────────
        # Scanned documents often have noise (random black/white speckles).
        # Bilateral filtering removes noise while preserving sharp text edges.
        # d=9: diameter of pixel neighbourhood
        # sigmaColor=75: filter strength for colour similarity
        # sigmaSpace=75: filter strength for spatial proximity
        denoised = cv2.bilateralFilter(binary, d=9, sigmaColor=75, sigmaSpace=75)

        # ── Step 6: Sharpen if the image is blurry ──────────────────────────
        # We measure blur using the Laplacian variance. A sharp image has
        # high variance (lots of edges). A blurry image has low variance.
        # If the variance is below 100, the image is considered blurry and
        # we apply an unsharp masking filter to enhance edges.
        laplacian_variance = cv2.Laplacian(denoised, cv2.CV_64F).var()
        if laplacian_variance < 100:
            # Unsharp masking: subtract a blurred version from the original
            # to enhance edges and fine details.
            blurred = cv2.GaussianBlur(denoised, (0, 0), 3)
            sharpened = cv2.addWeighted(denoised, 1.5, blurred, -0.5, 0)
            processed = sharpened
            logger.debug(
                "extractor_ocr.run_tesseract_on_image: "
                "Applied sharpening (Laplacian variance: %.1f < 100)",
                laplacian_variance,
            )
        else:
            processed = denoised

        # ── Run Tesseract OCR on the preprocessed image ─────────────────────
        # --oem 1 selects the LSTM neural network engine (most accurate)
        # --psm 6 tells Tesseract to assume the image is a uniform block of text
        #         (like a table) rather than a single word or mixed document.
        config = "--oem 1 --psm 6"
        extracted_text = pytesseract.image_to_string(processed, config=config)

        # Get word-level confidence scores from Tesseract.
        # image_to_data returns a TSV (tab-separated values) table with confidence
        # scores for each recognised word. We parse this to get the average confidence.
        data = pytesseract.image_to_data(processed, config=config, output_type=pytesseract.Output.DICT)

        # Filter to only words that were actually recognised (confidence > 0).
        # Tesseract assigns -1 to tokens that are not individual words (like spaces).
        valid_confidences = [
            conf for conf in data["conf"]
            if isinstance(conf, (int, float)) and conf > 0
        ]

        if valid_confidences:
            average_confidence = sum(valid_confidences) / len(valid_confidences)
        else:
            # No words were recognised at all — confidence is 0.
            average_confidence = 0.0

        word_count = len(valid_confidences)

        logger.info(
            "extractor_ocr.run_tesseract_on_image: "
            "Tesseract complete. Words: %d, Average confidence: %.1f%%",
            word_count,
            average_confidence,
        )

        return {
            "text": extracted_text,
            "confidence": average_confidence,
            "word_count": word_count,
        }

    except Exception as error:
        logger.error(
            "extractor_ocr.run_tesseract_on_image: "
            "Error processing image '%s': %s",
            image_path,
            error,
        )
        return {"text": "", "confidence": 0.0, "word_count": 0}


def _deskew_image(binary_image: np.ndarray) -> np.ndarray:
    """
    Detects and corrects the tilt (skew) of a scanned document image.

    If the document is on a scanner and placed slightly crooked, the text
    lines will not be perfectly horizontal. Even a 2-degree tilt can cause
    Tesseract to misread words. This function detects the dominant angle
    of text lines and rotates the image to correct the tilt.

    Parameters:
        binary_image (np.ndarray): Preprocessed binary (black/white) image.

    Returns:
        np.ndarray: Deskewed (straightened) image.
                    Returns the original image if no significant skew is detected
                    or if deskewing fails.
    """
    try:
        # Find all white pixels in the binary image (text pixels are white
        # after Otsu thresholding inverts the image).
        # np.column_stack creates an array of (x, y) coordinate pairs.
        coords = np.column_stack(np.where(binary_image > 0))

        if len(coords) < 10:
            # Not enough pixels to calculate skew — return unchanged.
            return binary_image

        # minAreaRect finds the smallest rectangle that encloses all white pixels.
        # The angle of this rectangle tells us the dominant text orientation.
        angle = cv2.minAreaRect(coords)[-1]

        # cv2.minAreaRect returns angles in the range [-90, 0).
        # We convert this to the actual rotation needed:
        # If angle < -45, the text is more horizontal → add 90 to get the skew.
        if angle < -45:
            angle = -(90 + angle)
        else:
            angle = -angle

        # Only deskew if the angle is more than 0.5 degrees.
        # Correcting a smaller angle introduces more interpolation artefacts
        # than the skew itself causes OCR errors.
        if abs(angle) < 0.5:
            return binary_image

        logger.debug(
            "extractor_ocr._deskew_image: Correcting skew of %.2f degrees", angle
        )

        # Rotate the image around its centre by the negative of the skew angle.
        height, width = binary_image.shape[:2]
        centre = (width // 2, height // 2)
        rotation_matrix = cv2.getRotationMatrix2D(centre, angle, scale=1.0)
        deskewed = cv2.warpAffine(
            binary_image,
            rotation_matrix,
            (width, height),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_REPLICATE,
        )
        return deskewed

    except Exception as deskew_error:
        logger.debug(
            "extractor_ocr._deskew_image: Deskewing failed: %s. Using original image.",
            deskew_error,
        )
        return binary_image


def run_groq_vision_on_image(image_path: str) -> str:
    """
    Sends an image to Groq's Llama 4 Scout Vision model for OCR.

    Used as a fallback when Tesseract confidence is below 80%.
    Groq Vision can read blurry, shadowed, curved, and low-quality images
    that Tesseract cannot handle reliably.

    Uses the GROQ2 key — the dedicated vision key, separate from the GROQ1
    text key used for column identification.

    The image is base64-encoded and sent as a data URL inside the chat
    message content. Groq's vision API follows the OpenAI-compatible format.

    WHAT IS SENT TO GROQ:
        - The image file (base64-encoded as a data URL)
        - The OCR prompt defined at the top of this file
    WHAT IS NOT SENT TO GROQ:
        - No account holder names or metadata (only the image itself)

    Parameters:
        image_path (str): Absolute path to the image file.

    Returns:
        str: Extracted text from Groq Vision, preserving table layout.
             Returns empty string on API failure after all retries.
    """
    from extraction.key_pool import VISION_POOL, is_daily_quota_error, AllKeysExhausted
    try:
        client, _pool_key = VISION_POOL.client()
    except AllKeysExhausted:
        # No vision key => the fallback genuinely cannot run. Fail loudly.
        raise RuntimeError(
            "No usable GROQ vision key found in .env — add GROQ2 (and optionally GROQ5) "
            "for the blurry-image OCR fallback."
        )

    # Downscale + JPEG-encode so the request stays under Groq Vision's size limit
    # (a full-resolution scan triggers HTTP 413 "Request Entity Too Large").
    data_url = _bounded_jpeg_data_url(image_path)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.warning(
                "extractor_ocr.run_groq_vision_on_image: "
                "Falling back to Groq Vision (attempt %d of %d) for '%s'",
                attempt,
                MAX_RETRIES,
                Path(image_path).name,
            )

            response = client.chat.completions.create(
                model=GROQ_VISION_MODEL,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": data_url},
                            },
                            {
                                "type": "text",
                                "text": GROQ_OCR_PROMPT,
                            },
                        ],
                    }
                ],
                max_tokens=4096,
            )

            extracted_text = response.choices[0].message.content.strip()

            logger.info(
                "extractor_ocr.run_groq_vision_on_image: "
                "Groq Vision extracted %d characters from '%s'",
                len(extracted_text),
                Path(image_path).name,
            )

            return extracted_text

        except Exception as groq_error:
            logger.warning(
                "extractor_ocr.run_groq_vision_on_image: "
                "Groq Vision attempt %d failed for '%s': %s",
                attempt,
                Path(image_path).name,
                groq_error,
            )
            if is_daily_quota_error(groq_error):
                VISION_POOL.mark_dead(_pool_key)
                try:
                    client, _pool_key = VISION_POOL.client()
                    logger.info("extractor_ocr: rotated to a fresh vision key — retrying.")
                    continue
                except AllKeysExhausted:
                    logger.error("extractor_ocr: all vision keys exhausted — stopping.")
                    break
            if _is_nonretryable(groq_error):
                logger.error(
                    "extractor_ocr.run_groq_vision_on_image: non-retryable error "
                    "(payload too large) — not retrying '%s'.", Path(image_path).name)
                break
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)

    logger.error(
        "extractor_ocr.run_groq_vision_on_image: "
        "All %d Groq Vision attempts failed for '%s'. Returning empty string.",
        MAX_RETRIES,
        Path(image_path).name,
    )
    return ""


def extract_text_from_scanned_pdf(file_path: str, max_pages: int = None) -> str:
    """
    Extracts text from a scanned PDF by converting each page to an image
    and running the OCR pipeline on each page image.

    Uses PyMuPDF (fitz) to rasterize each page at 300 DPI. At 300 DPI,
    text is large enough for Tesseract to read accurately. Lower resolutions
    (like 72 DPI, the default screen resolution) produce images too small
    for accurate OCR.

    Then runs the tiered OCR pipeline (Tesseract → Groq Vision fallback)
    on each page image.

    SAFETY BOUND: each low-confidence page costs one Groq Vision call, and OCR
    over a multi-hundred-page file is exactly the kind of heavy run that has
    overheated the laptop before. `max_pages` caps how many pages are OCR'd
    (None = all). Tests pass a small cap to stay bounded.

    Parameters:
        file_path (str): Absolute path to the scanned PDF file.
        max_pages (int): If set, only OCR the first `max_pages` pages.

    Returns:
        tuple: (combined_text, audit) where audit is the per-file OCR record
               described in _summarise_ocr_audit(). Returns ("", empty-audit)
               if the PDF cannot be processed.
    """
    logger.info(
        "extractor_ocr.extract_text_from_scanned_pdf: "
        "Processing scanned PDF '%s'",
        Path(file_path).name,
    )

    try:
        import fitz  # PyMuPDF — for rasterising PDF pages into images
    except ImportError:
        logger.error(
            "extractor_ocr.extract_text_from_scanned_pdf: "
            "PyMuPDF (fitz) is not installed. "
            "Install it with: pip install pymupdf"
        )
        return "", _summarise_ocr_audit([])

    all_page_texts = []
    page_audits = []  # one record per page: which engine read it + confidence

    try:
        # Open the PDF using PyMuPDF.
        pdf_document = fitz.open(file_path)
        total_pages = len(pdf_document)
        # Apply the safety bound: never OCR more than max_pages if one is set.
        pages_to_process = total_pages if max_pages is None else min(total_pages, max_pages)
        logger.info(
            "extractor_ocr.extract_text_from_scanned_pdf: "
            "PDF has %d page(s); OCR'ing %d.",
            total_pages,
            pages_to_process,
        )

        for page_number in range(pages_to_process):
            try:
                page = pdf_document[page_number]

                # Render at a moderate DPI, but CAP the longest side so a large page
                # can never exhaust memory (Tesseract's float64 preprocessing) or
                # exceed Groq Vision's request-size limit. A standard statement at
                # 200 DPI is well within OCR_MAX_DIM; only unusually large mediaboxes
                # get scaled down further.
                rect = page.rect
                longest_pt = max(rect.width, rect.height) or 1.0
                zoom = OCR_RENDER_DPI / 72.0
                if longest_pt * zoom > OCR_MAX_DIM:
                    zoom = OCR_MAX_DIM / longest_pt
                transform_matrix = fitz.Matrix(zoom, zoom)

                # Render the PDF page as a pixel image (rasterise).
                pixmap = page.get_pixmap(matrix=transform_matrix)

                # Save the rendered page as a temporary PNG file.
                # IMPORTANT (cross-platform): we do NOT use NamedTemporaryFile here.
                # On Windows that keeps an OPEN handle on the file, and PyMuPDF's
                # pixmap.save() then cannot write to the same path ("cannot remove
                # file … Permission denied") — so every scanned page was silently
                # skipped on Windows. Instead we save into a fresh temp DIRECTORY
                # under a brand-new filename (nothing to overwrite, no handle held),
                # which behaves identically on Windows, macOS and Linux. The Groq
                # Vision fallback also needs a real file on disk, so a temp file
                # (not an in-memory array) is the right shared representation.
                tmp_dir = tempfile.mkdtemp(prefix="sc_ocr_")
                tmp_path = os.path.join(tmp_dir, f"page_{page_number + 1}.png")
                try:
                    pixmap.save(tmp_path)

                    logger.info(
                        "extractor_ocr.extract_text_from_scanned_pdf: "
                        "Rendered page %d/%d to temporary image",
                        page_number + 1,
                        total_pages,
                    )

                    # Run the tiered OCR pipeline on this page image.
                    page_result = _process_image_with_tiered_ocr(tmp_path)
                    all_page_texts.append(page_result["text"])
                    page_audits.append({
                        "page": page_number + 1,
                        "path": page_result["path"],
                        "confidence": round(page_result["confidence"], 1),
                    })
                finally:
                    # Always clean up the temp image and its directory.
                    Path(tmp_path).unlink(missing_ok=True)
                    try:
                        os.rmdir(tmp_dir)
                    except OSError:
                        pass

            except Exception as page_error:
                logger.warning(
                    "extractor_ocr.extract_text_from_scanned_pdf: "
                    "Failed to process page %d: %s. Skipping.",
                    page_number + 1,
                    page_error,
                )
                continue

        pdf_document.close()

    except Exception as pdf_error:
        logger.error(
            "extractor_ocr.extract_text_from_scanned_pdf: "
            "Failed to open scanned PDF '%s': %s",
            file_path,
            pdf_error,
        )
        return "", _summarise_ocr_audit([])

    combined_text = "\n".join(all_page_texts)
    logger.info(
        "extractor_ocr.extract_text_from_scanned_pdf: "
        "Completed. Total characters extracted: %d",
        len(combined_text),
    )
    return combined_text, _summarise_ocr_audit(page_audits)


def _process_image_with_tiered_ocr(image_path: str) -> Dict:
    """
    Applies the two-tier OCR pipeline to a single image and reports which tier won.

    Tier 1: Run Tesseract with preprocessing, read its confidence.
    Tier 2: If Tesseract confidence < 80%, fall back to Groq Vision.

    If Groq Vision also fails, return the Tesseract output regardless of
    confidence (it is better than nothing) and log a warning.

    The decision to escalate is driven purely by the Tesseract confidence score
    the engine itself produces — not guesswork — so it is fully explainable.

    Parameters:
        image_path (str): Absolute path to the image file.

    Returns:
        dict: {
            "text": str,            # best available text for this image
            "path": str,            # which engine produced it (see below)
            "confidence": float,    # Tesseract's own confidence for this image
        }
        path is one of:
            "tesseract"            → Tesseract was confident (≥ threshold)
            "groq_vision"          → low confidence → Groq Vision read it
            "tesseract_low_kept"   → low confidence AND Vision failed → kept Tesseract
    """
    # Tier 1: Try Tesseract first
    tesseract_result = run_tesseract_on_image(image_path)
    tesseract_text = tesseract_result["text"]
    tesseract_confidence = tesseract_result["confidence"]

    if tesseract_confidence >= TESSERACT_CONFIDENCE_THRESHOLD:
        logger.info(
            "extractor_ocr._process_image_with_tiered_ocr: "
            "Tesseract confidence %.1f%% >= %.1f%% threshold. Using Tesseract output.",
            tesseract_confidence,
            TESSERACT_CONFIDENCE_THRESHOLD,
        )
        return {"text": tesseract_text, "path": "tesseract", "confidence": tesseract_confidence}

    # Tier 2: Tesseract confidence too low — fall back to Groq Vision
    logger.warning(
        "extractor_ocr._process_image_with_tiered_ocr: "
        "Tesseract confidence %.1f%% < %.1f%% threshold. "
        "Falling back to Groq Vision (GROQ2).",
        tesseract_confidence,
        TESSERACT_CONFIDENCE_THRESHOLD,
    )

    groq_text = run_groq_vision_on_image(image_path)

    if groq_text:
        return {"text": groq_text, "path": "groq_vision", "confidence": tesseract_confidence}

    # Both Tesseract and Groq Vision failed — return Tesseract output as last resort
    logger.warning(
        "extractor_ocr._process_image_with_tiered_ocr: "
        "Groq Vision also failed. Returning Tesseract output despite low confidence (%.1f%%).",
        tesseract_confidence,
    )
    return {"text": tesseract_text, "path": "tesseract_low_kept", "confidence": tesseract_confidence}


def _summarise_ocr_audit(page_audits: list) -> Dict:
    """
    Rolls per-page OCR records up into one per-file summary for the run metadata.

    This is what makes Problem 2 visible: for any scanned file the team can see,
    in the stored metadata, exactly which engine read each page and the Tesseract
    confidence that drove the decision.

    Parameters:
        page_audits (list[dict]): one {"page", "path", "confidence"} per page.

    Returns:
        dict: {
            "engine": "tesseract" | "groq_vision" | "mixed" | "none",
            "pages": [...the per-page records...],
            "tesseract_confidence_avg": float,
            "vision_pages": int,   # how many pages needed the Groq Vision fallback
        }
    """
    if not page_audits:
        return {"engine": "none", "pages": [], "tesseract_confidence_avg": 0.0, "vision_pages": 0}

    paths = {p["path"] for p in page_audits}
    vision_pages = sum(1 for p in page_audits if p["path"] == "groq_vision")
    avg_conf = sum(p["confidence"] for p in page_audits) / len(page_audits)

    # Label the file as a whole: all-Tesseract, all-Vision, or a mix of both.
    if paths <= {"tesseract"}:
        engine = "tesseract"
    elif vision_pages == len(page_audits):
        engine = "groq_vision"
    else:
        engine = "mixed"

    return {
        "engine": engine,
        "pages": page_audits,
        "tesseract_confidence_avg": round(avg_conf, 1),
        "vision_pages": vision_pages,
    }


def extract_text_with_ocr_audit(file_path: str, max_pages: int = None) -> tuple:
    """
    Main entry point for OCR extraction — returns text AND a visibility audit.

    Determines whether the input is a scanned PDF or a direct image, then routes
    to the correct sub-function, and reports which OCR engine read it.

    For images (.jpg, .jpeg, .png):
        Step 1 — Run Tesseract pipeline with preprocessing, get text + confidence
        Step 2 — If confidence >= 80.0, use Tesseract text
        Step 3 — If confidence < 80.0, fall back to Groq Vision (GROQ2)

    For scanned PDFs:
        Convert each page to image at 300 DPI, then apply the same
        Tesseract → Groq Vision tiered logic per page.

    Parameters:
        file_path (str): Absolute path to image (.jpg/.jpeg/.png) or scanned PDF.

    Returns:
        tuple: (text, audit). `audit` is the per-file OCR record from
               _summarise_ocr_audit() so the run metadata can show, per file,
               which engine read it and at what confidence. On unsupported input
               returns ("", empty-audit).
    """
    extension = Path(file_path).suffix.lower()

    logger.info(
        "extractor_ocr.extract_text_with_ocr_audit: Starting OCR for '%s'",
        Path(file_path).name,
    )

    if extension == ".pdf":
        # This is a scanned PDF — convert each page to image first, then OCR.
        return extract_text_from_scanned_pdf(file_path, max_pages=max_pages)

    elif extension in (".jpg", ".jpeg", ".png"):
        # This is a direct image file — apply tiered OCR directly.
        result = _process_image_with_tiered_ocr(file_path)
        audit = _summarise_ocr_audit([
            {"page": 1, "path": result["path"], "confidence": round(result["confidence"], 1)}
        ])
        return result["text"], audit

    else:
        logger.error(
            "extractor_ocr.extract_text_with_ocr_audit: "
            "Unsupported extension '%s' for OCR processing.",
            extension,
        )
        return "", _summarise_ocr_audit([])


def extract_text_from_image_or_scanned_pdf(file_path: str) -> str:
    """
    Backward-compatible text-only wrapper around extract_text_with_ocr_audit().

    Returns just the extracted text (no audit). The pipeline uses the audit
    version; this thin wrapper keeps any older callers/tests working.
    """
    text, _audit = extract_text_with_ocr_audit(file_path)
    return text
