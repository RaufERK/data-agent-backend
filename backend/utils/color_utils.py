"""Color normalization utilities."""
from __future__ import annotations

import re
from typing import Any, Optional

_HEX_COLOR_RE = re.compile(r"^[0-9a-fA-F]{6}$")


def normalize_hex_color(value: Any) -> Optional[str]:
    """Normalize a color value to '#rrggbb' format, or return None if invalid."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.lower().startswith("0x"):
        text = text[2:]
    if text.startswith("#"):
        text = text[1:]
    if not _HEX_COLOR_RE.match(text):
        return None
    return f"#{text.lower()}"
