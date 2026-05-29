"""Data column helpers mixin for _TriplexExportBuilder."""
from __future__ import annotations

from .triplex_data_semantic import _DataSemanticMixin

from datetime import datetime
from typing import Any, Dict, List, Optional
import xml.etree.ElementTree as ET

from backend.builders.triplex_xml_parser import _coerce_number


class _DataHelpersMixin(_DataSemanticMixin):
    """Column-kind detection, SQL helpers, and semantic source building."""

    def _column_kind(self, values: List[Any]) -> str:
        if not values:
            return "string"
        numeric_hits = 0
        date_hits = 0
        non_empty = 0
        for val in values:
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                numeric_hits += 1
                non_empty += 1
                continue
            if isinstance(val, datetime):
                date_hits += 1
                non_empty += 1
                continue
            if isinstance(val, str):
                val_str = val.strip()
                if not val_str:
                    continue
                non_empty += 1
                if any(pattern.match(val_str) for pattern in self.DATE_PATTERNS):
                    date_hits += 1
                    continue
                if self._STRICT_NUMERIC_RE.match(val_str):
                    numeric_hits += 1
                    continue
        if non_empty == 0:
            return "string"
        if date_hits > 0 and date_hits >= numeric_hits:
            return "datetime"
        # Require majority of non-empty values to be numeric.
        if numeric_hits > 0 and numeric_hits >= non_empty * 0.5:
            return "number"
        return "string"

    def _resolve_columns(
        self,
        columns: List[str],
        x_field: Optional[str],
        y_field: Optional[str],
    ) -> List[str]:
        resolved = list(columns or [])
        if not resolved:
            resolved = [col for col in [x_field, y_field] if col] or ["value"]
        if x_field and x_field not in resolved:
            resolved.insert(0, x_field)
        if y_field and y_field not in resolved:
            resolved.append(y_field)
        return resolved

    @staticmethod
    def _is_empty_cell(value: Any) -> bool:
        return value is None or (isinstance(value, str) and not value.strip())

    def _drop_empty_columns(self, columns: List[str], rows: List[dict]) -> List[str]:
        if not columns or not rows:
            return columns
        kept = []
        for col in columns:
            has_value = any(
                isinstance(row, dict)
                and col in row
                and not self._is_empty_cell(row.get(col))
                for row in rows
            )
            if has_value:
                kept.append(col)
        return kept or columns

    @staticmethod
    def _move_first(columns: List[str], first_col: Optional[str]) -> List[str]:
        if not first_col or first_col not in columns:
            return columns
        return [first_col] + [col for col in columns if col != first_col]

    def _ordered_source_columns(self, columns: List[str]) -> List[str]:
        ordered: List[str] = []
        seen: set[str] = set()
        for column in columns or []:
            if column in seen:
                continue
            seen.add(column)
            ordered.append(column)
        category_col = next(
            (column for column in ordered if str(column).strip().lower() in self._CATEGORY_COL_NAMES),
            None,
        )
        return self._move_first(ordered, category_col)

    @staticmethod
    def _to_sql_literal(value: Any) -> str:
        """Convert a Python value to a SQL literal for use in VALUES clauses."""
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        if isinstance(value, datetime):
            return f"'{value.isoformat()}'"
        text = str(value)
        return "'" + text.replace("'", "''") + "'"

    @staticmethod
    def _sql_ident(name: str) -> str:
        """Quote a SQL identifier."""
        return '"' + str(name).replace('"', '""') + '"'

    def _column_pg_type(self, values: List[Any], column_name: Optional[str] = None) -> str:
        if column_name and str(column_name).strip().lower() in self._CATEGORY_COL_NAMES:
            return "character varying"
        col_kind = self._column_kind(values)
        if col_kind == "number":
            return "numeric"
        if col_kind == "datetime":
            return "timestamp without time zone"
        return "character varying"

    def _build_xheader(self, columns: List[str]) -> str:
        header_el = ET.Element("header")
        for idx, col in enumerate(columns):
            ET.SubElement(header_el, "column", attrib={
                "name": col,
                "alias": col,
                "order": str(idx),
                "isvisible": "true",
            })
        return ET.tostring(header_el, encoding="unicode")

    def _build_xstructure(self, columns: List[str], rows: List[dict]) -> str:
        ts_el = ET.Element("tablestructure")
        cols_el = ET.SubElement(ts_el, "columns")
        sample_rows = [r for r in rows if isinstance(r, dict)][:20]
        for col_idx, col_name in enumerate(columns, start=1):
            values = [r.get(col_name) for r in sample_rows if col_name in r]
            pg_type = self._column_pg_type(values, col_name)
            ET.SubElement(cols_el, "column", attrib={
                "id": str(col_idx),
                "name": col_name,
                "type": pg_type,
                "isnullable": "true",
            })
        ET.SubElement(ts_el, "indexes")
        ET.SubElement(ts_el, "constraints")
        return ET.tostring(ts_el, encoding="unicode")

    def _build_view_query(self, columns: List[str], rows: List[dict]) -> str:
        if not columns:
            return ""
        if rows:
            sample_rows = [r for r in rows if isinstance(r, dict)][:20]
            col_kinds: Dict[str, str] = {}
            for col in columns:
                col_vals = [r.get(col) for r in sample_rows if col in r]
                col_kinds[col] = self._column_kind(col_vals)

            limit = getattr(self, "inline_row_limit", None)
            nav_rows = rows[:limit] if isinstance(limit, int) and limit > 0 else rows
            values_chunks = []
            for row in nav_rows:
                if not isinstance(row, dict):
                    continue
                vals = []
                for col in columns:
                    raw = row.get(col)
                    force_text = str(col).strip().lower() in self._CATEGORY_COL_NAMES
                    col_lower = str(col).strip().lower()
                    will_cast_numeric = (
                        not force_text
                        and (
                            col_kinds.get(col) == "number"
                            or col_lower in self._VALUE_COL_NAMES
                            or col_lower.endswith("_value")
                        )
                    )
                    if will_cast_numeric and isinstance(raw, str):
                        num = _coerce_number(raw)
                        if num is not None:
                            vals.append(str(int(num)) if num == int(num) else str(num))
                        else:
                            vals.append("null")
                    else:
                        vals.append(self._to_sql_literal(raw))
                values_chunks.append(f"({', '.join(vals)})")
            if not values_chunks:
                values_chunks.append(f"({', '.join(['null'] * len(columns))})")
            cols_sql = ", ".join(self._sql_ident(col) for col in columns)
            # Wrap in a SELECT that casts numeric columns explicitly.  Without this,
            # PostgreSQL infers "integer" for whole-number literals and "numeric" for
            # decimals.  Navigator reads the pg column type to decide rendering, and
            # inconsistent inference between datasets can cause "Нет данных" for
            # tables whose numeric columns happen to contain fractional values.
            # Columns that end with "_value" are always numeric (generated by the
            # frontend buildExportPayload).  Promote them even when all rows are
            # null so that PostgreSQL infers numeric instead of text.
            for col in columns:
                col_lower = col.lower()
                if (
                    col_kinds.get(col) != "number"
                    and col_lower not in self._CATEGORY_COL_NAMES
                    and (col_lower.endswith("_value") or col_lower in self._VALUE_COL_NAMES)
                ):
                    col_kinds[col] = "number"
            num_cols = [col for col in columns if col_kinds.get(col) == "number"]
            if num_cols:
                cast_list = []
                for col in columns:
                    qname = self._sql_ident(col)
                    if col_kinds.get(col) == "number":
                        cast_list.append(f"{qname}::numeric AS {qname}")
                    else:
                        cast_list.append(qname)
                cast_sql = ", ".join(cast_list)
                return (
                    f"select {cast_sql} from "
                    f"(values {', '.join(values_chunks)}) as t({cols_sql})"
                )
            return f"select * from (values {', '.join(values_chunks)}) as t({cols_sql})"
        cols_sql = ", ".join(self._sql_ident(col) for col in columns)
        return f"select * from (values ({', '.join(['null'] * len(columns))})) as t({cols_sql})"

    def _emit_user_source(
        self,
        *,
        data: ET.Element,
        t11: ET.Element,
        t12: ET.Element,
        dv: ET.Element,
        sources_info: ET.Element,
        source_name: str,
        stable: str,
        columns: List[str],
        rows: List[dict],
        dt_last_load: str,
        view_query: str | None = None,
        view_level: int = 0,
    ) -> int:
        source_id = self._next_id()
        ordered_columns = self._ordered_source_columns(columns)
        view_query = view_query or self._build_view_query(ordered_columns, rows)
        xheader = self._build_xheader(ordered_columns)
        if view_query:
            viewquery_el = ET.Element("viewquery")
            viewquery_el.text = view_query
            xstructure = ET.tostring(viewquery_el, encoding="unicode")
        else:
            xstructure = self._build_xstructure(ordered_columns, rows)

        link_id = self._next_id()
        ET.SubElement(t11, "r", attrib={
            "nid": str(link_id),
            "nsubjectareaid": str(self.subject_area_id),
            "nusersourceid": str(source_id),
            "ismainsa": "true",
        })
        ET.SubElement(t12, "r", attrib={
            "nid": str(source_id),
            "sname": source_name,
            "stable": stable,
            "sdescription": source_name,
            "ntype": "3",
            "isshowforvisualization": "true",
            "dtlastload": dt_last_load,
            "xheader": xheader,
            "xstructure": xstructure,
        })
        ET.SubElement(dv, "r", attrib={
            "nid": str(source_id),
            "nlevel": str(view_level),
        })
        ET.SubElement(sources_info, "r", attrib={
            "nID": str(source_id),
            "sName": source_name,
        })

        set_us = ET.SubElement(data, "setusersource")
        set_params = ET.SubElement(set_us, "params")
        ET.SubElement(set_params, "param", attrib={
            "name": "nID",
            "value": str(source_id),
        })
        set_data = ET.SubElement(set_us, "data")
        ET.SubElement(set_data, "usersource", attrib={
            "sDescription": source_name,
            "sName": source_name,
            "sTable": stable,
            "sSchema": "src",
            "nType": "3",
            "isShowForVisualization": "true",
        })
        set_sa = ET.SubElement(set_data, "subjectareas")
        ET.SubElement(set_sa, "r", attrib={
            "nID": str(self.subject_area_id),
        })
        ET.SubElement(set_data, "viewquery").text = view_query
        set_ts = ET.SubElement(set_data, "tablestructure")
        set_cols_el = ET.SubElement(set_ts, "columns")
        for ci, cn in enumerate(ordered_columns, start=1):
            cvs = [r.get(cn) for r in rows if isinstance(r, dict) and cn in r]
            ET.SubElement(set_cols_el, "column", attrib={
                "name": cn,
                "type": self._column_pg_type(cvs, cn),
                "id": str(ci),
            })
        return source_id

    def _build_widget_navsql_query(
        self,
        *,
        stable: str,
        columns: List[str],
        filter_field: Optional[str],
        filter_value: Any = None,
        display_names: Optional[Dict[str, str]] = None,
    ) -> str:
        select_parts: List[str] = []
        for column in columns or []:
            source_column = (display_names or {}).get(column, column)
            source_sql = self._sql_ident(source_column)
            if source_column == column:
                select_parts.append(source_sql)
            else:
                select_parts.append(f"{source_sql} AS {self._sql_ident(column)}")
        if not select_parts:
            select_parts.append("*")
        query = f"SELECT {', '.join(select_parts)} FROM src.{self._sql_ident(stable)}"
        where_parts: List[str] = []
        if filter_field:
            source_filter = (display_names or {}).get(filter_field, filter_field)
            where_parts.append(f"{self._sql_ident(source_filter)} IS NOT NULL")
            if filter_value is not None and str(filter_value).strip() != "":
                where_parts.append(f"{self._sql_ident(source_filter)} = {self._to_sql_literal(filter_value)}")
        if where_parts:
            query += " WHERE " + " AND ".join(where_parts)
        return query

    @staticmethod
    def _generate_placeholder_rows(columns: List[str], count: int = 4) -> List[dict]:
        """Generate placeholder data rows for empty tables so that Navigator
        can display a chart instead of 'Нет данных для отображения'."""
        import random
        rng = random.Random(hash(tuple(columns)) & 0xFFFF_FFFF)
        placeholder_categories = [
            "Категория A", "Категория B", "Категория C", "Категория D",
            "Категория E", "Категория F",
        ]
        placeholder_periods = ["Янв", "Фев", "Март", "Апр", "Май", "Июнь"]
        rows: List[dict] = []
        for i in range(min(count, 6)):
            row: dict = {}
            for col in columns:
                col_lower = col.lower()
                if col_lower in ("value", "metric_value", "amount", "total",
                                 "sum", "y", "y_value"):
                    row[col] = rng.randint(10, 100)
                elif col_lower in ("category", "категория", "name", "название",
                                   "region", "регион", "series"):
                    row[col] = placeholder_categories[i % len(placeholder_categories)]
                elif col_lower in ("period", "период", "date", "дата",
                                   "month", "месяц"):
                    row[col] = placeholder_periods[i % len(placeholder_periods)]
                elif col_lower in ("metric_code",):
                    row[col] = f"metric_{i + 1}"
                elif col_lower in ("metric_name",):
                    row[col] = f"Метрика {i + 1}"
                elif col_lower in ("unit",):
                    row[col] = ""
                elif col_lower in ("note",):
                    row[col] = ""
                else:
                    row[col] = rng.randint(1, 50)
            rows.append(row)
        return rows

    def _infer_columns(self, dataset_name: str) -> Dict[str, Any]:
        entry = self.tables.get(dataset_name)
        if not entry:
            alt = self._normalize_dataset_key(dataset_name)
            entry = self.tables.get(alt)
        rows = entry.get("rows") if entry else []
        columns = entry.get("columns") if entry else []
        rows = rows or []
        columns = columns or []
        # Derive columns from row keys when not explicitly provided
        if not columns and rows:
            seen: dict = {}
            for row in rows:
                if isinstance(row, dict):
                    for k in row.keys():
                        seen[k] = None
            columns = list(seen.keys())
        # Generate placeholder data for empty tables so charts aren't blank
        if columns and not rows:
            rows = self._generate_placeholder_rows(columns)
        return {"rows": rows, "columns": columns}

    # Well-known column names that indicate the measurement / y-axis.
    _VALUE_COL_NAMES = {"value", "значение", "metric_value", "total", "итого", "sum"}
    # Well-known column names that indicate the dimension / x-axis.
    _CATEGORY_COL_NAMES = {"category", "категория", "period", "период", "name",
                           "название", "date", "дата", "month", "месяц"}
    _SEMANTIC_ALIASES = {
        "metric_code": {
            "metric_code", "metriccode", "кодметрики", "код_метрики",
            "indicator_code", "metric_id", "measure_code",
        },
        "metric_name": {
            "metric_name", "metricname", "названиеметрики", "метрика",
            "indicator_name", "name_metric", "measure_name",
        },
        "period": {
            "period", "date", "month", "year", "quarter", "week",
            "период", "дата", "месяц", "год", "квартал", "неделя",
        },
        "category": {
            "category", "name", "label", "segment", "bucket",
            "категория", "название", "лейбл", "сегмент", "группа",
        },
        "series": {
            "series", "group", "legend", "stack", "color_group",
            "серия", "группа", "легенда",
        },
        "organization": {
            "organization", "org", "company", "branch", "department",
            "организация", "компания", "филиал", "департамент", "подразделение",
        },
        "status": {
            "status", "state", "decision", "result", "stage",
            "статус", "состояние", "решение", "результат", "этап",
        },
        "unit": {
            "unit", "units", "measure_unit", "единица", "едизм",
        },
        "note": {
            "note", "notes", "comment", "description", "примечание", "комментарий", "описание",
        },
        "value": {
            "value", "metric_value", "measure", "amount", "total",
            "значение", "сумма", "итого",
        },
    }
