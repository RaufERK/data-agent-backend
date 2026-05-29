"""Semantic source helpers for Triplex export builder."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from backend.builders.triplex_xml_parser import _coerce_number, _slugify_js


class _DataSemanticMixin:
    @staticmethod
    def _normalize_semantic_key(value: Any) -> str:
        text = str(value or "").strip().lower()
        text = re.sub(r"[\s\-]+", "_", text)
        text = re.sub(r"[^a-z0-9а-яё_]+", "", text)
        return text.strip("_")

    def _match_semantic_column(self, columns: List[str], semantic_key: str) -> Optional[str]:
        aliases = self._SEMANTIC_ALIASES.get(semantic_key, set())
        if not columns:
            return None
        normalized_map = {
            col: self._normalize_semantic_key(col)
            for col in columns
            if col
        }
        for col, normalized in normalized_map.items():
            if normalized == semantic_key or normalized in aliases:
                return col
        for col, normalized in normalized_map.items():
            if any(normalized.endswith(alias) or alias.endswith(normalized) for alias in aliases):
                return col
        return None

    def _first_non_empty_value(self, row: Dict[str, Any], columns: List[Optional[str]]) -> Any:
        for col in columns:
            if not col or not isinstance(row, dict) or col not in row:
                continue
            value = row.get(col)
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            return value
        return None

    def _guess_semantic_dimensions(
        self,
        dataset_name: str,
        columns: List[str],
        rows: List[dict],
        meta: Dict[str, Any],
    ) -> Dict[str, Optional[str]]:
        chart_type = str(meta.get("chart_type") or meta.get("viz_type") or "table")
        axes = self._infer_axes(chart_type, columns, rows, meta)
        x_field = axes.get("x")
        y_field = axes.get("y")
        group_field = meta.get("group_field") or meta.get("series_field")

        period_field = self._match_semantic_column(columns, "period")
        category_field = self._match_semantic_column(columns, "category")
        series_field = self._match_semantic_column(columns, "series")
        organization_field = self._match_semantic_column(columns, "organization")
        status_field = self._match_semantic_column(columns, "status")
        metric_code_field = self._match_semantic_column(columns, "metric_code")
        metric_name_field = self._match_semantic_column(columns, "metric_name")
        unit_field = self._match_semantic_column(columns, "unit")
        note_field = self._match_semantic_column(columns, "note")

        if chart_type in {"line", "area", "combo"} and category_field and not period_field:
            period_field = category_field
            category_field = None

        if x_field and not period_field:
            x_kind = self._column_kind([row.get(x_field) for row in rows[:20] if isinstance(row, dict) and x_field in row])
            if chart_type in {"line", "area", "combo"} or x_kind == "datetime" or self._match_semantic_column([x_field], "period"):
                period_field = x_field
            elif not category_field and x_field != y_field:
                category_field = x_field
        if group_field and not series_field and group_field in columns:
            series_field = group_field

        return {
            "value_field": y_field or self._match_semantic_column(columns, "value"),
            "period_field": period_field,
            "category_field": category_field,
            "series_field": series_field,
            "organization_field": organization_field,
            "status_field": status_field,
            "metric_code_field": metric_code_field,
            "metric_name_field": metric_name_field,
            "unit_field": unit_field,
            "note_field": note_field,
        }

    def _build_semantic_sources(self) -> List[Dict[str, Any]]:
        observation_rows: List[Dict[str, Any]] = []
        observation_id = 1
        seen_kpi_consolidated = False

        for dataset_name, entry in self.tables.items():
            if not isinstance(entry, dict):
                continue
            if dataset_name == "FactDashboardSummary":
                continue
            if dataset_name.startswith("FactDashboardMetric_"):
                # Prefer the consolidated KPI source to N single-row tables.
                continue
            if dataset_name == "FactDashboardMetric":
                seen_kpi_consolidated = True
            rows = entry.get("rows") if isinstance(entry.get("rows"), list) else []
            columns = entry.get("columns") if isinstance(entry.get("columns"), list) else []
            if not columns and rows:
                first = rows[0] if isinstance(rows[0], dict) else {}
                columns = list(first.keys()) if isinstance(first, dict) else []
            meta = self.chart_meta.get(dataset_name) if isinstance(self.chart_meta.get(dataset_name), dict) else {}
            source_title = str(meta.get("title") or dataset_name).strip() or dataset_name

            if dataset_name == "FactDashboardMetric":
                for row_idx, row in enumerate(rows):
                    if not isinstance(row, dict):
                        continue
                    metric_name = str(row.get("metric_name") or row.get("metric_code") or source_title).strip() or source_title
                    metric_code = str(row.get("metric_code") or _slugify_js(metric_name) or f"metric_{row_idx + 1}")
                    value = row.get("value")
                    if value is None:
                        value = row.get("metric_value")
                    if value is None:
                        continue
                    observation_rows.append({
                        "observation_id": observation_id,
                        "source_table": dataset_name,
                        "source_title": source_title,
                        "metric_code": metric_code,
                        "metric_name": metric_name,
                        "period": row.get("period"),
                        "category": None,
                        "series": None,
                        "organization": None,
                        "status": None,
                        "value": value,
                        "unit": row.get("unit"),
                        "note": row.get("note"),
                    })
                    observation_id += 1
                continue

            semantic_fields = self._guess_semantic_dimensions(dataset_name, columns, rows, meta)
            value_field = semantic_fields.get("value_field")
            if not value_field:
                numeric_candidates = []
                for col in columns:
                    values = [row.get(col) for row in rows[:20] if isinstance(row, dict) and col in row]
                    if self._column_kind(values) == "number":
                        numeric_candidates.append(col)
                value_field = numeric_candidates[0] if numeric_candidates else None
            if not value_field:
                continue

            base_metric_name = source_title
            base_metric_code = _slugify_js(
                meta.get("metric_code")
                or meta.get("title")
                or dataset_name
            ) or _slugify_js(dataset_name)

            for row in rows:
                if not isinstance(row, dict):
                    continue
                raw_value = row.get(value_field)
                numeric_value = _coerce_number(raw_value)
                if numeric_value is None and raw_value is None:
                    continue
                metric_code = self._first_non_empty_value(row, [semantic_fields.get("metric_code_field")])
                metric_name = self._first_non_empty_value(row, [semantic_fields.get("metric_name_field")])
                observation_rows.append({
                    "observation_id": observation_id,
                    "source_table": dataset_name,
                    "source_title": source_title,
                    "metric_code": str(metric_code or base_metric_code),
                    "metric_name": str(metric_name or base_metric_name),
                    "period": self._first_non_empty_value(row, [semantic_fields.get("period_field")]),
                    "category": self._first_non_empty_value(row, [semantic_fields.get("category_field")]),
                    "series": self._first_non_empty_value(row, [semantic_fields.get("series_field")]),
                    "organization": self._first_non_empty_value(row, [semantic_fields.get("organization_field")]),
                    "status": self._first_non_empty_value(row, [semantic_fields.get("status_field")]),
                    "value": numeric_value if numeric_value is not None else raw_value,
                    "unit": self._first_non_empty_value(row, [semantic_fields.get("unit_field")]),
                    "note": self._first_non_empty_value(row, [semantic_fields.get("note_field")]),
                })
                observation_id += 1

        if not observation_rows and self.kpi_rows and not seen_kpi_consolidated:
            for row_idx, row in enumerate(self.kpi_rows):
                if not isinstance(row, dict):
                    continue
                metric_name = str(row.get("metric_name") or row.get("metric_code") or f"metric_{row_idx + 1}").strip()
                metric_code = str(row.get("metric_code") or _slugify_js(metric_name) or f"metric_{row_idx + 1}")
                observation_rows.append({
                    "observation_id": observation_id,
                    "source_table": "FactDashboardMetric",
                    "source_title": "FactDashboardMetric",
                    "metric_code": metric_code,
                    "metric_name": metric_name,
                    "period": row.get("period"),
                    "category": None,
                    "series": None,
                    "organization": None,
                    "status": None,
                    "value": row.get("value"),
                    "unit": row.get("unit"),
                    "note": row.get("note"),
                })
                observation_id += 1

        def unique_rows(key: str, extra_columns: List[str]) -> List[Dict[str, Any]]:
            seen: set[str] = set()
            result: List[Dict[str, Any]] = []
            for row in observation_rows:
                value = row.get(key)
                if value is None or (isinstance(value, str) and not value.strip()):
                    continue
                normalized = str(value).strip()
                if normalized in seen:
                    continue
                seen.add(normalized)
                entry = {key: value}
                for col in extra_columns:
                    if row.get(col) is not None:
                        entry[col] = row.get(col)
                result.append(entry)
            return result

        semantic_sources: List[Dict[str, Any]] = []
        if observation_rows:
            semantic_sources.append({
                "logical_name": "FactObservation",
                "columns": [
                    "observation_id", "source_table", "source_title",
                    "metric_code", "metric_name", "period", "category",
                    "series", "organization", "status", "value", "unit", "note",
                ],
                "rows": observation_rows,
                "_semantic_role": "fact",
            })

        dim_metric_rows = unique_rows("metric_code", ["metric_name", "unit"])
        if dim_metric_rows:
            semantic_sources.append({
                "logical_name": "DimMetric",
                "columns": ["metric_code", "metric_name", "unit"],
                "rows": dim_metric_rows,
                "_semantic_role": "dimension",
                "_join_to": [{"target_logical_name": "FactObservation", "source_column": "metric_code", "target_column": "metric_code"}],
            })

        for logical_name, column_name in (
            ("DimPeriod", "period"),
            ("DimCategory", "category"),
            ("DimSeries", "series"),
            ("DimOrganization", "organization"),
            ("DimStatus", "status"),
        ):
            dim_rows = unique_rows(column_name, [])
            if not dim_rows:
                continue
            semantic_sources.append({
                "logical_name": logical_name,
                "columns": [column_name],
                "rows": dim_rows,
                "_semantic_role": "dimension",
                "_join_to": [{"target_logical_name": "FactObservation", "source_column": column_name, "target_column": column_name}],
            })

        dim_source_rows = unique_rows("source_table", ["source_title"])
        if dim_source_rows:
            semantic_sources.append({
                "logical_name": "DimSource",
                "columns": ["source_table", "source_title"],
                "rows": dim_source_rows,
                "_semantic_role": "dimension",
                "_join_to": [{"target_logical_name": "FactObservation", "source_column": "source_table", "target_column": "source_table"}],
            })

        return semantic_sources
