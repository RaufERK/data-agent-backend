"""Foresight compiler-ready export helpers.

This builder does not emit a native `.pefx` package yet.
It emits a stable compiler-ready JSON contract:

chat payload -> normalized datasets -> semantic model -> widgets -> layout

The goal is to have an explicit intermediate representation that can later
be compiled into native Foresight repository objects.
"""

from __future__ import annotations

from typing import Any, List, Optional


def _normalize_text(value: Any, fallback: str = "") -> str:
    text = str(value or "").strip()
    return text or fallback


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y"}:
        return True
    if text in {"0", "false", "no", "n"}:
        return False
    return default


def _coerce_number(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        text = value.strip().replace("\u00a0", " ").replace(",", ".")
        if not text:
            return value
        try:
            if "." in text:
                return float(text)
            return int(text)
        except Exception:
            return value
    return value


def _infer_column_type(values: List[Any]) -> str:
    non_null = [v for v in values if v not in (None, "")]
    if not non_null:
        return "string"

    if all(isinstance(v, bool) for v in non_null):
        return "boolean"

    numeric_count = 0
    date_like_count = 0
    for value in non_null:
        coerced = _coerce_number(value)
        if isinstance(coerced, (int, float)) and not isinstance(coerced, bool):
            numeric_count += 1
            continue
        text = str(value).strip()
        if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
            date_like_count += 1

    if numeric_count == len(non_null):
        return "number"
    if date_like_count == len(non_null):
        return "date"
    return "string"


def _field_role(field_name: str, field_type: str) -> str:
    name = str(field_name or "").strip().lower()
    if field_type == "date":
        return "date"
    if field_type == "number":
        if any(token in name for token in ("profit", "revenue", "sales", "amount", "margin", "count", "share", "units")):
            return "measure"
        return "measure"
    return "dimension"


def _default_aggregation(field_name: str, field_role: str) -> Optional[str]:
    if field_role != "measure":
        return None
    name = str(field_name or "").strip().lower()
    if any(token in name for token in ("margin", "share", "ratio", "avg")):
        return "avg"
    return "sum"


def _normalize_chart_type(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"pie", "donut", "doughnut"}:
        return "pie"
    if text in {"bar", "column", "histogram"}:
        return "bar"
    if text in {"line", "spline", "area"}:
        return "line"
    if text in {"table", "grid"}:
        return "table"
    if text in {"candlestick", "candle", "ohlc"}:
        return "candlestick"
    return "bar"
