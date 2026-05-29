"""Image color extraction mixin for DashboardVisionService."""
from __future__ import annotations

import colorsys
import logging
from pathlib import Path
from typing import Any, Dict, List

from ..utils.color_utils import normalize_hex_color as _normalize_hex_color
from ..utils.chart_utils import normalize_chart_type as _normalize_chart_type

logger = logging.getLogger(__name__)

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None  # type: ignore


class _ColorsMixin:
    """Dominant color extraction from chart image crops."""

    @staticmethod
    def _hex_is_visible(hex_color: str) -> bool:
        """Return True if hex color is bright enough to be visible on a dark background."""
        try:
            h = hex_color.lstrip("#")
            if len(h) != 6:
                return False
            r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
            _, _, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
            return v >= 0.32
        except Exception:
            return False

    def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
        return "#{:02x}{:02x}{:02x}".format(*rgb)

    @staticmethod
    def _color_distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
        return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5

    @classmethod
    def _dominant_chart_colors(
        cls,
        image_path: Path,
        position: Dict[str, Any],
        *,
        max_colors: int = 6,
    ) -> List[str]:
        """Extract saturated chart-series colors from the source crop."""
        if Image is None:
            return []
        try:
            with Image.open(image_path) as image:
                rgb_image = image.convert("RGB")
                img_w, img_h = rgb_image.size
                left = max(0, int(float(position.get("left", 0.0) or 0.0) * img_w))
                top = max(0, int(float(position.get("top", 0.0) or 0.0) * img_h))
                width = max(1, int(float(position.get("width", 1.0) or 1.0) * img_w))
                height = max(1, int(float(position.get("height", 1.0) or 1.0) * img_h))
                right = min(img_w, left + width)
                bottom = min(img_h, top + height)
                if right <= left or bottom <= top:
                    return []
                crop = rgb_image.crop((left, top, right, bottom))
                crop_w, crop_h = crop.size
                stride = max(1, int(((crop_w * crop_h) / 24000) ** 0.5))
                buckets: Dict[tuple[int, int, int], int] = {}
                for y in range(0, crop_h, stride):
                    for x in range(0, crop_w, stride):
                        r, g, b = crop.getpixel((x, y))
                        _, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
                        if s < 0.28 or v < 0.32 or v > 0.98:
                            continue
                        quantized = (round(r / 16) * 16, round(g / 16) * 16, round(b / 16) * 16)
                        quantized = tuple(max(0, min(255, value)) for value in quantized)
                        buckets[quantized] = buckets.get(quantized, 0) + 1

                if not buckets:
                    return []

                ordered = sorted(buckets.items(), key=lambda item: item[1], reverse=True)
                min_count = max(3, int(sum(buckets.values()) * 0.003))
                selected: List[tuple[int, int, int]] = []
                for rgb, count in ordered:
                    if count < min_count and selected:
                        continue
                    if any(cls._color_distance(rgb, existing) < 42 for existing in selected):
                        continue
                    selected.append(rgb)
                    if len(selected) >= max_colors:
                        break
                return [cls._rgb_to_hex(rgb) for rgb in selected]
        except Exception as exc:
            logger.debug("Chart color extraction failed for %s: %s", image_path, exc)
            return []

    @classmethod
    def _detect_background_theme(cls, image_path: Path) -> str:
        """Return 'dark' or 'light' based on sampling the image background region.

        Samples the top strip and four corner patches of the image, which typically
        show the dashboard background rather than chart content.  Returns 'light'
        when average luminance exceeds 0.55, otherwise 'dark'.
        """
        if Image is None:
            return "dark"
        try:
            with Image.open(image_path) as image:
                rgb = image.convert("RGB")
                w, h = rgb.size
                # Collect pixels from corners + top strip (background areas)
                sample_boxes: list[tuple[int, int, int, int]] = [
                    (0, 0, min(w, max(1, w // 6)), min(h, max(1, h // 6))),                          # top-left
                    (max(0, w - w // 6), 0, w, min(h, max(1, h // 6))),                               # top-right
                    (0, max(0, h - h // 6), min(w, max(1, w // 6)), h),                               # bottom-left
                    (max(0, w - w // 6), max(0, h - h // 6), w, h),                                   # bottom-right
                    (0, 0, w, min(h, max(1, h // 10))),                                                # top strip
                ]
                total_v = 0.0
                total_count = 0
                for box in sample_boxes:
                    x0, y0, x1, y1 = box
                    if x1 <= x0 or y1 <= y0:
                        continue
                    crop = rgb.crop((x0, y0, x1, y1))
                    cw, ch = crop.size
                    stride = max(1, int(((cw * ch) / 4000) ** 0.5))
                    for y in range(0, ch, stride):
                        for x in range(0, cw, stride):
                            r, g, b_val = crop.getpixel((x, y))
                            _, _, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b_val / 255.0)
                            total_v += v
                            total_count += 1
                if total_count == 0:
                    return "dark"
                avg_v = total_v / total_count
                return "light" if avg_v > 0.55 else "dark"
        except Exception as exc:
            logger.debug("Background theme detection failed for %s: %s", image_path, exc)
            return "dark"

    @classmethod
    def _apply_extracted_chart_colors(cls, parsed: Dict[str, Any], image_path: Path) -> Dict[str, Any]:
        if not isinstance(parsed, dict):
            return parsed

        # Detect and store background theme (dark/light)
        bg_theme = cls._detect_background_theme(image_path)
        parsed["background_theme"] = bg_theme

        charts = parsed.get("charts")
        if not isinstance(charts, list):
            return parsed
        updated = 0
        for chart in charts:
            if not isinstance(chart, dict):
                continue
            chart_type = _normalize_chart_type(chart.get("chart_type") or chart.get("type"))
            if chart_type in {"table", "pivot_table", "big_number", "country_map"}:
                continue
            position = chart.get("position") if isinstance(chart.get("position"), dict) else None
            if not position:
                continue
            palette = cls._dominant_chart_colors(image_path, position)
            if not palette:
                continue
            chart["extracted_palette"] = palette
            if not _normalize_hex_color(chart.get("color")):
                chart["color"] = palette[0]

            series = chart.get("series")
            if isinstance(series, list):
                for idx, entry in enumerate(series):
                    if not isinstance(entry, dict):
                        continue
                    existing = _normalize_hex_color(entry.get("hex_code"))
                    if existing and cls._hex_is_visible(existing):
                        continue  # keep good color from vision
                    if idx < len(palette):
                        entry["hex_code"] = palette[idx]
                    elif palette:
                        entry["hex_code"] = palette[idx % len(palette)]

            legend_items = chart.get("legend_items")
            if isinstance(legend_items, list):
                for idx, item in enumerate(legend_items):
                    if isinstance(item, dict) and idx < len(palette):
                        item["hex_code"] = palette[idx]
                        item["color"] = palette[idx]
            updated += 1

        if updated:
            diag = parsed.get("stage_diagnostics") if isinstance(parsed.get("stage_diagnostics"), dict) else {}
            diag["image_color_matched_chart_count"] = updated
            parsed["stage_diagnostics"] = diag
        return parsed

