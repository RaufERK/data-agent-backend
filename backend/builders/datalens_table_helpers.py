"""Table/row conversion helpers for DataLens export."""
from __future__ import annotations

from typing import Any, Dict, List

from .datalens_helpers import _try_float

_INTERNAL_COLS = {"widget_id"}

def _auto_fields(columns: List[str], rows: List[Any]) -> tuple:
    """Автоматически определяет категориальное и числовое поля."""
    if not columns or not rows:
        return ("", "")
    # Пробуем определить числовые колонки
    numeric_cols = []
    cat_cols = []
    for col in columns:
        if col in _INTERNAL_COLS:
            continue
        vals = []
        for row in rows[:20]:
            if isinstance(row, dict):
                v = row.get(col)
            elif isinstance(row, (list, tuple)) and len(row) > columns.index(col):
                v = row[columns.index(col)]
            else:
                v = None
            if v is not None:
                vals.append(v)
        if vals and all(_try_float(v) is not None for v in vals):
            numeric_cols.append(col)
        else:
            cat_cols.append(col)
    cat = cat_cols[0] if cat_cols else (columns[0] if columns else "")
    num = numeric_cols[0] if numeric_cols else (columns[-1] if columns else "")
    return (cat, num)


def _rows_to_series_and_categories(
    tbl: Dict,
    x_field: str = "",
    y_field: str = "",
    series_field: str = "",
) -> tuple:
    """Извлекает из таблицы categories + series для чартов.

    Возвращает (categories, series) в формате для _make_bar_js/_make_line_js.
    """
    columns = tbl.get("columns") or []
    rows_raw = tbl.get("rows") or []
    if not rows_raw:
        return ([], [])

    # Нормализуем строки в dict
    def row_to_dict(row):
        if isinstance(row, dict):
            return row
        if isinstance(row, (list, tuple)):
            return {columns[i]: row[i] for i in range(min(len(columns), len(row)))}
        return {}

    rows = [row_to_dict(r) for r in rows_raw[:500]]

    # Определяем поля если не заданы
    if not x_field or not y_field:
        auto_cat, auto_num = _auto_fields(columns, rows_raw)
        if not x_field:
            x_field = auto_cat
        if not y_field:
            y_field = auto_num

    if not x_field or not y_field:
        return ([], [])

    # Определяем все числовые колонки кроме x_field (для pivot/multi-series)
    auto_cat, _ = _auto_fields(columns, rows_raw)
    numeric_cols = [c for c in columns if c != x_field and c not in _INTERNAL_COLS and _try_float(rows[0].get(c) if rows else None) is not None]

    if series_field and series_field in columns and series_field != x_field:
        # Явно заданный series_field — длинный формат
        series_map: Dict[str, Dict[str, float]] = {}
        categories_order: List[str] = []
        seen_cats: set = set()
        for row in rows:
            cat = str(row.get(x_field, ""))
            ser = str(row.get(series_field, "Значение"))
            val = _try_float(row.get(y_field)) or 0.0
            if cat not in seen_cats:
                categories_order.append(cat)
                seen_cats.add(cat)
            if ser not in series_map:
                series_map[ser] = {}
            series_map[ser][cat] = series_map[ser].get(cat, 0.0) + val
        series = [
            {"name": ser, "data": [series_map[ser].get(cat, 0.0) for cat in categories_order]}
            for ser in series_map
        ]
        return (categories_order, series)
    elif len(numeric_cols) > 1:
        # Pivot-формат: x_field = категория/дата, остальные числовые = серии
        categories = [str(row.get(x_field, "")) for row in rows]
        series = [
            {"name": col, "data": [_try_float(row.get(col)) or 0.0 for row in rows]}
            for col in numeric_cols[:10]  # не более 10 серий
        ]
        return (categories, series)
    else:
        # Одна серия
        categories = []
        values = []
        for row in rows:
            cat = str(row.get(x_field, ""))
            val = _try_float(row.get(y_field)) or 0.0
            categories.append(cat)
            values.append(val)
        return (categories, [{"name": y_field, "data": values}])


def _rows_to_pie(tbl: Dict, label_field: str = "", value_field: str = "") -> tuple:
    """Извлекает labels + values для круговой диаграммы."""
    columns = tbl.get("columns") or []
    rows_raw = tbl.get("rows") or []
    if not rows_raw:
        return ([], [])

    if not label_field or not value_field:
        auto_cat, auto_num = _auto_fields(columns, rows_raw)
        if not label_field:
            label_field = auto_cat
        if not value_field:
            value_field = auto_num

    if not label_field or not value_field:
        return ([], [])

    labels = []
    values = []
    for row in rows_raw[:50]:
        if isinstance(row, dict):
            label = str(row.get(label_field, ""))
            val = _try_float(row.get(value_field)) or 0.0
        elif isinstance(row, (list, tuple)):
            col_idx_l = columns.index(label_field) if label_field in columns else 0
            col_idx_v = columns.index(value_field) if value_field in columns else -1
            label = str(row[col_idx_l]) if col_idx_l < len(row) else ""
            val = _try_float(row[col_idx_v]) if col_idx_v >= 0 and col_idx_v < len(row) else 0.0
        else:
            continue
        labels.append(label)
        values.append(val or 0.0)
    return (labels, values)
