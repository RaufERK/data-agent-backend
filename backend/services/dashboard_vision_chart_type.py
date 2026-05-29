"""Chart-type detection and KPI classification mixin for DashboardVisionService."""
from __future__ import annotations

from .dashboard_vision_kpi import _ChartKpiMixin

import re
from typing import Any, Dict, Optional


_TEMPORAL_LABEL_RE = re.compile(
    r"(янв|фев|мар|апр|май|июн|июл|авг|сен|oct|окт|ноя|дек|q[1-4]|\b20\d{2}\b|\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?)",
    re.IGNORECASE,
)
_KPI_TITLE_RE = re.compile(
    r"(итог|всего|kpi|показател|выручк|доход|расход|прибыл|план|факт|конверс|ctr|cr|arpu|arppu|маu|dau|ltv|nps|roi|romi|cpc|cpm|клики|лиды|продаж|заказ|балл|скор|metric)",
    re.IGNORECASE,
)
_GAUGE_TITLE_RE = re.compile(
    r"(gauge|progress|thermometer|speedometer|radial|pictorial|спидометр|термометр|прогресс|шкала|индикатор)",
    re.IGNORECASE,
)
_KPI_GROUP_TITLE_RE = re.compile(
    r"(виды|структур|состав|характеристик|показател[ья]|профил|сводк|обзор|итог[ио]|сравнен)",
    re.IGNORECASE,
)


class _ChartTypeMixin(_ChartKpiMixin):
    """Chart-type detection, KPI classification, dedup and pruning helpers."""

    @staticmethod
    def _chart_title(chart: Dict[str, Any]) -> str:
        # Reject strings that look like axis tick labels (only digits/spaces/punctuation)
        _numeric_only = re.compile(r'^[\d\s.,;/:%-]+$')
        for key in ("title", "chart_name", "name", "y_axis"):
            candidate = str(chart.get(key) or "").strip()
            if candidate and not _numeric_only.match(candidate):
                return candidate
        return ""

    @staticmethod
    def _is_temporal_categories(categories: Any) -> bool:
        if not isinstance(categories, list) or not categories:
            return False
        labels = [str(item or "").strip() for item in categories if str(item or "").strip()]
        if not labels:
            return False
        hits = sum(1 for label in labels if _TEMPORAL_LABEL_RE.search(label))
        return hits >= max(1, min(2, len(labels) // 2))

    @staticmethod
    def _series_has_xy_points(series: Any) -> bool:
        if not isinstance(series, list):
            return False
        for entry in series:
            if not isinstance(entry, dict):
                continue
            data = entry.get("data")
            if not isinstance(data, list):
                continue
            for point in data:
                if isinstance(point, dict) and ("x" in point or "y" in point):
                    return True
        return False

    @staticmethod
    def _chart_has_rows(chart: Dict[str, Any]) -> bool:
        rows = chart.get("rows")
        return isinstance(rows, list) and any(isinstance(row, dict) for row in rows)

    @classmethod
    def _rows_look_like_stacked_bar(cls, chart: Dict[str, Any], title: str) -> bool:
        """
        Detect if table rows actually represent a stacked bar chart.
        Signs: temporal labels in rows, multiple numeric columns, title hints at dynamics/trend.
        """
        rows = chart.get("rows")
        if not isinstance(rows, list) or len(rows) < 2:
            return False

        title_lower = title.lower()
        # Title hints that suggest time-based progression (stacked bar)
        dynamic_hints = ("динамик", "trend", "тренд", "измен", "по месяц", "по период", "история", "progress")
        has_dynamic_title = any(hint in title_lower for hint in dynamic_hints)

        # Extract row labels (first value in each row, or a common key like period/month)
        row_labels = []
        numeric_columns = set()

        for row in rows:
            if not isinstance(row, dict):
                continue
            # Try to find temporal label in row
            for key, val in row.items():
                val_str = str(val or "").strip()
                # Check if this looks like a temporal label
                if _TEMPORAL_LABEL_RE.search(val_str):
                    row_labels.append(val_str)
                    break
                # Also check if key itself is temporal (e.g., "Месяц": "Апр")
                if _TEMPORAL_LABEL_RE.search(key):
                    row_labels.append(f"{key}: {val_str}")
                    break
            # Count numeric columns
            for key, val in row.items():
                val_str = str(val or "").strip().replace(",", ".").replace(" ", "")
                try:
                    float(val_str)
                    numeric_columns.add(key)
                except (ValueError, TypeError):
                    pass

        # Stacked bar chart criteria:
        # 1. At least 3 rows with temporal labels OR title hints at dynamics
        # 2. Multiple numeric columns (the "stacks")
        has_temporal_rows = len(row_labels) >= max(2, len(rows) // 2)
        has_multiple_stacks = len(numeric_columns) >= 3

        return (has_temporal_rows or has_dynamic_title) and has_multiple_stacks

    @staticmethod
    def _looks_like_numeric_cell(value: Any) -> bool:
        text = str(value or "").strip().lower()
        if not text:
            return False
        normalized = (
            text.replace("%", "")
            .replace("₽", "")
            .replace("руб.", "")
            .replace("руб", "")
            .replace("тыс.", "")
            .replace("млн", "")
            .replace("часов", "")
            .replace("час", "")
            .replace("чел.", "")
            .replace("чел", "")
            .replace("дн.", "")
            .replace("дн", "")
            .replace(" ", "")
            .replace(",", ".")
        )
        try:
            float(normalized)
            return True
        except (TypeError, ValueError):
            return False

    @classmethod
    def _rows_look_like_transposed_series(cls, chart: Dict[str, Any], title: str) -> bool:
        rows = chart.get("rows")
        if not isinstance(rows, list) or not rows or len(rows) > 2:
            return False
        title_hint = cls._type_from_title(title)
        if title_hint in {"table", "pivot_table"}:
            return False
        categories = chart.get("categories") if isinstance(chart.get("categories"), list) else []
        if not categories:
            first_row = rows[0] if isinstance(rows[0], dict) else {}
            categories = list(first_row.keys()) if isinstance(first_row, dict) else []
        labels = [str(item or "").strip() for item in categories if str(item or "").strip()]
        if len(labels) < 3:
            return False
        temporal_hits = sum(1 for label in labels if _TEMPORAL_LABEL_RE.search(label))
        if temporal_hits < max(2, len(labels) - 1):
            return False
        numeric_cells = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            for value in row.values():
                if cls._looks_like_numeric_cell(value):
                    numeric_cells += 1
        return numeric_cells >= 2

    @classmethod
    def _looks_like_stacked_bar_without_rows(cls, chart: Dict[str, Any], title: str) -> bool:
        """
        Detect stacked horizontal bar chart even when rows are not extracted.
        Uses legend_items (colored segments) + title hints as signals.

        Example: "Динамика всех договоров" with legend ["Новый", "На согласовании", ...]
        is clearly a stacked bar, not a table.
        """
        # Don't convert if chart is already recognized as pie/donut
        current_type = str(chart.get("chart_type") or chart.get("type") or "").lower()
        if current_type in ("pie", "donut", "doughnut", "radar", "polararea", "funnel"):
            return False

        # Don't convert if block_kind is explicitly chart (and not table)
        block_kind = str(chart.get("block_kind") or "").lower()
        if block_kind == "chart" and current_type not in ("table", "pivot_table", ""):
            return False

        legend_items = chart.get("legend_items")
        if not isinstance(legend_items, list) or len(legend_items) < 3:
            return False

        title_lower = title.lower()

        # Title hints that strongly suggest stacked bar (progression/dynamics)
        dynamic_hints = (
            "динамик", "динам", "по месяц", "по период", "по год",
            "trend", "тренд", "история", "progress", "измен",
            "распредел", "структур", "состав", "breakdown"
        )
        has_dynamic_title = any(hint in title_lower for hint in dynamic_hints)

        # Also check if legend items look like status categories (common in stacked bars)
        status_hints = ("новый", "согласов", "подписан", "отклонен", "выполн", "завер", "в работе", "план", "факт")
        legend_text = " ".join(str(item).lower() for item in legend_items)
        has_status_legend = sum(1 for hint in status_hints if hint in legend_text) >= 2

        # Stacked bar criteria: many legend items + (dynamic title OR status-like legend)
        many_legend_items = len(legend_items) >= 4

        return many_legend_items and (has_dynamic_title or has_status_legend)

    @staticmethod
    def _chart_point_count(chart: Dict[str, Any]) -> int:
        total = 0
        series = chart.get("series")
        if not isinstance(series, list):
            return 0
        for entry in series:
            if not isinstance(entry, dict):
                continue
            data = entry.get("data")
            if isinstance(data, list):
                total += len(data)
            elif entry.get("value") is not None:
                total += 1
        return total

    @staticmethod
    def _extract_chart_value(chart: Dict[str, Any]) -> Any:
        for key in ("value_number", "value", "metric_value"):
            value = chart.get(key)
            if value not in (None, ""):
                return value
        series = chart.get("series")
        if isinstance(series, list):
            for entry in series:
                if not isinstance(entry, dict):
                    continue
                if entry.get("value") not in (None, ""):
                    return entry.get("value")
                data = entry.get("data")
                if isinstance(data, list) and len(data) == 1:
                    point = data[0]
                    if isinstance(point, (int, float, str)):
                        return point
        return None

    @classmethod
    def _type_from_title(cls, title: str) -> Optional[str]:
        text = str(title or "").strip().lower()
        if not text:
            return None
        if text == "показатели" or text.startswith("показатели "):
            return "table"
        if "распределение финансовых поток" in text:
            return "bar"
        # Titles like "Распределение X" strongly imply pie/donut unless explicitly bar
        if text.startswith("распределение ") and "поток" not in text and "продаж" not in text:
            return "donut"
        if any(token in text for token in ("санки", "санкей", "sankey")):
            return "sankey"
        if any(token in text for token in ("sunburst",)):
            return "sunburst"
        if any(token in text for token in ("treemap", "дерев")):
            return "treemap"
        if any(token in text for token in ("ворон", "funnel")):
            return "funnel"
        if any(token in text for token in ("радар", "radar")):
            return "radar"
        if any(token in text for token in ("точеч", "scatter", "bubble", "quadrant")):
            return "scatter"
        if any(token in text for token in ("две оси", "combo", "комб", "смеш")):
            return "combo"
        if any(token in text for token in ("таблиц", "table", "pivot", "свод")):
            return "pivot_table" if "pivot" in text or "свод" in text else "table"
        if any(token in text for token in ("кольц", "donut", "doughnut")):
            return "donut"
        if any(token in text for token in ("круг", "pie")):
            return "pie"
        if any(token in text for token in ("гориз", "horizontal", "race")):
            return "bar_horizontal"
        if any(token in text for token in ("площад", "area", "заливк")):
            return "area"
        if any(token in text for token in ("тренд", "линейн", "line")):
            return "line"
        if any(token in text for token in ("candlestick", "свечн", "ohlc")):
            return "candlestick"
        if any(token in text for token in ("гист", "столб", "bar", "waterfall", "факторн")):
            return "bar"
        if any(token in text for token in ("карта", "map")):
            return "country_map"
        if _GAUGE_TITLE_RE.search(text):
            return "big_number"
        return None

    @classmethod
    def _refine_chart_type(cls, chart: Dict[str, Any], allowed_set: set[str]) -> str:
        current = cls._coerce_chart_type(chart.get("chart_type") or chart.get("type"), allowed_set)
        title = cls._chart_title(chart)
        title_hint = cls._type_from_title(title)
        block_kind = cls._block_kind_from_type(chart.get("block_kind") or chart.get("chart_type"))
        widget_family = str(chart.get("widget_family") or "").lower()
        categories = chart.get("categories") if isinstance(chart.get("categories"), list) else []
        point_count = cls._chart_point_count(chart)

        # Chart types that indicate a visual chart (not a table)
        visual_chart_types = {"line", "bar", "bar_horizontal", "area", "pie", "donut", "scatter", "combo", "funnel", "radar", "treemap", "sunburst", "sankey", "country_map", "gantt"}

        if cls._chart_has_rows(chart):
            # If chart already has a visual type AND has series data, don't downgrade to table
            # This prevents line/bar charts with extracted table rows from becoming tables
            if current in visual_chart_types and point_count > 0:
                pass  # Keep current visual type, don't convert to table
            elif cls._rows_look_like_transposed_series(chart, title):
                return "line" if "line" in allowed_set else ("bar" if "bar" in allowed_set else current)
            # Check if rows represent a stacked bar chart (temporal rows with multiple numeric columns)
            elif cls._rows_look_like_stacked_bar(chart, title):
                return "bar_horizontal" if "bar_horizontal" in allowed_set else "bar"
            else:
                return "pivot_table" if current == "pivot_table" else "table"

        # Detect stacked horizontal bar by legend + title even without rows
        # (vision may recognize it as table but with legend_items from colored segments)
        if cls._looks_like_stacked_bar_without_rows(chart, title):
            return "bar_horizontal" if "bar_horizontal" in allowed_set else "bar"

        # bar with ≥3 named series and data is almost always a stacked/grouped horizontal bar
        if current == "bar" and "bar_horizontal" in allowed_set:
            series_list = chart.get("series")
            if isinstance(series_list, list):
                named_with_data = [
                    s for s in series_list
                    if isinstance(s, dict)
                    and str(s.get("name") or "").strip()
                    and isinstance(s.get("data"), list) and s["data"]
                ]
                if len(named_with_data) >= 3:
                    return "bar_horizontal"

        if cls._series_has_xy_points(chart.get("series")):
            return "scatter" if "scatter" in allowed_set else current
        if block_kind == "table":
            # Double-check: if it has legend and dynamic title, it's probably stacked bar
            if cls._looks_like_stacked_bar_without_rows(chart, title):
                return "bar_horizontal" if "bar_horizontal" in allowed_set else "bar"
            return "pivot_table" if current == "pivot_table" else "table"
        if block_kind == "map" or "country_map" in widget_family or widget_family.startswith("nav:14") or widget_family.startswith("nav:83") or widget_family.startswith("nav:90") or widget_family.startswith("nav:97") or widget_family.startswith("nav:115"):
            return "country_map" if "country_map" in allowed_set else current
        if block_kind == "gauge":
            return "gauge" if "gauge" in allowed_set else ("big_number" if "big_number" in allowed_set else current)

        if current == "big_number":
            if categories and point_count > 2:
                if cls._is_temporal_categories(categories) and "line" in allowed_set:
                    return "line"
                return "bar" if "bar" in allowed_set else current
            return current

        if title_hint and title_hint in allowed_set:
            if title_hint in {"sankey", "sunburst", "treemap", "funnel", "radar", "country_map", "scatter", "gantt"}:
                return title_hint
            # pie/donut inferred from title strongly overrides bar/line when no explicit data present
            if title_hint in {"pie", "donut"} and current in {"bar", "line", "table"}:
                return title_hint
            # Only override when current type is generic (table) or title_hint is more specific
            if current == "table" or title_hint == "bar_horizontal":
                return title_hint

        if current == "table" and categories and cls._is_temporal_categories(categories):
            if title_hint not in {"bar", "bar_horizontal"} and "line" in allowed_set:
                return "line"

        if current == "line" and title_hint in {"bar", "bar_horizontal"}:
            return title_hint if title_hint in allowed_set else current

        if current == "bar" and title_hint == "line" and "line" in allowed_set:
            return "line"

        if current == "table" and title_hint in {"pie", "donut", "line", "bar", "bar_horizontal", "combo"}:
            return title_hint if title_hint in allowed_set else current

        return current

