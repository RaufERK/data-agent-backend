"""Shared chart type normalization utilities."""
from __future__ import annotations

from typing import Any

# Mapping: alias/synonym -> canonical chart type
# Order matters only for documentation; lookup is O(1) via the flat dict below.
_ALIASES: dict[str, str] = {
    # big_number / KPI
    "kpi": "big_number",
    "number": "big_number",
    "big_number_total": "big_number",
    # gauge
    "gauge": "gauge",
    "progress": "gauge",
    "thermometer": "gauge",
    "pictorial": "gauge",
    # combo
    "combo": "combo",
    "mixed": "combo",
    "mixed_chart": "combo",
    # sankey
    "sunkey": "sankey",
    "санки": "sankey",
    "санкей": "sankey",
    # gantt
    "gant": "gantt",
    "gantt": "gantt",
    "гант": "gantt",
    "timeline": "gantt",
    # donut
    "donut": "donut",
    "doughnut": "donut",
    # bar_horizontal
    "bar_horizontal": "bar_horizontal",
    "horizontal_bar": "bar_horizontal",
    "horizontal": "bar_horizontal",
    "race_chart": "bar_horizontal",
    "race": "bar_horizontal",
    # pivot_table
    "pivot_table_v2": "pivot_table",
    "pivot_table": "pivot_table",
    # sunburst
    "sunburst_v2": "sunburst",
    "sunburst": "sunburst",
    # treemap
    "treemap_v2": "treemap",
    "treemap": "treemap",
    # scatter / bubble
    "bubble": "scatter",
    "bubble_chart": "scatter",
    "quadrant": "scatter",
    # candlestick — native type, not collapsed to bar
    "candlestick": "candlestick",
    "candlestick_chart": "candlestick",
    "ohlc": "candlestick",
    "waterfall": "bar",
    # country_map
    "country_map": "country_map",
    "map": "country_map",
    # mosaic_map — stays as-is in dashboard_vision, treated as country_map in drawing
    "mosaic_map": "mosaic_map",
    # pass-through canonical types
    "bar": "bar",
    "line": "line",
    "area": "area",
    "pie": "pie",
    "scatter": "scatter",
    "table": "table",
    "big_number": "big_number",
    "sankey": "sankey",
    "funnel": "funnel",
    "radar": "radar",
    "image": "image",
}


def normalize_chart_type(value: Any, *, default: str = "table") -> str:
    """Return canonical chart type string for *value*, falling back to *default*."""
    chart_type = str(value or "").strip().lower()
    return _ALIASES.get(chart_type, default)
