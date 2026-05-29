"""Triplex/Navigator XML plan parser.

Extracted from triplex_export.py. Contains:
- Module-level helpers: _slugify_js, _coerce_number, _hex_to_nav,
  _series_color_map, _append_colors
- _XmlPlanParser — parses Navigator XML plan files and converts widget
  types to Superset-compatible equivalents.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.services.navigator_mapping import (
    NAVIGATOR_WIDGET_MAP,
    chart_type_by_title,
    map_navigator_chart_type,
)
from backend.utils.color_utils import normalize_hex_color as _normalize_hex_color


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _slugify_js(value: str) -> str:
    """Match the frontend JS slugify exactly so KPI dataset names align."""
    if not value:
        return ""
    result = str(value).strip().lower()
    result = re.sub(r"[^a-z0-9а-яё]+", "_", result)
    result = re.sub(r"^_+|_+$", "", result)
    return result


def _coerce_number(value: Any) -> Optional[float]:
    """Best-effort numeric extraction used for chart axis inference."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except Exception:
            return None
    if isinstance(value, str):
        cleaned = value.strip().replace("\u2212", "-").replace("\u00a0", " ")
        if not cleaned:
            return None
        match = re.search(r"[+-]?\d[\d\s]*(?:[.,]\d+)?", cleaned)
        if not match:
            return None
        num_str = match.group(0)
        num_str = re.sub(r"(\d)\s+(\d)", r"\1\2", num_str).replace(",", ".")
        try:
            return float(num_str)
        except Exception:
            return None
    return None


def _hex_to_nav(value: Any) -> Optional[str]:
    normalized = _normalize_hex_color(value)
    if not normalized:
        return None
    return f"0x{normalized[1:]}"


def _series_color_map(chart: Any) -> List[tuple[str, str]]:
    if not isinstance(chart, dict):
        return []
    series = chart.get("series")
    if not isinstance(series, list):
        return []
    colors: List[tuple[str, str]] = []
    for idx, entry in enumerate(series):
        if not isinstance(entry, dict):
            continue
        hex_code = _normalize_hex_color(entry.get("hex_code") or entry.get("color"))
        if not hex_code:
            continue
        label = str(entry.get("name") or entry.get("label") or "").strip()
        if not label:
            label = f"Series {idx + 1}"
        colors.append((label, hex_code))
    return colors


def _append_colors(parent: ET.Element, series_colors: List[tuple[str, str]]) -> None:
    if not series_colors:
        return
    color_rows: List[tuple[str, str]] = []
    for label, hex_code in series_colors:
        nav_color = _hex_to_nav(hex_code)
        if not nav_color:
            continue
        color_rows.append((label, nav_color))
    if not color_rows:
        return
    colors_el = ET.SubElement(parent, "colors")
    for label, nav_color in color_rows:
        ET.SubElement(colors_el, "color", attrib={"sName": label, "sColor": nav_color})


# ---------------------------------------------------------------------------
# _XmlPlanParser
# ---------------------------------------------------------------------------


class _XmlPlanParser:
    """Парсит XML-план и конвертирует типы виджетов в аналоги Superset."""

    XML_WIDGET_MAP: Dict[str, str] = NAVIGATOR_WIDGET_MAP

    @classmethod
    def _map_superset_type(
        cls,
        widget_type: Optional[str],
        visualization_type: Optional[str],
        title: Optional[str] = None,
    ) -> str:
        return map_navigator_chart_type(widget_type, visualization_type, title)

    @classmethod
    def _chart_type_by_title(cls, title_norm: str) -> Optional[str]:
        return chart_type_by_title(title_norm)

    @classmethod
    def _safe_parse_chart_vis(cls, xparams: str) -> Optional[str]:
        xparams = (xparams or "").strip()
        if not xparams:
            return None
        try:
            root = ET.fromstring(xparams)
        except Exception:
            return None
        chart = root.find(".//chart")
        if chart is None:
            return None
        return chart.attrib.get("nVisualizationType") or chart.attrib.get("nvisualizationtype")

    @classmethod
    def _parse_xparams_rich(cls, xparams: str) -> Dict[str, Any]:
        """Extract rich metadata from widget xparams XML."""
        result: Dict[str, Any] = {}
        xparams = (xparams or "").strip()
        if not xparams:
            return result
        try:
            root = ET.fromstring(xparams)
        except Exception:
            return result

        # Extract series info from <seriesList>
        series_list = root.findall(".//seriesList/series")
        parsed_series: List[Dict[str, Any]] = []
        series_types: set[str] = set()
        has_accumulation = False
        for s in series_list:
            s_type = (s.get("sType") or "").strip()
            if not s_type:
                continue
            s_name = (s.get("sName") or "").strip()
            s_color = (s.get("sColor") or "").strip()
            is_accum = s.get("isAccumulation", "0") == "1"
            s_measure = (s.get("sMeasure") or "").strip()
            if is_accum:
                has_accumulation = True
            series_types.add(s_type)
            entry: Dict[str, Any] = {"type": s_type, "name": s_name}
            if s_color:
                hex_color = _normalize_hex_color(s_color)
                if hex_color:
                    entry["hex_code"] = hex_color
            if is_accum:
                entry["stacked"] = True
            if s_measure:
                entry["measure"] = s_measure
            parsed_series.append(entry)

        if parsed_series:
            result["series"] = parsed_series

        # Determine if horizontal from canvas
        canvases = root.findall(".//canvas")
        is_horizontal = False
        is_compressed = False
        for canvas in canvases:
            if canvas.get("isHorizontal", "0") == "1":
                is_horizontal = True
            if canvas.get("isCompressed", "0") == "1":
                is_compressed = True
        if is_horizontal:
            result["is_horizontal"] = True
        if is_compressed:
            result["is_compressed"] = True

        # Determine stacking
        if has_accumulation:
            result["stacked"] = True

        # Determine combo chart (mixed histogram + plot/area)
        bar_types = {"histogram"}
        line_types = {"plot", "spline"}
        area_types = {"area", "area_spline"}
        has_bar = bool(series_types & bar_types)
        has_line = bool(series_types & line_types)
        has_area = bool(series_types & area_types)
        if has_bar and (has_line or has_area):
            result["is_combo"] = True
        elif has_line and not has_bar:
            result["primary_type"] = "line"
        elif has_area and not has_bar:
            result["primary_type"] = "area"

        # Extract params
        for param in root.findall(".//params/r"):
            param_id = param.get("sID", "")
            param_val = param.get("sValue", "")
            if param_id == "chartType" and param_val:
                result["param_chart_type"] = param_val
            elif param_id == "xField" and param_val:
                result["x_field"] = param_val
            elif param_id == "yField" and param_val:
                result["y_field"] = param_val
            elif param_id == "groupField" and param_val:
                result["group_field"] = param_val

        # Extract dataset fields
        fields = root.findall(".//fields/r")
        parsed_fields: List[Dict[str, str]] = []
        for f in fields:
            f_name = f.get("sName", "")
            f_type = f.get("sColumnType", "")
            f_id = f.get("sID", "")
            if f_name:
                parsed_fields.append({"id": f_id, "name": f_name, "type": f_type})
        if parsed_fields:
            result["fields"] = parsed_fields

        return result

    @classmethod
    def _refine_superset_type(cls, base_type: str, xparams_meta: Dict[str, Any]) -> str:
        """Refine superset chart type using rich xparams metadata."""
        if not xparams_meta:
            return base_type

        if xparams_meta.get("is_combo"):
            return "combo"

        primary = xparams_meta.get("primary_type")
        if primary == "line" and base_type == "bar":
            return "line"
        if primary == "area" and base_type == "bar":
            return "area"

        if base_type == "bar" and xparams_meta.get("is_horizontal"):
            return "bar_horizontal"

        return base_type

    @staticmethod
    def _coerce_int(value: Any) -> Optional[int]:
        try:
            return int(str(value).strip())
        except Exception:
            return None

    @classmethod
    def _attach_screen_positions(cls, entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not entries:
            return entries
        coords = []
        for entry in entries:
            x = cls._coerce_int(entry.get("x"))
            y = cls._coerce_int(entry.get("y"))
            width = cls._coerce_int(entry.get("width_units"))
            height = cls._coerce_int(entry.get("height_units"))
            if x is None or y is None or width is None or height is None:
                continue
            if width <= 0 or height <= 0:
                continue
            coords.append((x, y, width, height))
        if not coords:
            return entries

        min_x = min(x for x, _, _, _ in coords)
        min_y = min(y for _, y, _, _ in coords)
        max_right = max(x + width for x, _, width, _ in coords)
        max_bottom = max(y + height for _, y, _, height in coords)
        total_width = max(1, max_right - min_x)
        total_height = max(1, max_bottom - min_y)

        for entry in entries:
            x = cls._coerce_int(entry.get("x"))
            y = cls._coerce_int(entry.get("y"))
            width = cls._coerce_int(entry.get("width_units"))
            height = cls._coerce_int(entry.get("height_units"))
            if x is None or y is None or width is None or height is None:
                continue
            if width <= 0 or height <= 0:
                continue
            entry["position"] = {
                "left": round(max(0.0, min(1.0, (x - min_x) / total_width)), 4),
                "top": round(max(0.0, min(1.0, (y - min_y) / total_height)), 4),
                "width": round(max(0.0, min(1.0, width / total_width)), 4),
                "height": round(max(0.0, min(1.0, height / total_height)), 4),
            }
        return entries

    @classmethod
    def _parse_widget_row(cls, row: ET.Element) -> Dict[str, Any]:
        """Parse a single t19 widget row into a typed dict."""
        widget_type = row.attrib.get("nwidgettypeid")
        xparams_raw = row.attrib.get("xparams", "")
        visualization_type = cls._safe_parse_chart_vis(xparams_raw)
        xparams_meta = cls._parse_xparams_rich(xparams_raw)
        title = (
            row.attrib.get("sname_ru")
            or row.attrib.get("sname_en")
            or row.attrib.get("sname")
            or ""
        ).strip()
        superset_type = cls._map_superset_type(widget_type, visualization_type, title)
        superset_type = cls._refine_superset_type(superset_type, xparams_meta)
        entry: Dict[str, Any] = {
            "widget_id": row.attrib.get("nid"),
            "title": title,
            "widget_type": widget_type,
            "visualization_type": visualization_type,
            "superset_type": superset_type,
        }
        screen_id = row.attrib.get("nscreenid")
        if screen_id:
            entry["screen_id"] = screen_id
        x_coord = cls._coerce_int(row.attrib.get("nxcoord"))
        y_coord = cls._coerce_int(row.attrib.get("nycoord"))
        width_units = cls._coerce_int(row.attrib.get("nwidth"))
        height_units = cls._coerce_int(row.attrib.get("nheight"))
        if x_coord is not None:
            entry["x"] = x_coord
        if y_coord is not None:
            entry["y"] = y_coord
        if width_units is not None:
            entry["width_units"] = width_units
        if height_units is not None:
            entry["height_units"] = height_units
        order = cls._coerce_int(row.attrib.get("norder"))
        if order is not None:
            entry["order"] = order
        if xparams_meta.get("series"):
            entry["series"] = xparams_meta["series"]
        if xparams_meta.get("stacked"):
            entry["stacked"] = True
        if xparams_meta.get("is_horizontal"):
            entry["is_horizontal"] = True
        if xparams_meta.get("is_combo"):
            entry["is_combo"] = True
        if xparams_meta.get("is_compressed"):
            entry["is_compressed"] = True
        if xparams_meta.get("x_field"):
            entry["x_field"] = xparams_meta["x_field"]
        if xparams_meta.get("y_field"):
            entry["y_field"] = xparams_meta["y_field"]
        if xparams_meta.get("group_field"):
            entry["group_field"] = xparams_meta["group_field"]
        if xparams_meta.get("fields"):
            entry["fields"] = xparams_meta["fields"]
        return entry

    @classmethod
    def parse_plan(cls, xml_path: Path) -> List[Dict[str, Any]]:
        if not xml_path.exists() or not xml_path.is_file():
            raise FileNotFoundError(f"XML файл не найден: {xml_path}")
        tree = ET.parse(xml_path)
        root = tree.getroot()
        data = root.find("data")
        if data is None:
            return []
        widgets = data.find("t19")
        if widgets is None:
            return []

        results: List[Dict[str, Any]] = []
        for row in widgets:
            results.append(cls._parse_widget_row(row))
        return cls._attach_screen_positions(results)

    @classmethod
    def parse_plan_by_screen(cls, xml_path: Path) -> Dict[str, List[Dict[str, Any]]]:
        """Parse XML and group widgets by screen_id.

        Returns dict mapping screen_id -> list of widget entries.
        """
        if not xml_path.exists() or not xml_path.is_file():
            raise FileNotFoundError(f"XML файл не найден: {xml_path}")
        tree = ET.parse(xml_path)
        root = tree.getroot()
        data = root.find("data")
        if data is None:
            return {}
        widgets = data.find("t19")
        if widgets is None:
            return {}

        by_screen: Dict[str, List[Dict[str, Any]]] = {}
        for row in widgets:
            entry = cls._parse_widget_row(row)
            screen_id = entry.get("screen_id", "__unknown__")
            by_screen.setdefault(screen_id, []).append(entry)
        for screen_id, entries in by_screen.items():
            by_screen[screen_id] = cls._attach_screen_positions(entries)
        return by_screen

    @staticmethod
    def latest_plan(upload_dir: Path) -> Optional[Path]:
        if not upload_dir.exists():
            return None
        xml_files = sorted(
            upload_dir.glob("*.xml"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return xml_files[0] if xml_files else None
