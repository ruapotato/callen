# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""
Image processing for managed sites.

Takes a local image, strips EXIF, resizes, converts to WebP (JPEG
fallback), and pushes to the site's GitHub repo.
"""

import io
import logging
from pathlib import Path

from PIL import Image, ImageOps

log = logging.getLogger(__name__)


def process_and_upload_image(
    image_path: str,
    site_subdomain: str,
    manager,
    dest_path: str | None = None,
    max_width: int = 1200,
    quality: int = 85,
    commit_message: str = "",
) -> dict:
    """Process a local image and push it to a site's repo.

    Returns a dict with upload details.
    """
    src = Path(image_path)
    if not src.exists():
        raise FileNotFoundError(f"image not found: {image_path}")

    img = Image.open(src)

    # Auto-rotate based on EXIF orientation, then strip EXIF
    img = ImageOps.exif_transpose(img)
    # Create a clean copy without metadata
    clean = Image.new(img.mode, img.size)
    clean.putdata(list(img.getdata()))
    img = clean

    # Convert RGBA/P to RGB for JPEG/WebP compatibility
    if img.mode in ("RGBA", "P", "LA"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        if img.mode == "P":
            img = img.convert("RGBA")
        bg.paste(img, mask=img.split()[-1] if "A" in img.mode else None)
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")

    # Resize if too wide
    if img.width > max_width:
        ratio = max_width / img.width
        new_size = (max_width, int(img.height * ratio))
        img = img.resize(new_size, Image.LANCZOS)

    # Try WebP first, fall back to JPEG
    fmt = "webp"
    ext = ".webp"
    buf = io.BytesIO()
    try:
        img.save(buf, "WEBP", quality=quality)
    except Exception:
        log.info("WebP save failed, falling back to JPEG")
        fmt = "jpeg"
        ext = ".jpeg"
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality)

    image_bytes = buf.getvalue()

    # Determine destination path in repo
    if not dest_path:
        stem = src.stem.lower().replace(" ", "-")
        dest_path = f"images/{stem}{ext}"

    # Ensure dest_path has the right extension
    if not dest_path.endswith(ext):
        dest_path = str(Path(dest_path).with_suffix(ext))

    repo_full = f"{manager.github_org}/{site_subdomain}"
    message = commit_message or f"Upload image {dest_path}"

    manager._upsert_file_binary(repo_full, dest_path, image_bytes, message)

    log.info("Uploaded %s to %s/%s (%d bytes, %s)",
             src.name, site_subdomain, dest_path, len(image_bytes), fmt)

    return {
        "site": site_subdomain,
        "file": dest_path,
        "format": fmt,
        "width": img.width,
        "height": img.height,
        "size_bytes": len(image_bytes),
        "status": "pushed",
    }
