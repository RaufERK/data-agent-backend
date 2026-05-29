#!/usr/bin/env python3
"""Build a dashboard in Data Agent and export it as a Foresight compiler bundle."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import httpx


DASHBOARD_MAX_COLUMNS = 12
DASHBOARD_ROW_HEIGHT = 112
RAW_DASHBOARD_TABLE = "FactDashboardRaw"
DEFAULT_COLOR = "#3B82F6"


def _api_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _post_json(client: httpx.Client, base_url: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = client.post(_api_url(base_url, path), json=payload)
    response.raise_for_status()
    return response.json()


def _create_session(client: httpx.Client, base_url: str) -> str:
    response = client.post(_api_url(base_url, "/api/sessions"))
    response.raise_for_status()
    session_id = response.json().get("session_id")
    if not session_id:
        raise RuntimeError("Backend did not return session_id")
    return str(session_id)


def _upload_file(client: httpx.Client, base_url: str, session_id: str, file_path: Path, table_name: str | None) -> None:
    data = {}
    if table_name:
        data["table_name"] = table_name
    with file_path.open("rb") as file_handle:
        response = client.post(
            _api_url(base_url, f"/api/sessions/{session_id}/upload"),
            data=data,
            files={"file": (file_path.name, file_handle)},
        )
    response.raise_for_status()


def _normalize_chart_type(chart_type: str) -> str:
    normalized = (chart_type or "").strip().lower()
    if normalized in {"big_number", "number", "metric"}:
        return "kpi"
    if normalized in {"bar_horizontal", "horizontal_bar"}:
        return "hbar"
    return normalized or "bar"


def _export_chart_type(chart_type: str) -> str:
    normalized = _normalize_chart_type(chart_type)
    if normalized == "kpi":
        return "big_number"
    if normalized == "hbar":
        return "bar_horizontal"
    return normalized


def _default_layout(chart: dict[str, Any]) -> dict[str, int]:
    chart_type = _normalize_chart_type(str(chart.get("type") or ""))
    if chart_type == "kpi":
        return {"colSpan": 3, "rowSpan": 2}
    if chart_type == "filter":
        return {"colSpan": 4, "rowSpan": 2}
    if chart_type == "table":
        return {"colSpan": 6, "rowSpan": 3}
    if chart_type in {"pie", "donut"}:
        return {"colSpan": 4, "rowSpan": 3}
    return {"colSpan": 6, "rowSpan": 3}


def _with_layout(chart: dict[str, Any], index: int) -> dict[str, Any]:
    defaults = _default_layout(chart)
    col_span = int(chart.get("colSpan") or defaults["colSpan"])
    row_span = int(chart.get("rowSpan") or defaults["rowSpan"])
    return {
        "id": chart.get("id") or index,
        **chart,
        "type": _normalize_chart_type(str(chart.get("type") or "")),
        "colSpan": min(max(col_span, 3), DASHBOARD_MAX_COLUMNS),
        "rowSpan": min(max(row_span, 2), 6),
    }


def _field_name(value: str, fallback: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_]+", "_", str(value or "").strip()).strip("_").lower()
    safe_fallback = re.sub(r"[^a-zA-Z0-9_]+", "_", str(fallback or "").strip()).strip("_").lower()
    return normalized or safe_fallback or "field"


def _export_number(value: Any) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    normalized = str(value).strip().replace(" ", "").replace(",", ".")
    if normalized in {"", "-", "—", "N/A"}:
        return None
    try:
        parsed = float(normalized)
    except ValueError:
        return None
    return int(parsed) if parsed.is_integer() else parsed


def _primary_color(chart: dict[str, Any]) -> str:
    if chart.get("color"):
        return str(chart["color"])
    series = chart.get("series") or []
    if series and series[0].get("color"):
        return str(series[0]["color"])
    slices = chart.get("slices") or []
    if slices and slices[0].get("color"):
        return str(slices[0]["color"])
    return DEFAULT_COLOR


def build_export_payload(charts: list[dict[str, Any]], dashboard_title: str) -> dict[str, Any]:
    layout: list[dict[str, Any]] = []
    export_charts: list[dict[str, Any]] = []
    kpi_rows: list[dict[str, Any]] = []
    chart_meta: dict[str, Any] = {}
    raw_columns = {"category"}
    raw_rows_by_category: dict[str, dict[str, Any]] = {}

    def ensure_raw_row(category: str) -> dict[str, Any]:
        key = category or "Итого"
        raw_rows_by_category.setdefault(key, {"category": key})
        return raw_rows_by_category[key]

    def set_raw_value(category: str, field: str, value: Any) -> None:
        raw_columns.add(field)
        ensure_raw_row(category)[field] = value

    row = 0
    col = 0
    for index, source_chart in enumerate(charts, start=1):
        chart = _with_layout(source_chart, index)
        width = int(chart["colSpan"])
        height = int(chart["rowSpan"]) * DASHBOARD_ROW_HEIGHT
        if col + width > DASHBOARD_MAX_COLUMNS:
            col = 0
            row += 1

        chart_id = str(chart["id"])
        chart_type = _export_chart_type(str(chart.get("type") or ""))
        slice_name = str(chart.get("title") or f"Виджет {chart_id}")
        base_field = _field_name(slice_name, f"widget_{chart_id}")
        x_field = "category"
        y_field = f"{base_field}_value"
        metric_fields: list[str] = []
        export_series: list[dict[str, Any]] = []

        layout.append(
            {
                "id": chart_id,
                "slice_name": slice_name,
                "dataset": RAW_DASHBOARD_TABLE,
                "row": row,
                "col": col,
                "width": width,
                "height": height,
            }
        )

        if chart_type == "big_number":
            raw_columns.add(y_field)
            metric_fields.append(y_field)
            raw_value = _export_number(chart.get("value"))
            ensure_raw_row("Итого")[y_field] = raw_value
            export_charts.append(
                {
                    "id": chart_id,
                    "slice_name": slice_name,
                    "name": slice_name,
                    "title": slice_name,
                    "dataset": RAW_DASHBOARD_TABLE,
                    "table_name": RAW_DASHBOARD_TABLE,
                    "chart_type": chart_type,
                    "viz_type": chart_type,
                    "x_field": x_field,
                    "y_field": y_field,
                    "metric_fields": metric_fields,
                }
            )
            kpi_rows.append(
                {
                    "title": slice_name,
                    "metric_name": slice_name,
                    "value": raw_value,
                    "unit": "",
                    "dataset": RAW_DASHBOARD_TABLE,
                    "table_name": RAW_DASHBOARD_TABLE,
                    "x_field": x_field,
                    "y_field": y_field,
                    "metric_fields": metric_fields,
                }
            )
        else:
            slices = chart.get("slices") or []
            series = chart.get("series") or []
            table = chart.get("table") or {}
            if chart_type in {"pie", "donut"} and slices:
                for item in slices:
                    set_raw_value(str(item.get("label") or ""), y_field, item.get("value"))
                metric_fields.append(y_field)
                export_series.append(
                    {
                        "name": slice_name,
                        "data": [item.get("value") for item in slices],
                        "color": _primary_color(chart),
                    }
                )
            elif chart.get("categories") and series:
                categories = [str(item) for item in chart.get("categories") or []]
                for series_index, item in enumerate(series, start=1):
                    series_name = str(item.get("name") or f"Ряд {series_index}")
                    series_field = y_field if len(series) == 1 else f"{base_field}_{_field_name(series_name, f'series_{series_index}')}"
                    raw_columns.add(series_field)
                    metric_fields.append(series_field)
                    values = item.get("values") or []
                    for category_index, value in enumerate(values):
                        category = categories[category_index] if category_index < len(categories) else f"Строка {category_index + 1}"
                        set_raw_value(category, series_field, value)
                    if series_index == 1:
                        y_field = series_field
                    export_series.append({"name": series_name, "data": values, "color": item.get("color")})
            elif table:
                columns = [str(item) for item in table.get("columns") or []]
                table_fields = [_field_name(column, f"table_{chart_id}_{i + 1}") for i, column in enumerate(columns)]
                for field in table_fields:
                    raw_columns.add(field)
                for row_index, table_row in enumerate(table.get("rows") or []):
                    category = str(table_row[0] if table_row else f"Строка {row_index + 1}")
                    raw_row = ensure_raw_row(category)
                    for column_index, field in enumerate(table_fields):
                        raw_row[field] = table_row[column_index] if column_index < len(table_row) else ""
                y_field = ""
            else:
                set_raw_value(slice_name, y_field, _export_number(chart.get("value")))
                metric_fields.append(y_field)

            export_charts.append(
                {
                    "id": chart_id,
                    "slice_name": slice_name,
                    "name": slice_name,
                    "title": slice_name,
                    "dataset": RAW_DASHBOARD_TABLE,
                    "table_name": RAW_DASHBOARD_TABLE,
                    "chart_type": chart_type,
                    "viz_type": chart_type,
                    "categories": chart.get("categories"),
                    "series": export_series,
                    "x_field": x_field,
                    "y_field": y_field,
                    "metric_fields": metric_fields,
                }
            )

        chart_meta[slice_name] = {
            "chart_type": chart_type,
            "x_field": x_field,
            "y_field": y_field,
            "metric_fields": metric_fields,
            "color": _primary_color(chart),
        }
        col += width

    columns = sorted(raw_columns, key=lambda item: (item != "category", item))
    raw_rows = [{column: raw_row.get(column) for column in columns} for raw_row in raw_rows_by_category.values()]

    return {
        "dashboard_title": dashboard_title,
        "subject_area_name": dashboard_title,
        "title": dashboard_title,
        "slug": "data_agent_dashboard",
        "charts": export_charts,
        "layout": layout,
        "tables": [{"table_name": RAW_DASHBOARD_TABLE, "columns": columns, "rows": raw_rows}],
        "kpi_rows": kpi_rows,
        "chart_meta": chart_meta,
        "inline_data": True,
        "navigator_single_raw_source": True,
    }


def parse_upload(value: str) -> tuple[Path, str | None]:
    if "=" not in value:
        return Path(value).expanduser().resolve(), None
    table_name, file_path = value.split("=", 1)
    return Path(file_path).expanduser().resolve(), table_name.strip() or None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a Data Agent dashboard and export it as *_foresight_bundle.json."
    )
    parser.add_argument("files", nargs="+", help="Files to upload. Use table_name=/path/file.xlsx to set table name.")
    parser.add_argument("--topic", required=True, help="Dashboard topic/request.")
    parser.add_argument("--title", help="Dashboard title. Defaults to --topic.")
    parser.add_argument("--backend", default="http://127.0.0.1:8000", help="Data Agent backend URL.")
    parser.add_argument("--output", default="foresight_bundle.json", help="Output JSON path.")
    args = parser.parse_args()

    uploads = [parse_upload(item) for item in args.files]
    missing = [str(path) for path, _ in uploads if not path.exists()]
    if missing:
        raise FileNotFoundError("Files not found: " + ", ".join(missing))

    with httpx.Client(timeout=120.0) as client:
        health = client.get(_api_url(args.backend, "/api/health"))
        health.raise_for_status()

        session_id = _create_session(client, args.backend)
        print(f"session_id={session_id}")

        for file_path, table_name in uploads:
            _upload_file(client, args.backend, session_id, file_path, table_name)
            print(f"uploaded={file_path.name} table={table_name or file_path.stem}")

        dashboard = _post_json(client, args.backend, f"/api/sessions/{session_id}/dashboard", {"topic": args.topic})
        charts = dashboard.get("charts") or []
        if not charts:
            raise RuntimeError("Dashboard endpoint returned no charts")
        print(f"charts={len(charts)}")

        payload = build_export_payload(charts, args.title or args.topic)
        response = client.post(_api_url(args.backend, "/api/export/dashboard/foresight"), json=payload)
        response.raise_for_status()

    output_path = Path(args.output).expanduser().resolve()
    output_path.write_bytes(response.content)

    try:
        exported = json.loads(response.content)
        print(f"format={exported.get('format')}")
        print(f"artifact={exported.get('artifact_id') or exported.get('dashboard_id') or output_path.name}")
    except json.JSONDecodeError:
        pass
    print(f"saved={output_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:1000]
        print(f"HTTP error {exc.response.status_code}: {detail}", file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
