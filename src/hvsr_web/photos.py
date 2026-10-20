"""Sensor/area photograph processing (spec 2.2).

Validate actual image contents and decompressed size, apply EXIF orientation, aspect-preserving
resize + pad to exactly 300 x 300, and re-encode without EXIF (removing GPS). Only the processed
image is stored.
"""

from __future__ import annotations

import io

from PIL import Image, ImageOps, UnidentifiedImageError

from hvsr_core.model import InputError

ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}
TARGET = (300, 300)


def process_photo(data: bytes, max_input_bytes: int = 15 * 1024 * 1024, max_pixels: int = 40_000_000) -> tuple[bytes, dict]:
    if len(data) > max_input_bytes:
        raise InputError("photo_size", "photo exceeds the upload size limit")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            fmt = probe.format
            w, h = probe.size
            if fmt not in ALLOWED_FORMATS:
                raise InputError("photo_format", f"unsupported image format {fmt}; use JPEG, PNG or WebP")
            if w * h > max_pixels:
                raise InputError("photo_pixels", f"image has {w * h} pixels; limit is {max_pixels} (decompression safety)")
            probe.verify()
    except InputError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise InputError("photo_invalid", f"not a valid image ({type(exc).__name__})") from exc

    previous = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = max_pixels
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            oriented = ImageOps.exif_transpose(img)
            rgb = oriented.convert("RGB")
            out_img = ImageOps.pad(rgb, TARGET, method=Image.Resampling.LANCZOS, color=(255, 255, 255), centering=(0.5, 0.5))
    finally:
        Image.MAX_IMAGE_PIXELS = previous
    buf = io.BytesIO()
    out_img.save(buf, format="JPEG", quality=88, optimize=True)
    out = buf.getvalue()
    return out, {"input_format": fmt, "input_size": [w, h], "output_size": list(out_img.size), "exif_removed": True}
