"""DataLens export builder."""

from __future__ import annotations

import random
from typing import Any, Dict, List, Optional

from .datalens_helpers import (
    _GRID_WIDTH,
    _auto_fields,
    _extract_categories,
    _extract_kpi_value,
    _extract_series,
    _is_metric_chart,
    _make_bar_js,
    _make_empty_js,
    _make_hash,
    _short_id,
    _make_line_js,
    _make_metric_js,
    _make_pie_js,
    _make_table_js,
    _make_editor_widget,
    _rows_to_pie,
    _try_float,
    _try_int,
    _rows_to_series_and_categories,
    _sanitize_title,
)

__all__ = [
    "DataLensExportBuilder",
    "_auto_fields",
    "_extract_categories",
    "_extract_kpi_value",
    "_extract_series",
    "_make_bar_js",
    "_make_empty_js",
    "_make_hash",
    "_make_line_js",
    "_make_metric_js",
    "_make_pie_js",
    "_make_table_js",
    "_rows_to_pie",
    "_rows_to_series_and_categories",
    "_sanitize_title",
    "_try_float",
    "_try_int",
]


class DataLensExportBuilder:
    """Строит DataLens JSON из payload дашборда AI Platform.

    Использует advanced-chart_node (DataLens Editor) с данными, зашитыми
    в JavaScript — не требует датасетов или подключений.
    """

    def __init__(self, payload: Dict[str, Any]) -> None:
        self.payload = payload
        self.dashboard_title: str = _sanitize_title(payload.get("dashboard_title") or "", "Dashboard")
        self.charts: List[Dict] = payload.get("charts") or []
        self.tables: List[Dict] = payload.get("tables") or []
        if not self.tables and isinstance(payload.get("raw_table"), dict):
            raw_table = payload["raw_table"]
            self.tables = [{
                "table_name": raw_table.get("table_name") or "FactDashboardRaw",
                "columns": raw_table.get("columns") or [],
                "rows": raw_table.get("rows") or [],
            }]
        self.kpi_rows: List[Dict] = payload.get("kpi_rows") or []
        self.layout: List[Dict] = payload.get("layout") or []
        self.chart_meta: Dict[str, Any] = payload.get("chart_meta") or {}
        inline_limit_raw = payload.get("inline_row_limit")
        parsed_inline_limit = _try_int(inline_limit_raw, 0) if inline_limit_raw not in (None, "") else 0
        self.inline_row_limit: Optional[int] = parsed_inline_limit if parsed_inline_limit > 0 else None
        self._widget_counter = 0
        self._widgets: Dict[str, Any] = {}
        self._datasets: Dict[str, Any] = {}
        self._tab_items: List[Dict] = []
        self._tab_layout: List[Dict] = []
        # Индекс таблиц по имени для быстрого поиска
        self._tables_by_name: Dict[str, Dict] = {}
        for tbl in self.tables:
            name = tbl.get("table_name") or ""
            if name:
                self._tables_by_name[name] = tbl
                # также нормализованный ключ
                self._tables_by_name[name.lower()] = tbl

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build(self) -> Dict[str, Any]:
        self._build_dataset_entries()
        self._build_kpi_widgets()
        self._build_chart_widgets()
        if not self.charts:
            self._build_table_widgets()

        dash_entry = self._build_dash_entry()

        export_data = {
            "export": {
                "version": "v1",
                "entries": {
                    "dash": {"1": dash_entry},
                    "dataset": self._datasets,
                    "widget": self._widgets,
                },
            }
        }
        export_data["hash"] = _make_hash(export_data["export"])
        return export_data

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _next_widget_id(self) -> str:
        self._widget_counter += 1
        return str(self._widget_counter)

    def _get_layout_item(
        self,
        *,
        slice_name: str = "",
        dataset: str = "",
        item_id: str = "",
    ) -> Optional[Dict]:
        # Build name-only candidates (slice_name, id) — no dataset to avoid
        # false matches when all charts share the same dataset name.
        name_candidates = {
            str(value).strip()
            for value in (slice_name, item_id)
            if str(value or "").strip()
        }
        if slice_name and not slice_name.lower().startswith("kpi:"):
            name_candidates.add(f"KPI: {slice_name}")

        for item in self.layout:
            item_name_candidates = {
                str(value).strip()
                for value in (
                    item.get("slice_name"),
                    item.get("label"),
                    item.get("id"),
                )
                if str(value or "").strip()
            }
            if name_candidates & item_name_candidates:
                return item

        # Fallback: match by dataset only when it is non-empty and looks unique
        # (skip generic shared table names like 'FactDashboardRaw').
        if dataset and str(dataset).strip():
            dataset_str = str(dataset).strip()
            dataset_matches = [
                item for item in self.layout
                if str(item.get("dataset") or "").strip() == dataset_str
            ]
            if len(dataset_matches) == 1:
                return dataset_matches[0]

        return None

    def _grid_from_layout_item(self, layout_item: Dict) -> Dict[str, int]:
        if layout_item:
            if any(key in layout_item for key in ("col", "row", "width", "height")):
                x_raw = _try_int(layout_item.get("col", layout_item.get("x", 0)))
                y_raw = _try_int(layout_item.get("row", layout_item.get("y", 0)))
                w_raw = _try_int(layout_item.get("width", layout_item.get("w", 6)), 6)
                h_raw = _try_int(layout_item.get("height", layout_item.get("h", 8)), 8)
                h_units = h_raw if h_raw <= 20 else int(round(h_raw / 20))
                scale = _GRID_WIDTH / 12
                return {
                    "x": max(0, int(x_raw * scale)),
                    "y": max(0, y_raw),
                    "w": max(6, min(_GRID_WIDTH, int(w_raw * scale))),
                    "h": max(4, h_units),
                }
            x_raw = _try_int(layout_item.get("x", 0))
            y_raw = _try_int(layout_item.get("y", 0))
            w_raw = _try_int(layout_item.get("w", 12), 12)
            h_raw = _try_int(layout_item.get("h", 8), 8)
            scale = _GRID_WIDTH / 24
            return {
                "x": max(0, int(x_raw * scale)),
                "y": max(0, y_raw),
                "w": max(6, min(_GRID_WIDTH, int(w_raw * scale))),
                "h": max(4, h_raw),
            }
        return {}

    def _get_layout_for_chart(self, chart: Dict) -> Optional[Dict]:
        return self._get_layout_item(
            slice_name=str(chart.get("slice_name") or chart.get("name") or ""),
            dataset=str(chart.get("dataset") or chart.get("table_name") or ""),
            item_id=str(chart.get("id") or ""),
        )

    def _get_layout_for_kpi(self, kpi: Dict) -> Optional[Dict]:
        metric_name = str(
            kpi.get("title")
            or kpi.get("metric_name")
            or kpi.get("name")
            or kpi.get("label")
            or kpi.get("metric_code")
            or ""
        )
        return self._get_layout_item(
            slice_name=metric_name,
            dataset=str(kpi.get("dataset") or kpi.get("table_name") or ""),
        )

    def _chart_to_grid(self, chart: Dict, index: int) -> Dict[str, int]:
        layout_item = self._get_layout_for_chart(chart)
        if layout_item:
            grid = self._grid_from_layout_item(layout_item)
            if grid:
                return grid
        col = index % 2
        row = index // 2
        w = _GRID_WIDTH // 2
        return {"x": col * w, "y": row * 12, "w": w, "h": 12}

    def _kpi_to_grid(self, kpi: Dict, index: int, total: int) -> Dict[str, int]:
        layout_item = self._get_layout_for_kpi(kpi)
        if layout_item:
            grid = self._grid_from_layout_item(layout_item)
            if grid:
                return grid
        cols = min(total, 4)
        w = _GRID_WIDTH // cols
        return {"x": (index % cols) * w, "y": 0, "w": w, "h": 4}

    def _table_to_grid(self, index: int, chart_rows: int) -> Dict[str, int]:
        y_offset = chart_rows * 12 + 4
        return {"x": 0, "y": y_offset + index * 16, "w": _GRID_WIDTH, "h": 14}

    # ------------------------------------------------------------------
    # Dataset entries
    # ------------------------------------------------------------------

    def _infer_data_type(self, values: List[Any]) -> str:
        clean = [value for value in values if value not in (None, "")]
        if clean and all(_try_float(value) is not None for value in clean):
            return "integer" if all(float(str(value).replace(",", ".")).is_integer() for value in clean) else "float"
        return "string"

    def _dataset_rows(self, table: Dict, columns: List[str]) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        source_rows = table.get("rows") or []
        if self.inline_row_limit is not None:
            source_rows = source_rows[:self.inline_row_limit]
        for row in source_rows:
            if isinstance(row, dict):
                rows.append({col: row.get(col) for col in columns})
            elif isinstance(row, (list, tuple)):
                rows.append({col: row[index] if index < len(row) else None for index, col in enumerate(columns)})
        return rows

    def _build_dataset_entries(self) -> None:
        for index, table in enumerate(self.tables):
            if not isinstance(table, dict):
                continue
            table_name = _sanitize_title(table.get("table_name") or f"Dataset {index + 1}", f"Dataset {index + 1}")
            columns = [str(col) for col in (table.get("columns") or [])]
            rows_raw = table.get("rows") or []
            if not columns and rows_raw and isinstance(rows_raw[0], dict):
                columns = [str(col) for col in rows_raw[0].keys()]
            if not columns:
                continue

            rows = self._dataset_rows(table, columns)
            fields = []
            for col in columns:
                values = [row.get(col) for row in rows[:100]]
                data_type = self._infer_data_type(values)
                fields.append({
                    "name": col,
                    "title": col,
                    "data_type": data_type,
                    "cast": data_type,
                    "type": "MEASURE" if data_type in {"integer", "float"} and col not in {"widget_id"} else "DIMENSION",
                })

            dataset_id = str(index + 1)
            self._datasets[dataset_id] = {
                "dataset": {
                    "name": table_name,
                    "annotation": {"description": "Inline dataset exported from Data Agent"},
                    "data": {
                        "source": "inline",
                        "table_name": table_name,
                        "fields": fields,
                        "columns": columns,
                        "rows": rows,
                        "row_count": len(rows),
                    },
                }
            }

    # ------------------------------------------------------------------
    # KPI widgets
    # ------------------------------------------------------------------

    def _build_kpi_widgets(self) -> None:
        total = len(self.kpi_rows)
        for i, kpi in enumerate(self.kpi_rows):
            widget_id = self._next_widget_id()
            item_id = _short_id()

            title = _sanitize_title(
                kpi.get("title")
                or kpi.get("metric_name")
                or kpi.get("name")
                or kpi.get("label")
                or kpi.get("metric_code")
                or f"KPI {i+1}"
            )
            value = _extract_kpi_value(kpi)
            unit = str(kpi.get("unit") or kpi.get("suffix") or "")

            prepare_js = _make_metric_js(title, value, unit)
            self._widgets[widget_id] = _make_editor_widget(title, prepare_js)

            grid = self._kpi_to_grid(kpi, i, total)
            self._add_tab_item(item_id, widget_id, title, grid)

    # ------------------------------------------------------------------
    # Chart widgets
    # ------------------------------------------------------------------

    def _find_table_for_chart(self, chart: Dict) -> Optional[Dict]:
        """Находит таблицу с данными для чарта, фильтруя строки по widget_id."""
        tbl = None
        for key in ("dataset", "table_name", "slice_name"):
            name = chart.get(key) or ""
            if name:
                tbl = self._tables_by_name.get(name) or self._tables_by_name.get(name.lower())
                if tbl:
                    break
        if not tbl:
            return None
        # Filter rows by widget_id if the table has that column and chart provides filter_value
        filter_value = str(chart.get("filter_value") or chart.get("id") or "").strip()
        if not filter_value:
            return tbl
        rows = tbl.get("rows") or []
        columns = tbl.get("columns") or []
        if "widget_id" not in columns:
            return tbl
        filtered_rows = [
            row for row in rows
            if str((row.get("widget_id") if isinstance(row, dict) else None) or "") == filter_value
        ]
        if not filtered_rows:
            return tbl
        return {**tbl, "rows": filtered_rows}

    def _normalize_table_content(self, tbl: Optional[Dict]) -> tuple[list[str], list[Any]]:
        if not isinstance(tbl, dict):
            return ([], [])
        columns = tbl.get("columns") or []
        rows_raw = tbl.get("rows") or []
        if not columns and rows_raw and isinstance(rows_raw[0], dict):
            columns = list(rows_raw[0].keys())
        return ([str(col) for col in columns], rows_raw[:200])

    def _build_chart_widgets(self) -> None:
        kpi_titles = {
            _sanitize_title(k.get("title") or k.get("metric_name") or k.get("name") or "").lower()
            for k in self.kpi_rows
        }

        for i, chart in enumerate(self.charts):
            title = _sanitize_title(
                chart.get("slice_name") or chart.get("name") or chart.get("title") or f"График {i+1}"
            )
            chart_type = (chart.get("chart_type") or chart.get("viz_type") or chart.get("type") or "bar").lower()

            # Skip big_number charts that are already rendered as KPI widgets
            if _is_metric_chart(chart) and title.lower() in kpi_titles:
                continue

            widget_id = self._next_widget_id()
            item_id = _short_id()

            # Нормализация типов
            if "donut" in chart_type or "doughnut" in chart_type or "кольц" in chart_type:
                chart_type = "donut"
            elif "pie" in chart_type or "круг" in chart_type:
                chart_type = "pie"
            elif "line" in chart_type or "area" in chart_type or "линейн" in chart_type:
                chart_type = "line"
            elif "bar" in chart_type or "column" in chart_type or "столб" in chart_type or "гист" in chart_type:
                chart_type = "bar"

            # Получаем chart_meta для x_field/y_field
            meta = self.chart_meta.get(chart.get("slice_name") or "") or self.chart_meta.get(chart.get("dataset") or "") or {}
            x_field = str(meta.get("x_field") or chart.get("x_field") or "")
            y_field = str(meta.get("y_field") or chart.get("y_field") or "")
            series_field = str(meta.get("series_field") or chart.get("series_field") or "")

            # Пробуем взять данные из таблицы
            tbl = self._find_table_for_chart(chart)

            # Данные могут быть уже вшиты в чарт (legacy)
            inline_series = _extract_series(chart)
            inline_cats = _extract_categories(chart)

            if _is_metric_chart(chart):
                value = inline_series[0]["data"][0] if inline_series and inline_series[0]["data"] else _extract_kpi_value(chart)
                if value is None and tbl:
                    rows = tbl.get("rows") or []
                    cols = tbl.get("columns") or []
                    if rows:
                        row0 = rows[0]
                        yf = y_field or (cols[-1] if cols else "")
                        if isinstance(row0, dict):
                            value = _try_float(row0.get(yf))
                        elif isinstance(row0, (list, tuple)) and cols:
                            idx = cols.index(yf) if yf in cols else -1
                            value = _try_float(row0[idx]) if idx >= 0 < len(row0) else None
                prepare_js = _make_metric_js(title, value)

            elif chart_type in ("pie", "donut"):
                if tbl:
                    labels, values = _rows_to_pie(tbl, x_field, y_field)
                elif inline_cats or inline_series:
                    labels = inline_cats or [s["name"] for s in inline_series]
                    values = inline_series[0]["data"] if inline_series else []
                else:
                    labels, values = [], []
                prepare_js = _make_pie_js(title, labels, values)

            elif chart_type == "line":
                if tbl:
                    categories, series = _rows_to_series_and_categories(tbl, x_field, y_field, series_field)
                elif inline_cats or inline_series:
                    categories, series = inline_cats, inline_series
                else:
                    categories, series = [], []
                prepare_js = _make_line_js(title, categories, series)

            elif chart_type == "table":
                columns, rows = self._normalize_table_content(tbl)
                if not columns and inline_cats:
                    columns = [str(cat) for cat in inline_cats]
                prepare_js = _make_table_js(title, columns, rows) if columns else _make_empty_js(title)

            else:  # bar and fallback
                if tbl:
                    categories, series = _rows_to_series_and_categories(tbl, x_field, y_field, series_field)
                elif inline_cats or inline_series:
                    categories, series = inline_cats, inline_series
                else:
                    categories, series = [], []
                prepare_js = _make_bar_js(title, categories, series) if (categories or series) else _make_empty_js(title)

            self._widgets[widget_id] = _make_editor_widget(title, prepare_js)

            grid = self._chart_to_grid(chart, i)
            self._add_tab_item(item_id, widget_id, title, grid)

    # ------------------------------------------------------------------
    # Table widgets
    # ------------------------------------------------------------------

    def _build_table_widgets(self) -> None:
        chart_rows = max(1, (len(self.charts) + 1) // 2)
        for i, tbl in enumerate(self.tables):
            tbl_name = _sanitize_title(tbl.get("table_name") or f"Таблица {i+1}")
            columns = tbl.get("columns") or []
            rows_raw = tbl.get("rows") or []

            if not columns and rows_raw:
                first = rows_raw[0]
                if isinstance(first, dict):
                    columns = list(first.keys())

            # Нормализуем строки — каждая строка как список значений
            rows_normalized: List[List] = []
            for row in rows_raw[:200]:
                if isinstance(row, dict):
                    rows_normalized.append([row.get(c) for c in columns])
                elif isinstance(row, (list, tuple)):
                    rows_normalized.append(list(row))
                else:
                    rows_normalized.append([row])

            widget_id = self._next_widget_id()
            item_id = _short_id()

            prepare_js = _make_table_js(tbl_name, columns, rows_normalized)
            self._widgets[widget_id] = _make_editor_widget(tbl_name, prepare_js)

            grid = self._table_to_grid(i, chart_rows)
            self._add_tab_item(item_id, widget_id, tbl_name, grid, auto_height=True)

    # ------------------------------------------------------------------
    # Dash entry
    # ------------------------------------------------------------------

    def _add_tab_item(self, item_id: str, widget_id: str, title: str, grid: Dict, auto_height: bool = False) -> None:
        tab_id = _short_id()
        self._tab_items.append({
            "id": item_id,
            "type": "widget",
            "namespace": "default",
            "data": {
                "hideTitle": True,
                "tabs": [
                    {
                        "id": tab_id,
                        "title": title,
                        "hint": "",
                        "params": {},
                        "chartId": widget_id,
                        "isDefault": True,
                        "autoHeight": auto_height,
                        "background": {"color": "transparent"},
                        "enableHint": False,
                        "description": "",
                        "enableDescription": False,
                    }
                ],
            },
        })
        self._tab_layout.append({
            "i": item_id,
            "x": grid["x"],
            "y": grid["y"],
            "w": grid["w"],
            "h": grid["h"],
        })

    def _build_dash_entry(self) -> Dict:
        return {
            "dash": {
                "name": self.dashboard_title,
                "annotation": {"description": ""},
                "data": {
                    "salt": str(random.random()),
                    "schemeVersion": 8,
                    "counter": len(self._tab_items) + 1,
                    "settings": {
                        "hideTabs": True,
                        "expandTOC": False,
                        "globalParams": {},
                        "loadPriority": "charts",
                        "hideDashTitle": False,
                        "silentLoading": False,
                        "autoupdateInterval": None,
                        "dependentSelectors": False,
                        "loadOnlyVisibleCharts": True,
                        "maxConcurrentRequests": None,
                    },
                    "tabs": [
                        {
                            "id": _short_id(),
                            "title": self.dashboard_title,
                            "items": self._tab_items,
                            "layout": self._tab_layout,
                            "aliases": {},
                            "connections": [],
                        }
                    ],
                },
            }
        }
