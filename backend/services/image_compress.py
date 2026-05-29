"""Read images and return base64 for LLM APIs."""
from __future__ import annotations

import base64
import io
import logging
from pathlib import Path

from PIL import Image

logger = logging.getLogger(__name__)

_MAX_SIDE = 2048


def compress_image_to_base64(image_path: str | Path) -> tuple[str, str]:
    """Convert image to JPEG RGB, downscale if needed, return (base64, mime)."""
    path = Path(image_path)
    img = Image.open(path)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGB")
    elif img.mode != "RGB":
        img = img.convert("RGB")
    if max(img.size) > _MAX_SIDE:
        img.thumbnail((_MAX_SIDE, _MAX_SIDE), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    logger.info("Compressed image %s → JPEG %dKB", path.name, len(b64) // 1024)
    return b64, "image/jpeg"
