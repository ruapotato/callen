# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""
Email attachment extraction.

Walks a MIME message, saves each attachment to disk, and extracts
text from images (OCR via Tesseract) and PDFs (pdfplumber). The
extracted text is appended to the email body so every downstream
layer — the regex injection scanner, the Mistral preflight
classifier, and the Claude agent — sees it without needing to
load the raw binary.
"""

import email.message
import email.utils
import hashlib
import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

ATTACHMENT_DIR = Path("./attachments")

# Hard limits so a malicious attachment can't exhaust disk or OCR workers
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024  # 20 MB per file
MAX_EXTRACT_BYTES = 5 * 1024 * 1024       # Skip OCR on files > 5 MB
MAX_EXTRACTED_CHARS = 20_000              # Truncate OCR output

IMAGE_CONTENT_TYPES = {
    "image/png", "image/jpeg", "image/jpg", "image/gif",
    "image/bmp", "image/tiff", "image/webp", "image/x-ms-bmp",
}

PDF_CONTENT_TYPES = {"application/pdf", "application/x-pdf"}

TEXT_CONTENT_TYPES = {
    "text/plain", "text/csv", "text/html", "text/x-log",
    "application/x-log", "application/json",
}


def _safe_filename(raw: str, fallback: str) -> str:
    """Sanitize a filename so it's safe to write to disk."""
    if not raw:
        raw = fallback
    # Strip any path components and keep only the basename
    raw = os.path.basename(raw)
    # Replace anything non-word-ish with underscores, keeping dots
    cleaned = "".join(c if (c.isalnum() or c in "._-") else "_" for c in raw)
    if not cleaned:
        cleaned = fallback
    return cleaned[:200]


def _ocr_image(path: str) -> tuple[str, str]:
    """Return (text, method). Empty text on failure."""
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return ("", "ocr_unavailable")
    try:
        img = Image.open(path)
        text = pytesseract.image_to_string(img)
        return (text.strip(), "tesseract")
    except Exception as e:
        log.warning("OCR failed for %s: %s", path, e)
        return ("", f"ocr_error:{type(e).__name__}")


def _extract_pdf(path: str) -> tuple[str, str]:
    """Return (text, method). Empty text on failure."""
    try:
        import pdfplumber
    except ImportError:
        return ("", "pdf_unavailable")
    try:
        with pdfplumber.open(path) as pdf:
            pages = []
            for page in pdf.pages:
                t = page.extract_text() or ""
                if t:
                    pages.append(t)
        return ("\n\n".join(pages).strip(), "pdfplumber")
    except Exception as e:
        log.warning("PDF extract failed for %s: %s", path, e)
        return ("", f"pdf_error:{type(e).__name__}")


def _extract_text_file(path: str) -> tuple[str, str]:
    try:
        with open(path, "rb") as f:
            data = f.read(MAX_EXTRACTED_CHARS * 2)
        return (data.decode("utf-8", errors="replace").strip(), "text")
    except Exception as e:
        return ("", f"text_error:{type(e).__name__}")


def extract_attachments(
    msg: email.message.Message,
    email_id: int,
    message_id: str,
) -> list[dict]:
    """Walk the message, save attachments, and extract their text.

    Returns a list of dicts with filename, content_type, file_path,
    size_bytes, extracted_text, extraction_method. Caller persists
    these rows and appends the extracted text to the email body so
    downstream filters (regex + Mistral + Claude) see it too.
    """
    if not msg.is_multipart():
        return []

    ATTACHMENT_DIR.mkdir(parents=True, exist_ok=True)

    # Sub-directory per email so we can clean up easily
    # Use a short hash of message_id to avoid filesystem-unsafe chars
    email_slug = hashlib.sha1(
        (message_id or f"email-{email_id}").encode()
    ).hexdigest()[:12]
    email_dir = ATTACHMENT_DIR / f"{email_id:06d}_{email_slug}"
    email_dir.mkdir(parents=True, exist_ok=True)

    results = []
    index = 0

    for part in msg.walk():
        if part.is_multipart():
            continue

        disposition = (part.get("Content-Disposition") or "").lower()
        content_type = (part.get_content_type() or "").lower()
        filename = part.get_filename() or ""

        # Treat as attachment if: Content-Disposition is attachment,
        # OR the part has an explicit filename,
        # OR the content type is not text/html or text/plain (those are
        # already handled as the email body).
        is_attachment = (
            "attachment" in disposition
            or bool(filename)
            or (content_type not in ("text/plain", "text/html")
                and not content_type.startswith("multipart/"))
        )
        if not is_attachment:
            continue

        try:
            payload = part.get_payload(decode=True)
        except Exception:
            payload = None
        if payload is None or not isinstance(payload, (bytes, bytearray)):
            continue
        if len(payload) > MAX_ATTACHMENT_BYTES:
            log.warning("Attachment too large (%d bytes), skipping: %s",
                        len(payload), filename)
            continue

        index += 1
        safe_name = _safe_filename(filename, f"part{index}")
        dest = email_dir / safe_name
        # Avoid collision if two attachments have the same name
        if dest.exists():
            stem = dest.stem
            suffix = dest.suffix
            dest = email_dir / f"{stem}_{index}{suffix}"

        try:
            dest.write_bytes(payload)
        except Exception:
            log.exception("Failed to write attachment %s", dest)
            continue

        size_bytes = len(payload)
        extracted_text = ""
        method = "stored"

        if size_bytes <= MAX_EXTRACT_BYTES:
            if content_type in IMAGE_CONTENT_TYPES:
                extracted_text, method = _ocr_image(str(dest))
            elif content_type in PDF_CONTENT_TYPES:
                extracted_text, method = _extract_pdf(str(dest))
            elif content_type in TEXT_CONTENT_TYPES:
                extracted_text, method = _extract_text_file(str(dest))
        else:
            method = "skipped_too_large"

        if extracted_text and len(extracted_text) > MAX_EXTRACTED_CHARS:
            extracted_text = extracted_text[:MAX_EXTRACTED_CHARS] + "\n[...truncated...]"

        log.info(
            "Attachment saved: %s (%s, %d bytes, method=%s, extracted=%d chars)",
            dest.name, content_type, size_bytes, method, len(extracted_text),
        )

        results.append({
            "filename": safe_name,
            "content_type": content_type,
            "file_path": str(dest),
            "size_bytes": size_bytes,
            "extracted_text": extracted_text,
            "extraction_method": method,
        })

    return results


def append_extracted_text_to_body(body_text: str, attachments: list[dict]) -> str:
    """Given the original body and the extracted attachment data,
    return a single combined body that includes the attachment text
    so the regex scanner, Mistral preflight, and Claude agent all see it.
    """
    if not attachments:
        return body_text

    parts = [body_text or ""]
    for att in attachments:
        filename = att.get("filename", "?")
        content_type = att.get("content_type", "?")
        text = att.get("extracted_text", "")
        method = att.get("extraction_method", "")
        header = (
            f"\n\n---\n"
            f"[ATTACHMENT: {filename} ({content_type}), "
            f"extracted via {method}]"
        )
        if text:
            parts.append(header + "\n" + text)
        else:
            parts.append(header + "\n(no text extracted)")

    return "\n".join(parts)
