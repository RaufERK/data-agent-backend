"""Chart collection helpers for Triplex export builder."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from backend.services.navigator_mapping import map_navigator_chart_type, parse_navigator_descriptor
from backend.builders.triplex_xml_parser import _slugify_js


class _TriplexChartCollectMixin:
    def _collect_charts(self, charts: Any) -> List[Dict[str, Any]]:
        collected: List[Dict[str, Any]] = []
        explicit_counts: Dict[str, int] = {}
        if isinstance(charts, list):
            for chart in charts:
                if not isinstance(chart, dict):
                    continue
                name = (
                    chart.get("slice_name")
                    or chart.get("name")
                    or chart.get("title")
                    or chart.get("widget_title")
                    or chart.get("id")
                    or ""
                ).strip()
                if not name:
                    continue
                normalized_chart = dict(chart)
                normalized_chart.setdefault("slice_name", name)
                normalized_chart.setdefault("name", name)
                if not normalized_chart.get("chart_type") and normalized_chart.get("type"):
                    normalized_chart["chart_type"] = normalized_chart.get("type")
                if not normalized_chart.get("viz_type") and normalized_chart.get("chart_type"):
                    normalized_chart["viz_type"] = normalized_chart.get("chart_type")
                if not normalized_chart.get("dataset") and not normalized_chart.get("table_name"):
                    if getattr(self, "tables", {}).get("FactDashboardRaw"):
                        normalized_chart["dataset"] = "FactDashboardRaw"
                        normalized_chart["table_name"] = "FactDashboardRaw"
                normalized_chart["_collect_order"] = len(collected)
                collected.append(normalized_chart)
                explicit_counts[name] = explicit_counts.get(name, 0) + 1

        layout_seen: Dict[str, int] = {}
        for entry in self.layout_entries:
            name = (entry.get("slice_name") or "").strip()
            if not name:
                continue
            layout_seen[name] = layout_seen.get(name, 0) + 1
            if layout_seen[name] <= explicit_counts.get(name, 0):
                continue
            dataset = entry.get("dataset") or entry.get("table_name")
            row_type = (entry.get("row_type") or "").strip().lower()
            if not dataset and row_type != "metric":
                continue
            collected.append({
                "slice_name": name,
                "dataset": dataset,
                "row_type": row_type,
                "viz_type": "big_number" if row_type == "metric" else "table",
                "_collect_order": len(collected),
            })
        existing_names = {
            str(chart.get("slice_name") or chart.get("name") or chart.get("title") or "").strip().lower()
            for chart in collected
            if isinstance(chart, dict)
        }
        for row in getattr(self, "kpi_rows", []) or []:
            if not isinstance(row, dict):
                continue
            metric_name = str(row.get("metric_name") or row.get("title") or row.get("metric_code") or "").strip()
            if not metric_name or metric_name.lower() in existing_names:
                continue
            slug = _slugify_js(row.get("metric_code") or metric_name)
            dataset = f"FactDashboardMetric_{slug}" if slug else "FactDashboardMetric"
            collected.append({
                "slice_name": metric_name,
                "name": metric_name,
                "dataset": dataset,
                "table_name": dataset,
                "row_type": "metric",
                "chart_type": "big_number",
                "viz_type": "big_number",
                "metric_code": row.get("metric_code") or metric_name,
                "unit": row.get("unit") or row.get("value_unit") or "",
                "value": row.get("value"),
                "position": row.get("position"),
                "_collect_order": len(collected),
            })
            existing_names.add(metric_name.lower())
        if self.layout_entries:
            layout_order: Dict[str, int] = {}
            for idx, entry in enumerate(self.layout_entries):
                name = str(entry.get("slice_name") or "").strip().lower()
                if name and name not in layout_order:
                    layout_order[name] = idx
            collected.sort(
                key=lambda chart: (
                    layout_order.get(
                        str(chart.get("slice_name") or chart.get("name") or chart.get("title") or "").strip().lower(),
                        len(layout_order) + int(chart.get("_collect_order") or 0),
                    ),
                    int(chart.get("_collect_order") or 0),
                )
            )
        return collected

    def _normalize_chart_type(self, raw_type: Any, row_type: Any) -> tuple[str, Optional[str]]:
        chart_type = str(raw_type or "").strip().lower()
        nav_widget = None
        nav_title = ""
        if chart_type.startswith(("nav:", "navigator:")):
            nav_widget, nav_vis, nav_title = parse_navigator_descriptor(chart_type)
            chart_type = map_navigator_chart_type(nav_widget, nav_vis, nav_title)
        if row_type and str(row_type).strip().lower() == "metric":
            chart_type = "big_number"
        if not chart_type or chart_type == "auto":
            chart_type = "table"
        normalized = self.CHART_TYPE_ALIASES.get(chart_type, chart_type)
        if normalized not in self.SUPPORTED_XPARAM_CHARTS:
            normalized = self.XPARAM_FALLBACK_CHARTS.get(normalized, "table")
        return normalized, nav_widget

    def _widget_type_id(self, chart_type: str) -> str:
        return self.WIDGET_TYPE_BY_CHART.get(chart_type, "89")

    # Pattern for strings that are "predominantly numeric": optional sign,
    # digits with optional decimal separator (comma or dot), optional
    # trailing '%' / whitespace.  This rejects things like "Стадия 1" or
    # "15% от всех" where the number is embedded in unrelated text.
    _STRICT_NUMERIC_RE = re.compile(
        r"^\s*[+-]?\s*\d[\d\s\u00a0]*(?:[.,]\d+)?\s*%?\s*$"
    )
