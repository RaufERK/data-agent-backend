"""Triplex/Navigator XML export builder.

_XmlPlanParser and module-level helpers were extracted to
triplex_xml_parser.py. This module re-exports them for backwards
compatibility and houses _TriplexExportBuilder.
"""

from __future__ import annotations

import secrets
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

# Import helpers and parser from the dedicated module; re-export for compat.
from backend.builders.triplex_xml_parser import (  # noqa: F401
    _XmlPlanParser,
    _slugify_js,
    _coerce_number,
    _hex_to_nav,
    _series_color_map,
    _append_colors,
)

from backend.builders.triplex_data_helpers import _DataHelpersMixin
from backend.builders.triplex_visual_helpers import _VisualHelpersMixin
from backend.builders.triplex_xml_builder import _XmlBuilderMixin
from backend.builders.triplex_xml_screen import _XmlScreenMixin
from backend.builders.triplex_chart_collect import _TriplexChartCollectMixin


# _XmlPlanParser is defined in triplex_xml_parser.py and re-exported above.

class _TriplexExportBuilder(
    _TriplexChartCollectMixin,
    _DataHelpersMixin,
    _VisualHelpersMixin,
    _XmlBuilderMixin,
    _XmlScreenMixin,
):
    # Export one structure step above the target Navigator's native 03.135
    # metadata. The embedded sd/jts block supplies the missing 203/204/205
    # tables, while the realistic version keeps the manual UI importer happy.
    DEFAULT_DB_VERSION = "03.136.00"
    DEFAULT_SCREEN_WIDTH = 12
    DEFAULT_SCREEN_HEIGHT = 8
    DEFAULT_WIDGET_WIDTH = 4
    DEFAULT_WIDGET_HEIGHT = 2
    DEFAULT_SUBJECT_AREA_ID = None  # generated per-export unless explicitly passed
    DEFAULT_INLINE_ROW_LIMIT = None
    DATE_PATTERNS = (
        re.compile(r"^\\d{4}-\\d{2}-\\d{2}"),
        re.compile(r"^\\d{2}\\.\\d{2}\\.\\d{4}"),
    )

    CHART_TYPE_ALIASES = {
        "big_number_total": "big_number",
        "big_number": "big_number",
        "number": "big_number",
        "metric": "big_number",
        "pie": "pie",
        "donut": "pie",
        "bar_horizontal": "bar_horizontal",
        "bar": "bar",
        "combo": "bar",
        "scatter": "scatter",
        "line": "line",
        "area": "area",
        "table": "table",
        "pivot_table": "table",
        "pivot_table_v2": "table",
        "sankey": "sankey",
        "sunkey": "sankey",
        "gantt": "gantt",
        "sunburst": "sunburst",
        "sunburst_v2": "sunburst",
        "treemap": "treemap",
        "treemap_v2": "treemap",
        "funnel": "funnel",
        "radar": "radar",
        "country_map": "country_map",
        "map": "country_map",
        "image": "image",
        "gauge": "gauge",
        "progress": "gauge",
        "thermometer": "gauge",
        "candlestick": "candlestick",
        "candle": "candlestick",
        "ohlc": "candlestick",
    }

    # Chart types for which this exporter knows how to build a complete
    # Navigator xparams <visuals> section.  If a detected chart type is outside
    # this set, emitting its widget type with an incomplete xparams payload can
    # produce an imported but blank/missing widget in Navigator.  Prefer a
    # supported visual with the same dataset over a formally closer empty one.
    SUPPORTED_XPARAM_CHARTS = {
        "big_number", "pie", "bar", "bar_horizontal", "line", "area",
        "scatter", "table", "sankey", "sunburst", "funnel", "candlestick",
    }

    XPARAM_FALLBACK_CHARTS = {
        "treemap": "pie",
        "country_map": "table",  # Navigator has no country_map renderer; show data as table
        "gantt": "table",
        "image": "table",
        "gauge": "big_number",  # Navigator has no gauge renderer; show as KPI card
        "progress": "big_number",
        # radar: wide-format data (one col per series) works correctly as multi-series
        # line chart; the native radar xparams builder requires long format which we
        # don't produce for per-widget sources.
        "radar": "line",
    }

    WIDGET_TYPE_BY_CHART = {
        "big_number": "94",
        "pie": "98",
        "bar": "89",
        "bar_horizontal": "89",
        "line": "89",
        "area": "89",
        "scatter": "89",
        "table": "102",
        "sankey": "110",
        "gantt": "99",
        "sunburst": "88",
        "treemap": "123",
        "funnel": "91",
        "radar": "96",
        "country_map": "102",  # falls back to table visual
        "image": "101",
        "candlestick": "89",
    }

    def __init__(self, payload: Dict[str, Any]):
        self.payload = payload or {}
        self.chart_meta = (
            self.payload.get("chart_meta")
            if isinstance(self.payload.get("chart_meta"), dict)
            else {}
        )
        self.kpi_rows = (
            self.payload.get("kpi_rows")
            if isinstance(self.payload.get("kpi_rows"), list)
            else []
        )
        self.tables = self._index_tables(self.payload.get("tables"))
        self.layout_entries = self._coerce_layout_entries(self.payload.get("layout"))
        self.layout_map = self._index_layout(self.layout_entries)
        self._id_counter = self._initial_id_counter()
        self._name_suffix = self._build_name_suffix()

        self.dashboard_id = self._coerce_int(self.payload.get("dashboard_id"))
        if self.dashboard_id is None or self.dashboard_id <= 0:
            self.dashboard_id = self._next_id()
        dashboard_ord = self._coerce_int(self.payload.get("dashboard_order"))
        if dashboard_ord is None:
            dashboard_ord = self.dashboard_id if -32768 <= self.dashboard_id <= 32767 else -1
        self.dashboard_ord = max(-32768, min(32767, dashboard_ord))
        requested_subject_area_id = self._coerce_int(self.payload.get("subject_area_id"))
        self.subject_area_id = (
            requested_subject_area_id
            if requested_subject_area_id is not None and requested_subject_area_id > 0
            else self._next_id()
        )
        self.screen_id = self._next_id()
        self.palette_id = self._next_id()
        self._dm_id_counter = self._initial_dm_id_counter()
        self._palette_colors: List[tuple[str, str]] = []
        self._palette_color_set: set[str] = set()
        inline_flag = self.payload.get("inline_data")
        self.inline_data = True if inline_flag is None else bool(inline_flag)
        inline_limit = self._coerce_int(self.payload.get("inline_row_limit"))
        self.inline_row_limit = inline_limit if inline_limit is not None and inline_limit > 0 else self.DEFAULT_INLINE_ROW_LIMIT
        base_subject_area_name = (
            str(self.payload.get("subject_area_name") or "")
            or str(self.payload.get("subject_area") or "")
            or "AI"
        )
        self.subject_area_name = self._with_suffix(base_subject_area_name)
        self.dashboard_title = self._with_suffix(
            str(self.payload.get("dashboard_title") or "Конструктор дашборда")
        )
        self.slug = str(self.payload.get("slug") or "custom_dashboard")
        self.charts = self._collect_charts(self.payload.get("charts"))
        self.semantic_sources = [] if self.payload.get("navigator_single_raw_source") else self._build_semantic_sources()

    @staticmethod
    def _coerce_int(value: Any) -> Optional[int]:
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

    def _next_id(self) -> int:
        self._id_counter += 1
        return self._id_counter

    @staticmethod
    def _initial_id_counter() -> int:
        # Navigator import expects positive object IDs. Start each export from a
        # high random int4-compatible range and increment to avoid collisions
        # between dashboards, subject areas, widgets, screens, and sources.
        min_id = 300_000_000
        max_id = 1_900_000_000
        return secrets.randbelow(max_id - min_id) + min_id

    @staticmethod
    def _initial_dm_id_counter() -> int:
        # Data model entities (t70..t75/t100) are expected as positive IDs.
        min_id = 900_000_000
        max_id = 2_000_000_000
        return secrets.randbelow(max_id - min_id) + min_id

    def _next_dm_id(self) -> int:
        self._dm_id_counter += 1
        return self._dm_id_counter

    @staticmethod
    def _build_name_suffix() -> str:
        now = datetime.now()
        millis = now.microsecond // 1000
        return f"{now:%y%m%d%H%M%S}{millis:03d}{secrets.randbelow(100):02d}"

    def _with_suffix(self, base: Any) -> str:
        text = str(base or "").strip() or "AI"
        return f"{text}_{self._name_suffix}"

    def _palette_id_for_export(self, exported_at: datetime) -> int:
        return self._next_id()

    def _palette_name_for_export(self, exported_at: datetime) -> str:
        stamp = exported_at.strftime("%Y-%m-%d %H:%M:%S")
        base = "AI Dashboard"
        title = (self.dashboard_title or "").strip()
        if title:
            base = f"{base} {title}"
        name = f"{base} {stamp}"
        return name[:100]

    def _normalize_layout_height(
        self,
        value: Any,
        *,
        chart_type: str = "",
        row_type: Any = None,
        width: Any = None,
        row_count: int = 0,
    ) -> int:
        try:
            height = float(value)
        except (TypeError, ValueError):
            height = float(self.DEFAULT_WIDGET_HEIGHT)
        if height <= 0:
            height = float(self.DEFAULT_WIDGET_HEIGHT)

        normalized_type = str(chart_type or "").strip().lower()
        normalized_row_type = str(row_type or "").strip().lower()
        try:
            width_value = int(width)
        except (TypeError, ValueError):
            width_value = self.DEFAULT_WIDGET_WIDTH

        scale = 20.0
        min_height = 3
        max_height = 8

        if normalized_row_type == "metric" or normalized_type == "big_number":
            scale = 12.0
            min_height = 5 if width_value >= 4 else 4
            max_height = 6
        elif normalized_type in {"pie", "donut", "sunburst", "treemap"}:
            scale = 12.0
            min_height = 8 if width_value >= 6 else 7
            max_height = 12
        elif normalized_type in {"bar", "bar_horizontal"}:
            scale = 12.0
            min_height = 12 if width_value >= 11 else (8 if width_value >= 6 else 7)
            max_height = 14
        elif normalized_type in {"line", "area", "radar", "funnel", "sankey"}:
            scale = 12.0
            min_height = 12 if width_value >= 11 else (8 if width_value >= 6 else 7)
            max_height = 14
        elif normalized_type == "table":
            scale = 12.0
            min_height = 9 if width_value >= 6 else 7
            max_height = 14
            if row_count > 0:
                # Size table to show its rows but cap at max_height so widgets
                # below it don't fall off screen.
                min_height = max(min_height, min(max_height, 3 + row_count // 2))

        if height > 20:
            height = round(height / scale)

        return max(min_height, min(max_height, int(height)))

    def _normalize_dataset_key(self, name: Optional[str]) -> str:
        if not name:
            return ""
        return re.sub(r"^Таблица:\\s*", "", str(name)).strip()

    def _short_dataset_name(self, name: Optional[str]) -> str:
        cleaned = self._normalize_dataset_key(name)
        if not cleaned:
            return ""
        base = cleaned.split("__", 1)[0].strip()
        return base or cleaned

    def _shorten_chart_title(self, title: str, dataset_name: Optional[str]) -> str:
        if not title:
            return title
        short_dataset = self._short_dataset_name(dataset_name)
        if not short_dataset:
            return title
        if dataset_name and dataset_name in title:
            return title.replace(dataset_name, short_dataset)
        if title.lower().startswith("таблица:"):
            base = title.split(":", 1)[1].strip()
            base_short = self._short_dataset_name(base)
            if base_short and base_short != base:
                return f"Таблица: {base_short}"
        return title

    def _index_tables(self, tables: Any) -> Dict[str, Dict[str, Any]]:
        indexed: Dict[str, Dict[str, Any]] = {}
        if not isinstance(tables, list):
            tables = []
        for table in tables:
            if not isinstance(table, dict):
                continue
            name = (table.get("table_name") or "").strip()
            if not name:
                continue
            rows = table.get("rows") if isinstance(table.get("rows"), list) else []
            raw_columns = table.get("columns") if isinstance(table.get("columns"), list) else None
            columns: List[str] = []
            if raw_columns:
                for col in raw_columns:
                    if isinstance(col, str):
                        columns.append(col)
                    elif isinstance(col, dict):
                        col_name = col.get("name") or col.get("column_name") or col.get("label")
                        if col_name:
                            columns.append(str(col_name))
            if not columns and rows:
                first = rows[0] if isinstance(rows[0], dict) else {}
                columns = list(first.keys()) if isinstance(first, dict) else []
            indexed[name] = {"rows": rows, "columns": columns}
        raw_table = self.payload.get("raw_table") if isinstance(self.payload, dict) else None
        if isinstance(raw_table, dict):
            rows = raw_table.get("rows") if isinstance(raw_table.get("rows"), list) else []
            raw_columns = raw_table.get("columns") if isinstance(raw_table.get("columns"), list) else []
            columns = [str(col) for col in raw_columns if str(col or "").strip()]
            if not columns and rows:
                first = rows[0] if isinstance(rows[0], dict) else {}
                columns = list(first.keys()) if isinstance(first, dict) else []
            if columns or rows:
                indexed.setdefault("FactDashboardRaw", {"rows": rows, "columns": columns})
        # Build ONE consolidated FactDashboardMetric table with all KPI rows
        # instead of N separate single-row tables.  Each KPI widget still gets
        # its own dataset reference (via _collect_charts / _infer_columns
        # fallback), but the data-model sees one coherent fact table.
        all_kpi_rows: List[Dict[str, Any]] = []
        kpi_col_set: set[str] = set()
        for row in self.kpi_rows:
            if not isinstance(row, dict):
                continue
            slug = _slugify_js(row.get("metric_code") or row.get("metric_name") or "")
            if not slug:
                continue
            value = (
                row.get("value")
                if row.get("value") is not None
                else row.get("metric_value")
                if row.get("metric_value") is not None
                else row.get("total")
            )
            kpi_entry = {
                "metric_code": row.get("metric_code"),
                "metric_name": row.get("metric_name"),
                "value": value if value is not None else 0,
                "unit": row.get("unit"),
                "period": row.get("period"),
                "note": row.get("note"),
            }
            # Also create an individual table so each KPI widget can reference
            # its own dataset (Navigator card widgets show a single row).
            individual_cols = [k for k, v in kpi_entry.items() if v is not None]
            individual_name = f"FactDashboardMetric_{slug}"
            if individual_name not in indexed:
                indexed[individual_name] = {
                    "rows": [kpi_entry],
                    "columns": individual_cols or ["value"],
                }
            sparkline = row.get("sparkline")
            # Also accept pre-serialized sparkline_json from synth.py
            if not isinstance(sparkline, list):
                import json as _json
                sparkline_json_str = row.get("sparkline_json")
                if isinstance(sparkline_json_str, str):
                    try:
                        sparkline = _json.loads(sparkline_json_str)
                    except Exception:
                        sparkline = []
            if isinstance(sparkline, list) and len(sparkline) >= 2:
                spark_rows: List[Dict[str, Any]] = []
                for spark_idx, spark_value in enumerate(sparkline, start=1):
                    value = _coerce_number(spark_value)
                    spark_rows.append({
                        "period": spark_idx,
                        "value": value if value is not None else 0,
                    })
                indexed[f"{individual_name}__sparkline"] = {
                    "rows": spark_rows,
                    "columns": ["period", "value"],
                    "_is_kpi_sparkline": True,
                    "_sparkline_type": str(row.get("sparkline_type") or "line").strip().lower(),
                }
            # Accumulate for consolidated table
            for k, v in kpi_entry.items():
                if v is not None:
                    kpi_col_set.add(k)
            all_kpi_rows.append(kpi_entry)

        # Register the consolidated KPI table (used by data-model builder)
        if all_kpi_rows:
            # Stable column order
            kpi_col_order = [c for c in ("metric_code", "metric_name", "value",
                                          "unit", "period", "note") if c in kpi_col_set]
            indexed["FactDashboardMetric"] = {
                "rows": all_kpi_rows,
                "columns": kpi_col_order or ["value"],
                "_is_consolidated_kpi": True,
            }
        return indexed

    def _coerce_layout_entries(self, layout: Any) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        if not isinstance(layout, list):
            return entries
        for entry in layout:
            if not isinstance(entry, dict):
                continue
            normalized = dict(entry)
            try:
                height = float(normalized.get("height"))
            except (TypeError, ValueError):
                height = 0.0
            if height > 20:
                normalized["height"] = max(1, round(height / 20.0))
            entries.append(normalized)
        return entries

    def _index_layout(self, layout: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        indexed: Dict[str, List[Dict[str, Any]]] = {}
        for entry in layout:
            name = (entry.get("slice_name") or "").strip()
            if name:
                indexed.setdefault(name, []).append(entry)
        return indexed

    def _find_kpi_row_for_widget(
        self,
        chart: Dict[str, Any],
        *,
        slice_name: str,
        dataset_name: str,
    ) -> Optional[Dict[str, Any]]:
        candidates = [
            str(chart.get("metric_code") or ""),
            slice_name.replace("KPI:", "", 1).strip(),
            dataset_name.replace("FactDashboardMetric_", "", 1).replace("__sparkline", "").strip(),
        ]
        normalized_candidates = {_slugify_js(candidate) for candidate in candidates if candidate}
        for row in self.kpi_rows:
            if not isinstance(row, dict):
                continue
            row_keys = {
                _slugify_js(row.get("metric_code") or ""),
                _slugify_js(row.get("metric_name") or ""),
            }
            if normalized_candidates & {key for key in row_keys if key}:
                return row
        return None

    def _kpi_sparkline_dataset_name(self, kpi_row: Dict[str, Any]) -> Optional[str]:
        slug = _slugify_js(kpi_row.get("metric_code") or kpi_row.get("metric_name") or "")
        if not slug:
            return None
        return f"FactDashboardMetric_{slug}__sparkline"
