#!/usr/bin/env python3
"""Build a portable Visiology bundle for manual import.

The script generates a working CSV dataset, a dashboard JSON bundle bound to a
stable table/measure schema, and a model manifest describing the measures that
must be created before the dashboard JSON is uploaded.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.config import get_settings
from backend.services.visiology_client import VisiologyClient, VisiologyPublishConfig

DATASET_PATH = ROOT / "public/datasets/crm_requests_export.xlsx"
DEFAULT_OUT = Path("/tmp/visiology_manual_bundle")
DASHBOARD_TOPIC = "анализ CRM-заявок по стадии, типу и подразделению"
TABLE_NAME = "data_agent_dashboard"
DATASET_PLACEHOLDER = "__DATASET_ID__"
DATASET_NAME = "CRM Заявки — Анализ"


def log(message: str) -> None:
    print(f"[manual-bundle] {message}", flush=True)


def _manual_measure_name(column: str) -> str:
    return f"measure_{column}"


def _read_dataset(path: Path) -> tuple[list[str], list[dict[str, Any]]]:
    import math
    import pandas as pd

    dataframe = pd.read_excel(path) if path.suffix.lower() in {".xlsx", ".xls"} else pd.read_csv(path)
    for column in dataframe.columns:
        if dataframe[column].dtype.kind == "M":
            dataframe[column] = dataframe[column].dt.strftime("%Y-%m-%d").where(dataframe[column].notna(), None)

    columns = list(dataframe.columns)
    rows: list[dict[str, Any]] = []
    for _, row in dataframe.iterrows():
        rows.append(
            {
                key: None if value is None or (isinstance(value, float) and math.isnan(value)) else value
                for key, value in row.items()
            }
        )
    return columns, rows


def _build_crm_slim_table(columns: list[str], rows: list[dict[str, Any]]) -> tuple[list[str], list[dict[str, Any]]]:
    def find_column(*needles: str, exclude: tuple[str, ...] = ()) -> str | None:
        for column in columns:
            lowered = column.lower()
            if any(token in lowered for token in needles) and not any(token in lowered for token in exclude):
                return column
        return None

    stage_col = find_column("тади", "stage")
    request_type_col = find_column("тип", "type", exclude=("доступ", "access"))
    department_col = find_column("одраз", "department")
    access_type_col = find_column("доступ", "access")

    selected = [
        (stage_col, "Стадия"),
        (request_type_col, "Тип"),
        (department_col, "Подразделение"),
        (access_type_col, "Тип доступа"),
    ]
    missing = [label for source, label in selected if source is None]
    if missing:
        raise RuntimeError(f"Не удалось найти CRM-колонки для slim payload: {', '.join(missing)}")

    slim_rows: list[dict[str, Any]] = []
    for row in rows:
        slim_row = {label: str(row.get(source) or "Не указано") for source, label in selected if source}
        slim_row["count"] = 1
        slim_rows.append(slim_row)
    return [label for _, label in selected] + ["count"], slim_rows


def _build_payload(title: str, columns: list[str], rows: list[dict[str, Any]], charts: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [dict(row) for row in rows]
    columns = list(columns)
    stage_type_column = "Стадия / Тип"
    needs_stage_type = any(
        "стади" in str(chart.get("title") or chart.get("slice_name") or "").lower()
        and "тип" in str(chart.get("title") or chart.get("slice_name") or "").lower()
        for chart in charts
    )
    if needs_stage_type and stage_type_column not in columns:
        columns.append(stage_type_column)
        for row in rows:
            row[stage_type_column] = f"{row.get('Стадия') or ''} / {row.get('Тип') or ''}".strip(" /")

    table = {"columns": columns, "rows": rows}
    measure_name = "count"
    dimension_columns = ["Стадия", "Тип", "Подразделение", "Тип доступа", stage_type_column]

    type_map = {
        "bar": "bar",
        "column": "bar",
        "dist_bar": "bar",
        "bar_horizontal": "bar_horizontal",
        "hbar": "bar_horizontal",
        "line": "line",
        "area": "area",
        "pie": "pie",
        "donut": "donut",
        "big_number": "big_number",
        "kpi": "big_number",
        "table": "table",
        "pivot_table": "pivot_table",
    }

    def pick_dimension(chart_title: str, index: int) -> str:
        lowered = chart_title.lower()
        if "стади" in lowered and "тип" in lowered:
            return stage_type_column
        if "доступ" in lowered:
            return "Тип доступа"
        if "подраз" in lowered:
            return "Подразделение"
        if "тип" in lowered and "стади" not in lowered:
            return "Тип"
        if "стади" in lowered:
            return "Стадия"
        return dimension_columns[index % len(dimension_columns)]

    kpi_rows: list[dict[str, Any]] = []
    chart_items: list[dict[str, Any]] = []
    dimension_index = 0
    visual_index = 0
    for index, chart in enumerate(charts):
        raw_type = str(chart.get("chart_type") or chart.get("viz_type") or chart.get("actualType") or chart.get("type") or "bar")
        chart_type = type_map.get(raw_type, raw_type)
        title_chart = chart.get("slice_name") or chart.get("title") or f"Виджет {index + 1}"

        if chart_type in {"big_number", "kpi"}:
            kpi_rows.append(
                {
                    "title": title_chart,
                    "metric_name": measure_name,
                    "y_field": measure_name,
                    "value": chart.get("value"),
                    "color": chart.get("color"),
                    "position": {
                        "left": len(kpi_rows) * 0.25,
                        "top": 0.0,
                        "width": 0.25,
                        "height": 0.15,
                    },
                }
            )
            continue

        chart_items.append(
            {
                "chart_type": chart_type,
                "slice_name": title_chart,
                "x_field": pick_dimension(title_chart, dimension_index),
                "y_field": measure_name,
                "position": chart.get("position") or {
                    "left": (visual_index % 2) * 0.5,
                    "top": 0.19 + (visual_index // 2) * 0.26,
                    "width": 0.5,
                    "height": 0.23,
                },
                "series_colors": chart.get("series_colors") or None,
            }
        )
        dimension_index += 1
        visual_index += 1

    return {
        "dashboard_title": title,
        "tables": [table],
        "charts": chart_items,
        "kpi_rows": kpi_rows,
    }


def _create_session_and_dashboard(backend: str) -> dict[str, Any]:
    client = httpx.Client(base_url=backend, timeout=180)
    try:
        session = client.post("/api/sessions")
        session.raise_for_status()
        session_id = session.json()["session_id"]

        with DATASET_PATH.open("rb") as handle:
            upload = client.post(
                f"/api/sessions/{session_id}/upload",
                files={
                    "file": (
                        DATASET_PATH.name,
                        handle,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                },
                timeout=60,
            )
        upload.raise_for_status()

        dashboard = client.post(
            f"/api/sessions/{session_id}/dashboard",
            json={"topic": DASHBOARD_TOPIC},
            timeout=120,
        )
        dashboard.raise_for_status()

        return {
            "session_id": session_id,
            "upload": upload.json(),
            "dashboard": dashboard.json(),
        }
    finally:
        client.close()


def build_bundle(backend: str, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    log("Генерирую дашборд в агенте данных...")
    generated = _create_session_and_dashboard(backend)
    dashboard = generated["dashboard"]

    raw_columns, raw_rows = _read_dataset(DATASET_PATH)
    slim_columns, slim_rows = _build_crm_slim_table(raw_columns, raw_rows)
    payload = _build_payload(DATASET_NAME, slim_columns, slim_rows, dashboard.get("charts") or [])

    settings = get_settings()
    client = VisiologyClient(VisiologyPublishConfig.from_settings(settings))
    try:
        client._ensure_token()
        csv_bytes, typed_columns, row_count = client._build_csv(payload["tables"][0])
        numeric_columns = [column["name"] for column in typed_columns if column["type"] in {"Int64", "Float64"}]
        chart_fields = client._chart_metric_fields(payload)
        measure_columns = [name for name in numeric_columns if not chart_fields or name in chart_fields] or numeric_columns[:1]
        measures = [
            {
                "id": f"__MEASURE_{index + 1}__",
                "name": _manual_measure_name(column),
                "column": column,
                "expression": f"SUM('{TABLE_NAME}'[{column}])",
            }
            for index, column in enumerate(measure_columns)
        ]

        bundle = client._build_dashboard_bundle(
            payload=payload,
            dashboard_name=DATASET_NAME,
            dataset_id=DATASET_PLACEHOLDER,
            table_name=TABLE_NAME,
            columns=typed_columns,
            measures=measures,
        )
    finally:
        client.close()

    csv_path = out_dir / f"{TABLE_NAME}.csv"
    csv_path.write_bytes(csv_bytes)
    (out_dir / "dashboard_spec.json").write_text(json.dumps(dashboard, ensure_ascii=False, indent=2))
    (out_dir / "visiology_payload.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    (out_dir / "visiology_dashboard_bundle.json").write_text(json.dumps(bundle, ensure_ascii=False, indent=2))
    (out_dir / "visiology_model_manifest.json").write_text(
        json.dumps(
            {
                "dataset_placeholder": DATASET_PLACEHOLDER,
                "dataset_name": DATASET_NAME,
                "table_name": TABLE_NAME,
                "row_count": row_count,
                "columns": typed_columns,
                "measure_columns": measure_columns,
                "measures": measures,
                "manual_steps": [
                    "1. Создать новый dataset/model в Visiology.",
                    f"2. Загрузить CSV {TABLE_NAME}.csv в таблицу {TABLE_NAME}.",
                    "3. Создать меры из раздела measures c теми же именами и выражениями.",
                    f"4. Перед upload dashboard JSON заменить {DATASET_PLACEHOLDER} на реальный datasetId.",
                    "5. Загрузить visiology_dashboard_bundle.json как dashboard JSON.",
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    log(f"Готово: {out_dir}")
    log(f"  CSV: {csv_path}")
    log(f"  Dashboard JSON: {out_dir / 'visiology_dashboard_bundle.json'}")
    log(f"  Model manifest: {out_dir / 'visiology_model_manifest.json'}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a manual Visiology import bundle from the CRM dashboard flow.")
    parser.add_argument("--backend", default="http://127.0.0.1:8000", help="Data Agent backend URL")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="Output directory for bundle files")
    args = parser.parse_args()

    build_bundle(args.backend, Path(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
