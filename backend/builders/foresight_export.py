"""Foresight export builder."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from .foresight_helpers import (
    _coerce_bool,
    _coerce_number,
    _default_aggregation,
    _field_role,
    _infer_column_type,
    _normalize_chart_type,
    _normalize_text,
)

__all__ = [
    "ForesightExportBuilder",
    "_coerce_bool",
    "_coerce_number",
    "_default_aggregation",
    "_field_role",
    "_infer_column_type",
    "_normalize_chart_type",
]


class ForesightExportBuilder:
    """Builds a compiler-ready Foresight dashboard specification."""

    FORMAT = "foresight_compiler_bundle"
    VERSION = "v2"

    def __init__(self, payload: Dict[str, Any]) -> None:
        self.payload = payload or {}
        self.dashboard_id = self.payload.get("dashboard_id")
        self.dashboard_title = _normalize_text(
            self.payload.get("dashboard_title"), "Dashboard"
        )
        self.slug = _normalize_text(self.payload.get("slug"), "dashboard")
        self.subject_area_name = _normalize_text(
            self.payload.get("subject_area_name") or self.payload.get("subject_area"),
            "AI",
        )
        self.tables = (
            self.payload.get("tables")
            if isinstance(self.payload.get("tables"), list)
            else []
        )
        self.charts = (
            self.payload.get("charts")
            if isinstance(self.payload.get("charts"), list)
            else []
        )
        self.table_widgets = (
            self.payload.get("table_widgets")
            if isinstance(self.payload.get("table_widgets"), list)
            else []
        )
        self.layout = (
            self.payload.get("layout")
            if isinstance(self.payload.get("layout"), list)
            else []
        )
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
        self.inline_data = _coerce_bool(self.payload.get("inline_data"), True)
        inline_limit_raw = self.payload.get("inline_row_limit")
        try:
            parsed_inline_limit = int(inline_limit_raw) if inline_limit_raw not in (None, "") else 0
        except (TypeError, ValueError):
            parsed_inline_limit = 0
        self.inline_row_limit = parsed_inline_limit if parsed_inline_limit > 0 else None

    def build(self) -> Dict[str, Any]:
        generated_at = datetime.now(timezone.utc).isoformat()
        datasets = self._build_datasets()
        dataset_map = {dataset["id"]: dataset for dataset in datasets}
        semantic_model = self._build_semantic_model(datasets)
        widgets = self._build_widgets(dataset_map)
        layout = self._build_layout(widgets)
        compiler_plan = self._build_compiler_plan(datasets, widgets)

        return {
            "format": self.FORMAT,
            "version": self.VERSION,
            "generated_at": generated_at,
            "target_product": "Foresight Analytics Platform",
            "native_import_supported": False,
            "native_import_format": ".pefx",
            "compiler_stage": "normalized_spec",
            "notes": [
                "This file is compiler-ready, not directly importable into Foresight.",
                "Next compiler stage must translate datasets and widgets into native repository objects.",
                "The purpose of this spec is deterministic generation from chat payloads.",
            ],
            "dashboard": {
                "id": self.dashboard_id,
                "title": self.dashboard_title,
                "slug": self.slug,
                "subject_area_name": self.subject_area_name,
            },
            "datasets": datasets,
            "semantic_model": semantic_model,
            "widgets": widgets,
            "layout": layout,
            "chart_meta": self.chart_meta,
            "compiler_plan": compiler_plan,
            "payload_shape": {
                "tables_count": len(datasets),
                "widgets_count": len(widgets),
                "chart_count": len([w for w in widgets if w.get("kind") == "chart"]),
                "table_count": len([w for w in widgets if w.get("kind") == "table"]),
                "kpi_count": len([w for w in widgets if w.get("kind") == "kpi"]),
            },
        }

    def _build_datasets(self) -> List[Dict[str, Any]]:
        datasets: List[Dict[str, Any]] = []
        for idx, table in enumerate(self.tables):
            if not isinstance(table, dict):
                continue

            table_name = _normalize_text(table.get("table_name"), f"dataset_{idx + 1}")
            rows = table.get("rows") if isinstance(table.get("rows"), list) else []
            columns = (
                table.get("columns")
                if isinstance(table.get("columns"), list)
                else []
            )
            if not columns and rows and isinstance(rows[0], dict):
                columns = list(rows[0].keys())

            source_rows = rows[: self.inline_row_limit] if self.inline_row_limit is not None else rows
            normalized_rows: List[Dict[str, Any]] = []
            for row in source_rows:
                if not isinstance(row, dict):
                    continue
                normalized_rows.append(
                    {key: _coerce_number(value) for key, value in row.items()}
                )

            field_defs: List[Dict[str, Any]] = []
            for col in columns:
                values = [row.get(col) for row in normalized_rows]
                field_type = _infer_column_type(values)
                role = _field_role(str(col), field_type)
                field_defs.append(
                    {
                        "name": str(col),
                        "type": field_type,
                        "role": role,
                        "aggregation": _default_aggregation(str(col), role),
                    }
                )

            datasets.append(
                {
                    "id": table_name,
                    "name": table_name,
                    "kind": "tabular",
                    "row_count": len(normalized_rows),
                    "fields": field_defs,
                    "rows": normalized_rows if self.inline_data else [],
                }
            )

        return datasets

    def _build_semantic_model(self, datasets: List[Dict[str, Any]]) -> Dict[str, Any]:
        entities: List[Dict[str, Any]] = []
        for dataset in datasets:
            fields = dataset.get("fields") or []
            dimensions = [field for field in fields if field.get("role") in {"dimension", "date"}]
            measures = [field for field in fields if field.get("role") == "measure"]
            entities.append(
                {
                    "dataset_id": dataset["id"],
                    "dataset_name": dataset["name"],
                    "dimensions": dimensions,
                    "measures": measures,
                }
            )
        return {
            "subject_area_name": self.subject_area_name,
            "entities": entities,
        }

    def _build_widgets(self, dataset_map: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        widgets: List[Dict[str, Any]] = []
        widgets.extend(self._build_kpi_widgets())
        widgets.extend(self._build_table_widgets(dataset_map))
        widgets.extend(self._build_chart_widgets(dataset_map))
        return widgets

    def _build_kpi_widgets(self) -> List[Dict[str, Any]]:
        widgets: List[Dict[str, Any]] = []
        for idx, row in enumerate(self.kpi_rows):
            if not isinstance(row, dict):
                continue
            title = _normalize_text(
                row.get("metric_name") or row.get("title") or row.get("metric_code"),
                f"KPI {idx + 1}",
            )
            value = row.get("value")
            if value is None:
                value = row.get("metric_value")
            if value is None:
                value = row.get("total")
            widgets.append(
                {
                    "id": f"kpi_{idx + 1}",
                    "kind": "kpi",
                    "title": title,
                    "widget_type": "metric",
                    "dataset_id": row.get("dataset") or row.get("table_name"),
                    "bindings": {
                        "value_field": row.get("field") or row.get("metric_code") or "value",
                    },
                    "value": _coerce_number(value),
                    "unit": row.get("unit"),
                    "compiler_target": {
                        "candidate_native_object": "summary_indicator",
                    },
                    "raw": row,
                }
            )
        return widgets

    def _build_table_widgets(self, dataset_map: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        widgets: List[Dict[str, Any]] = []
        source_rows = self.table_widgets
        if not source_rows and dataset_map:
            first_dataset = next(iter(dataset_map.values()))
            source_rows = [
                {
                    "title": f"{first_dataset['name']} Table",
                    "dataset": first_dataset["id"],
                    "columns": [field["name"] for field in first_dataset.get("fields", [])],
                }
            ]

        for idx, table_widget in enumerate(source_rows):
            if not isinstance(table_widget, dict):
                continue
            dataset_id = (
                table_widget.get("dataset")
                or table_widget.get("table_name")
                or next(iter(dataset_map.keys()), None)
            )
            dataset = dataset_map.get(dataset_id or "")
            columns = (
                table_widget.get("columns")
                if isinstance(table_widget.get("columns"), list)
                else []
            )
            if not columns and dataset:
                columns = [field["name"] for field in dataset.get("fields", [])]
            widgets.append(
                {
                    "id": f"table_{idx + 1}",
                    "kind": "table",
                    "title": _normalize_text(
                        table_widget.get("title"),
                        dataset.get("name") if dataset else f"Table {idx + 1}",
                    ),
                    "widget_type": "table",
                    "dataset_id": dataset_id,
                    "bindings": {
                        "columns": columns,
                    },
                    "compiler_target": {
                        "candidate_native_object": "report_table",
                    },
                    "raw": table_widget,
                }
            )
        return widgets

    def _build_chart_widgets(self, dataset_map: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        widgets: List[Dict[str, Any]] = []
        for idx, chart in enumerate(self.charts):
            if not isinstance(chart, dict):
                continue
            dataset_id = (
                chart.get("dataset")
                or chart.get("table_name")
                or chart.get("datasource_name")
                or chart.get("datasource_name_text")
                or next(iter(dataset_map.keys()), None)
            )
            chart_type = _normalize_chart_type(
                chart.get("chart_type") or chart.get("viz_type") or chart.get("type")
            )
            title = _normalize_text(
                chart.get("slice_name") or chart.get("name") or chart.get("title"),
                f"Chart {idx + 1}",
            )
            bindings = {
                "category_field": chart.get("x")
                or chart.get("x_field")
                or chart.get("category")
                or chart.get("dimension"),
                "value_field": chart.get("y")
                or chart.get("y_field")
                or chart.get("metric")
                or chart.get("measure"),
                "series_field": chart.get("series_field")
                or chart.get("group")
                or chart.get("group_field"),
            }
            widgets.append(
                {
                    "id": f"chart_{idx + 1}",
                    "kind": "chart",
                    "title": title,
                    "widget_type": chart_type,
                    "dataset_id": dataset_id,
                    "bindings": bindings,
                    "series": chart.get("series"),
                    "compiler_target": {
                        "candidate_native_object": "chart",
                        "candidate_visualization": chart_type,
                    },
                    "raw": chart,
                }
            )

        return widgets

    def _build_layout(self, widgets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        if self.layout:
            for idx, entry in enumerate(self.layout):
                if not isinstance(entry, dict):
                    continue
                widget_ref = (
                    entry.get("widget_id")
                    or entry.get("id")
                    or entry.get("chart_id")
                    or entry.get("name")
                    or f"layout_{idx + 1}"
                )
                items.append(
                    {
                        "widget_ref": widget_ref,
                        "x": entry.get("x", entry.get("col", 0)),
                        "y": entry.get("y", entry.get("row", 0)),
                        "w": entry.get("w", entry.get("width", 6)),
                        "h": entry.get("h", entry.get("height", 4)),
                        "raw": entry,
                    }
                )
            return items

        # Default layout for compiler experiments.
        cursor_y = 0
        for widget in widgets:
            if widget["kind"] == "table":
                items.append(
                    {
                        "widget_ref": widget["id"],
                        "x": 0,
                        "y": cursor_y,
                        "w": 12,
                        "h": 6,
                    }
                )
                cursor_y += 6
                continue

            items.append(
                {
                    "widget_ref": widget["id"],
                    "x": 0 if len(items) % 2 == 0 else 6,
                    "y": cursor_y,
                    "w": 6,
                    "h": 4,
                }
            )
            if len(items) % 2 == 0:
                cursor_y += 4

        return items

    def _build_compiler_plan(
        self,
        datasets: List[Dict[str, Any]],
        widgets: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        object_graph: List[Dict[str, Any]] = []
        for dataset in datasets:
            object_graph.append(
                {
                    "kind": "dataset",
                    "source_id": dataset["id"],
                    "candidate_native_object": "tabular_dataset",
                    "field_count": len(dataset.get("fields", [])),
                    "row_count": dataset.get("row_count", 0),
                }
            )
        for widget in widgets:
            object_graph.append(
                {
                    "kind": widget["kind"],
                    "source_id": widget["id"],
                    "title": widget["title"],
                    "candidate_native_object": widget.get("compiler_target", {}).get(
                        "candidate_native_object"
                    ),
                    "dataset_id": widget.get("dataset_id"),
                }
            )

        object_graph.append(
            {
                "kind": "dashboard",
                "source_id": self.slug,
                "title": self.dashboard_title,
                "candidate_native_object": "dashboard_container",
                "widget_count": len(widgets),
            }
        )

        return {
            "status": "compiler_ready_spec",
            "target_native_format": ".pefx",
            "required_next_stage": [
                "Create native Foresight data objects for datasets",
                "Create native visual objects for table/chart widgets",
                "Create dashboard container and place widgets by layout",
                "Serialize native repository objects into .pefx",
            ],
            "object_graph": object_graph,
        }
