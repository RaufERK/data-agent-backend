"""Publish generated Data Agent dashboards to Visiology."""
from __future__ import annotations

import copy
import csv
import io
import json
import logging
import math
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

from backend.config import Settings

logger = logging.getLogger("data_agent.visiology")


def _guid() -> str:
    return uuid.uuid4().hex


def _uuid() -> str:
    return str(uuid.uuid4())


def _slug(value: Any, fallback: str = "data_agent") -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9а-яё_-]+", "_", text, flags=re.IGNORECASE)
    text = re.sub(r"_+", "_", text).strip("_")
    return text or fallback


_TRANSLIT_MAP: dict[int, str] = {
    ord(src): dst for src, dst in zip(
        "абвгдеёжзийклмнопрстуфхцчшщъыьэюяАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ",
        ["a","b","v","g","d","e","yo","zh","z","i","y","k","l","m","n","o","p","r","s","t","u","f","kh","ts","ch","sh","sch","","y","","e","yu","ya",
         "A","B","V","G","D","E","YO","ZH","Z","I","Y","K","L","M","N","O","P","R","S","T","U","F","KH","TS","CH","SH","SCH","","Y","","E","YU","YA"],
    )
}


def _translit(text: str) -> str:
    return "".join(_TRANSLIT_MAP.get(ord(ch), ch) for ch in text)


def _safe_column(value: Any, fallback: str) -> str:
    text = _translit(str(value or "").strip())
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"[^\w_]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    if not text:
        text = fallback
    if text[0].isdigit():
        text = f"c_{text}"
    return text[:80]


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or (isinstance(value, str) and value.strip() == "")


def _number(value: Any) -> float | None:
    if _is_empty(value):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    text = str(value).strip().replace("\u00a0", "").replace(" ", "").replace(",", ".")
    text = re.sub(r"[^\d.+-]", "", text)
    if text in {"", "-", "+", ".", "+.", "-."}:
        return None
    try:
        parsed = float(text)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def _looks_like_date(value: Any) -> bool:
    if _is_empty(value):
        return False
    text = str(value).strip()
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}:\d{2})?$", text))


def _infer_visiology_type(values: list[Any]) -> str:
    sample = [value for value in values if not _is_empty(value)]
    if not sample:
        return "String"
    if all(_looks_like_date(value) for value in sample):
        return "Date"
    numbers = [_number(value) for value in sample]
    if all(value is not None for value in numbers):
        if all(float(value).is_integer() for value in numbers if value is not None):
            return "Int64"
        return "Float64"
    return "String"


def _chart_type(chart: dict[str, Any]) -> str:
    raw_type = str(
        chart.get("chart_type")
        or chart.get("viz_type")
        or chart.get("actualType")
        or chart.get("type")
        or ""
    ).strip().lower()
    type_map = {
        "kpi": "big_number",
        "metric": "big_number",
        "number": "big_number",
        "column": "bar",
        "dist_bar": "bar",
        "hbar": "bar_horizontal",
    }
    return type_map.get(raw_type, raw_type)


@dataclass(frozen=True)
class VisiologyPublishConfig:
    public_base_url: str
    api_base_url: str
    host_header: str
    username: str
    password: str
    client_id: str
    workspace_id: str
    theme_guid: str
    template_workspace_id: str
    template_dashboard_id: str
    verify_ssl: bool
    timeout: int

    @classmethod
    def from_settings(cls, settings: Settings) -> "VisiologyPublishConfig":
        return cls(
            public_base_url=settings.visiology_public_base_url.rstrip("/"),
            api_base_url=settings.visiology_api_base_url.rstrip("/"),
            host_header=settings.visiology_host_header,
            username=settings.visiology_username,
            password=settings.visiology_password,
            client_id=settings.visiology_client_id,
            workspace_id=settings.visiology_workspace_id,
            theme_guid=settings.visiology_theme_guid,
            template_workspace_id=settings.visiology_template_workspace_id,
            template_dashboard_id=settings.visiology_template_dashboard_id,
            verify_ssl=settings.visiology_verify_ssl,
            timeout=settings.visiology_timeout,
        )


class VisiologyClient:
    def __init__(self, config: VisiologyPublishConfig):
        self.config = config
        self.client = httpx.Client(
            base_url=config.api_base_url,
            verify=config.verify_ssl,
            timeout=httpx.Timeout(config.timeout),
            follow_redirects=True,
            headers={"Host": config.host_header},
        )
        self._token: str | None = None

    def close(self) -> None:
        self.client.close()

    def publish_dashboard(self, payload: dict[str, Any]) -> dict[str, Any]:
        title = str(payload.get("dashboard_title") or payload.get("title") or "Data Agent Dashboard").strip()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dataset_name = f"{title} {stamp}"
        dataset_id = str(uuid.uuid4())
        table_name = "data_agent_dashboard"

        table = self._extract_table(payload)
        csv_bytes, columns, row_count = self._build_csv(table)
        numeric_columns = [col["name"] for col in columns if col["type"] in {"Int64", "Float64"}]
        chart_fields = self._chart_metric_fields(payload)
        measure_columns = [name for name in numeric_columns if not chart_fields or name in chart_fields]
        if not measure_columns:
            measure_columns = numeric_columns[:1]

        self._ensure_token()
        self._create_model(dataset_id, dataset_name)
        table_id = self._upload_csv_dataset(dataset_id, table_name, csv_bytes, columns)
        self._wait_dms_operations(dataset_id)
        measures = self._create_measures(dataset_id, table_id, table_name, measure_columns)
        validation = self._validate_dataset(dataset_id, table_name, columns, measures)

        dashboard_guid = self._upload_dashboard(
            payload=payload,
            dashboard_name=dataset_name,
            dataset_id=dataset_id,
            table_name=table_name,
            columns=columns,
            measures=measures,
        )
        widget_validation = self._validate_widgets(dashboard_guid)

        dashboard_url = (
            f"{self.config.public_base_url}/visiology-designer/workspaces/"
            f"{self.config.workspace_id}/dashboards/{dashboard_guid}"
        )
        return {
            "workspace_id": self.config.workspace_id,
            "dataset_id": dataset_id,
            "dataset_name": dataset_name,
            "table_id": table_id,
            "table_name": table_name,
            "row_count": row_count,
            "columns": columns,
            "measures": measures,
            "dashboard_guid": dashboard_guid,
            "dashboard_url": dashboard_url,
            "validation": validation,
            "widget_validation": widget_validation,
        }

    def _ensure_token(self) -> str:
        if self._token:
            return self._token
        response = self.client.post(
            "/keycloak/realms/Visiology/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": self.config.client_id,
                "username": self.config.username,
                "password": self.config.password,
                "scope": (
                    "openid profile email roles formula_engine dashboard_service "
                    "data_management_service workspace_service forms_service edge_service ai_agent_service"
                ),
            },
            headers={"Content-Type": "application/x-www-form-urlencoded", "Host": self.config.host_header},
        )
        self._raise_for_response(response, "Не удалось получить токен Visiology")
        self._token = response.json()["access_token"]
        self.client.headers.update({"Authorization": f"Bearer {self._token}"})
        return self._token

    def _extract_table(self, payload: dict[str, Any]) -> dict[str, Any]:
        tables = payload.get("tables") or []
        if not tables:
            raise ValueError("В payload нет tables: нечего загружать в Visiology")
        table = tables[0]
        if not isinstance(table, dict):
            raise ValueError("Некорректный tables[0] в payload")
        if not table.get("columns") or not table.get("rows"):
            raise ValueError("В таблице payload нет columns или rows")
        return table

    def _build_csv(self, table: dict[str, Any]) -> tuple[bytes, list[dict[str, str]], int]:
        raw_columns = [str(col) for col in (table.get("columns") or [])]
        rows = table.get("rows") or []
        name_map: dict[str, str] = {}
        used: set[str] = set()
        for index, raw_name in enumerate(raw_columns):
            name = _safe_column(raw_name, f"column_{index + 1}")
            base = name
            suffix = 2
            while name.lower() in used:
                name = f"{base}_{suffix}"
                suffix += 1
            used.add(name.lower())
            name_map[raw_name] = name

        typed_columns: list[dict[str, str]] = []
        for raw_name in raw_columns:
            values = [row.get(raw_name) if isinstance(row, dict) else None for row in rows]
            typed_columns.append({"name": name_map[raw_name], "source": raw_name, "type": _infer_visiology_type(values)})

        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\r\n")
        writer.writerow([col["name"] for col in typed_columns])
        for row in rows:
            writer.writerow([
                "" if not isinstance(row, dict) or row.get(col["source"]) is None else row.get(col["source"])
                for col in typed_columns
            ])
        return output.getvalue().encode("utf-8"), typed_columns, len(rows)

    def _chart_metric_fields(self, payload: dict[str, Any]) -> set[str]:
        fields: set[str] = set()
        for chart in list(payload.get("charts") or []) + list(payload.get("kpi_rows") or []):
            if not isinstance(chart, dict):
                continue
            for key in ("y_field",):
                value = chart.get(key)
                if value:
                    fields.add(_safe_column(value, str(value)))
            for value in chart.get("metric_fields") or []:
                if value:
                    fields.add(_safe_column(value, str(value)))
        return fields

    def _create_model(self, dataset_id: str, name: str) -> None:
        response = self.client.post(
            f"/formula-engine/api/v1/workspaces/{self.config.workspace_id}/datasets/{dataset_id}/model",
            json={"name": name, "description": "Data Agent auto-published dashboard", "storageMode": "Import"},
        )
        self._raise_for_response(response, "Не удалось создать модель Visiology")

    def _upload_csv_dataset(
        self,
        dataset_id: str,
        table_name: str,
        csv_bytes: bytes,
        columns: list[dict[str, str]],
    ) -> str:
        original_file = f"{table_name}.csv"
        stamp = int(time.time() * 1000)
        uploaded_file = f"{table_name}_{stamp}.csv"
        upload_uri_response = self.client.get(
            f"/data-management-service/api/v1/workspaces/{self.config.workspace_id}/datasets/"
            f"{dataset_id}/fileLoader/{uploaded_file}/PresignedUploadURI"
        )
        self._raise_for_response(upload_uri_response, "Не удалось получить PresignedUploadURI")
        upload_url = self._json_or_text(upload_uri_response)
        if isinstance(upload_url, dict):
            upload_url = upload_url.get("url") or upload_url.get("uri") or upload_url.get("presignedUploadUri")
        if not isinstance(upload_url, str) or not upload_url:
            raise RuntimeError(f"Visiology вернула некорректный PresignedUploadURI: {upload_uri_response.text[:300]}")

        put_response = httpx.put(
            self._rewrite_url(upload_url),
            content=csv_bytes,
            headers={"Host": self.config.host_header, "Content-Type": "text/csv; charset=utf-8"},
            verify=self.config.verify_ssl,
            timeout=httpx.Timeout(self.config.timeout),
        )
        self._raise_for_response(put_response, "Не удалось загрузить CSV в Visiology storage")

        load_columns = [
            {
                "id": str(uuid.uuid4()),
                "originalType": col["type"],
                "newType": col["type"],
                "originalHeader": col["name"],
                "newHeader": col["name"],
                "isNullable": True,
                "enabled": True,
            }
            for col in columns
        ]

        load_body = {
            "tableName": table_name,
            "originalFileName": original_file,
            "newFileName": original_file,
            "bucketName": "csv",
            "columns": load_columns,
            "timeStamp": stamp,
            "measures": [],
            "csvWithNames": True,
            "useCRLF": True,
            "delimiter": ",",
            "fullPath": "",
            "inNetworkStorage": False,
            "needRefreshFile": False,
        }
        load_response = self.client.put(
            f"/data-management-service/api/v1/workspaces/{self.config.workspace_id}/datasets/"
            f"{dataset_id}/dataloader/LoadCSVWithMeta",
            json=load_body,
        )
        self._raise_for_response(load_response, "Не удалось выполнить LoadCSVWithMeta")
        table_id = self._json_or_text(load_response)
        if isinstance(table_id, dict):
            table_id = table_id.get("tableId") or table_id.get("id")
        if not isinstance(table_id, str) or not table_id:
            raise RuntimeError(f"Visiology вернула некорректный table id: {load_response.text[:300]}")

        refresh_response = self.client.post(
            f"/data-management-service/api/v1/workspaces/{self.config.workspace_id}/datasets/"
            f"{dataset_id}/table/{table_id}/refresh",
            json={},
        )
        self._raise_for_response(refresh_response, "Не удалось запустить refresh таблицы Visiology")
        return table_id

    def _wait_dms_operations(self, dataset_id: str) -> None:
        deadline = time.time() + max(30, self.config.timeout)
        while time.time() < deadline:
            response = self.client.get(
                f"/data-management-service/api/v1/workspaces/{self.config.workspace_id}/datasets/{dataset_id}/model/operations"
            )
            self._raise_for_response(response, "Не удалось получить статус DMS operations")
            operations = response.json()
            if not operations:
                return
            if all(str(op.get("state") or op.get("status") or "").lower() in {"completed", "success", "finished"} for op in operations):
                return
            time.sleep(2)
        raise TimeoutError("DMS refresh не завершился за отведенное время")

    def _create_measures(
        self,
        dataset_id: str,
        table_id: str,
        table_name: str,
        columns: list[str],
    ) -> list[dict[str, str]]:
        measures: list[dict[str, str]] = []
        run_stamp = uuid.uuid4().hex[:6]
        for column in columns:
            measure_name = self._measure_name(column, run_stamp)
            expression = f"SUM('{table_name}'[{column}])"
            measure_id = str(uuid.uuid4())
            response = self.client.put(
                f"/formula-engine/api/v1/workspaces/{self.config.workspace_id}/datasets/{dataset_id}/"
                f"tables/{table_id}/measures/{measure_id}",
                json={
                    "name": measure_name,
                    "description": None,
                    "expression": expression,
                    "formatString": "#,##0.##",
                    "isSimpleMeasure": True,
                    "displayFolder": None,
                },
            )
            self._raise_for_response(response, f"Не удалось создать меру {measure_name}")
            measures.append({"id": measure_id, "name": measure_name, "column": column, "expression": expression})
        return measures

    def _measure_name(self, column: str, stamp: str = "") -> str:
        text = _safe_column(column, "value")
        if stamp:
            return f"{text[:60]}_{stamp}"
        return text[:80] or "value"

    def _validate_dataset(
        self,
        dataset_id: str,
        table_name: str,
        columns: list[dict[str, str]],
        measures: list[dict[str, str]],
    ) -> dict[str, Any]:
        first_dimension = next((col["name"] for col in columns if col["type"] == "String"), columns[0]["name"])
        if measures:
            expr = (
                f"EVALUATE TOPN(5, SUMMARIZECOLUMNS('{table_name}'[{first_dimension}], "
                f"\"metric\", '{table_name}'[{measures[0]['name']}]), [metric], DESC)"
            )
        else:
            expr = f"EVALUATE TOPN(5, '{table_name}', '{table_name}'[{first_dimension}], ASC)"

        last_error: RuntimeError | None = None
        for attempt in range(6):
            response = self.client.post(
                f"/formula-engine/api/v1/workspaces/{self.config.workspace_id}/datasets/{dataset_id}/model/query",
                json={"expression": expr},
            )
            if response.status_code < 400:
                return response.json()

            detail = response.text[:300]
            lowered = detail.lower()
            if (
                response.status_code == 400
                and (
                    "не загружена" in detail
                    or "not loaded" in lowered
                    or "не найдена" in detail
                    or "not found" in lowered
                )
            ):
                last_error = RuntimeError(
                    f"DAX-проверка датасета Visiology не прошла: HTTP {response.status_code}: {detail}"
                )
                time.sleep(8)
                continue

            self._raise_for_response(response, "DAX-проверка датасета Visiology не прошла")

        if last_error:
            raise last_error
        return {}

    def _upload_dashboard(
        self,
        payload: dict[str, Any],
        dashboard_name: str,
        dataset_id: str,
        table_name: str,
        columns: list[dict[str, str]],
        measures: list[dict[str, str]],
    ) -> str:
        bundle = self._build_dashboard_bundle(payload, dashboard_name, dataset_id, table_name, columns, measures)
        files = {"DashboardFile": ("dashboard.json", json.dumps(bundle, ensure_ascii=False), "application/json")}
        response = self.client.post(
            f"/dashboard-service/api/workspaces/{self.config.workspace_id}/dashboards/upload",
            params={
                "DashboardName": dashboard_name,
                "DatasetId": dataset_id,
                "WorkspaceDatasetId": self.config.workspace_id,
                "ThemeGuid": self.config.theme_guid,
            },
            files=files,
        )
        if response.status_code >= 400:
            logger.warning("Visiology dashboard upload returned %s: %s", response.status_code, response.text[:500])
            existing = self._find_dashboard_by_name(dashboard_name)
            if existing:
                return existing
            self._raise_for_response(response, "Не удалось загрузить dashboard JSON в Visiology")
        data = response.json()
        guid = data.get("dashboardGuid") or data.get("guid") or data.get("id")
        if not guid:
            existing = self._find_dashboard_by_name(dashboard_name)
            if existing:
                return existing
            raise RuntimeError(f"Visiology не вернула dashboardGuid: {response.text[:300]}")
        return str(guid)

    def _build_dashboard_bundle(
        self,
        payload: dict[str, Any],
        dashboard_name: str,
        dataset_id: str,
        table_name: str,
        columns: list[dict[str, str]],
        measures: list[dict[str, str]],
    ) -> dict[str, Any]:
        template = self._fetch_template()
        dashboard = template["dashboard"]
        dashboard["guid"] = _guid()
        dashboard["name"] = dashboard_name
        dashboard["workspaceId"] = self.config.workspace_id
        dashboard["dataset"] = {"workspaceId": self.config.workspace_id, "datasetId": dataset_id}
        dashboard["themeGuid"] = self.config.theme_guid
        sheet = dashboard["sheets"][0]
        sheet["guid"] = _guid()
        sheet["name"] = "Data Agent"

        widgets = self._make_widgets(
            template_widgets=template["dashboard"]["sheets"][0]["widgets"],
            payload=payload,
            table_name=table_name,
            columns=columns,
            measures=measures,
        )
        sheet["widgets"] = widgets
        return {"dashboard": dashboard, "dashboardMeasures": [], "images": []}

    def _fetch_template(self) -> dict[str, Any]:
        response = self.client.get(
            f"/dashboard-service/api/workspaces/{self.config.template_workspace_id}/dashboards/"
            f"{self.config.template_dashboard_id}/download"
        )
        self._raise_for_response(response, "Не удалось скачать dashboard template из Visiology")
        data = response.json()
        if "dashboard" not in data:
            raise RuntimeError("Некорректный dashboard template: нет поля dashboard")
        return data

    def _make_widgets(
        self,
        template_widgets: list[dict[str, Any]],
        payload: dict[str, Any],
        table_name: str,
        columns: list[dict[str, str]],
        measures: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        # Canvas size used by Visiology designer (pixels)
        CANVAS_W = 1900
        CANVAS_H = 980
        MARGIN = 20

        pie = self._template_widget(template_widgets, "HighchartsPieChart")
        column = self._template_widget(template_widgets, "HighchartsColumnChart")
        line = self._template_widget(template_widgets, "HighchartsChart")
        table = self._template_widget(template_widgets, "OlapTable")
        filter_widget = self._template_widget(template_widgets, "Filter")

        measure_by_column = {item["column"]: item for item in measures}
        first_measure = measures[0] if measures else None
        dimension_columns = [col["name"] for col in columns if col["type"] == "String"] or [columns[0]["name"]]
        widgets: list[dict[str, Any]] = []
        raw_chart_items = [chart for chart in payload.get("charts") or [] if isinstance(chart, dict)]
        chart_items = [chart for chart in raw_chart_items if _chart_type(chart) not in {"big_number", "kpi"}]
        kpi_rows = [r for r in payload.get("kpi_rows") or [] if isinstance(r, dict)]
        for chart in raw_chart_items:
            if _chart_type(chart) not in {"big_number", "kpi"}:
                continue
            title = str(chart.get("slice_name") or chart.get("title") or chart.get("name") or "KPI")
            if any(str(row.get("title") or row.get("metric_name") or "") == title for row in kpi_rows):
                continue
            kpi_rows.append(
                {
                    "title": title,
                    "metric_name": title,
                    "value": chart.get("value"),
                    "y_field": chart.get("y_field") or (first_measure or {}).get("column"),
                    "position": chart.get("position"),
                    "color": chart.get("color"),
                }
            )

        # Grid fallback state (used when position is absent)
        grid_x = MARGIN
        grid_y = MARGIN
        max_content_bottom = MARGIN

        def _next_grid(w: int, h: int) -> tuple[int, int]:
            nonlocal grid_x, grid_y
            if grid_x + w > CANVAS_W - MARGIN:
                grid_x = MARGIN
                grid_y += h + MARGIN
            pos = (grid_x, grid_y)
            grid_x += w + MARGIN
            return pos

        def _pos_from_chart(chart: dict[str, Any], default_w: int, default_h: int) -> tuple[int, int, int, int]:
            """Convert vision position (0..1) or use grid fallback."""
            pos = chart.get("position")
            if isinstance(pos, dict) and all(k in pos for k in ("left", "top", "width", "height")):
                px = max(MARGIN, int(pos["left"] * CANVAS_W))
                py = max(MARGIN, int(pos["top"] * CANVAS_H))
                pw = max(100, int(pos["width"] * CANVAS_W) - MARGIN)
                ph = max(80, int(pos["height"] * CANVAS_H) - MARGIN)
                return px, py, pw, ph
            gx, gy = _next_grid(default_w, default_h)
            return gx, gy, default_w, default_h

        # ── KPI cards from kpi_rows ──────────────────────────────────────────
        kpi_count = len(kpi_rows)
        kpi_w = max(200, min(420, (CANVAS_W - MARGIN * (kpi_count + 1)) // max(kpi_count, 1)))
        for kpi_idx, kpi in enumerate(kpi_rows[:6]):
            if not column:
                break
            kpi_title = str(kpi.get("title") or kpi.get("metric_name") or f"KPI {kpi_idx + 1}")
            kpi_y_field = _safe_column(kpi.get("y_field") or "", "value")
            kpi_measure = measure_by_column.get(kpi_y_field) or first_measure
            if not kpi_measure:
                continue
            kpi_pos = kpi.get("position")
            if isinstance(kpi_pos, dict) and all(k in kpi_pos for k in ("left", "top", "width", "height")):
                wx = max(MARGIN, int(kpi_pos["left"] * CANVAS_W))
                wy = max(MARGIN, int(kpi_pos["top"] * CANVAS_H))
                ww = max(100, int(kpi_pos["width"] * CANVAS_W) - MARGIN)
                wh = max(140, int(kpi_pos["height"] * CANVAS_H) - MARGIN)
            else:
                wx = MARGIN + kpi_idx * (kpi_w + MARGIN)
                wy = MARGIN
                ww = kpi_w
                wh = 120
            widget = copy.deepcopy(column)
            self._bind_chart(widget, kpi_title, wx, wy, ww, wh)
            widget["dataSettings"]["columns"] = [
                self._meas("YAxis", table_name, kpi_measure["name"]),
            ]
            self._set_series_ids(widget, kpi_measure["name"])
            # Style as KPI: accent color, dark bg
            self._apply_kpi_style(widget, kpi.get("color") or (kpi_measure["column"]))
            self._apply_kpi_code(widget)
            widgets.append(widget)
            max_content_bottom = max(max_content_bottom, wy + wh)

        # Advance grid_y past KPI row if KPIs were placed on grid
        if kpi_rows and not any(r.get("position") for r in kpi_rows[:6]):
            grid_y = max(grid_y, MARGIN + 120 + MARGIN)
            grid_x = MARGIN

        # ── Main charts ──────────────────────────────────────────────────────
        for index, chart in enumerate(chart_items[:10]):
            chart_type = _chart_type(chart)
            x_field = _safe_column(chart.get("x_field") or "category", "category")
            y_field = _safe_column(chart.get("y_field") or "", "value")
            measure = measure_by_column.get(y_field) or first_measure
            title = str(chart.get("slice_name") or chart.get("title") or f"Виджет {index + 1}")
            series_colors = chart.get("series_colors") or []

            if chart_type in {"pie", "donut"} and pie and measure:
                wx, wy, ww, wh = _pos_from_chart(chart, 560, 330)
                widget = copy.deepcopy(pie)
                self._bind_chart(widget, title, wx, wy, ww, wh)
                widget["dataSettings"]["columns"] = [
                    self._dim("Legend", table_name, x_field),
                    self._meas("Values", table_name, measure["name"]),
                ]
                self._set_series_ids(widget, measure["name"])
                self._apply_pie_style(widget)
                if series_colors:
                    self._apply_series_colors(widget, series_colors)
                widgets.append(widget)
                max_content_bottom = max(max_content_bottom, wy + wh)
            elif chart_type in {"bar", "dist_bar", "hbar", "column", "bar_horizontal"} and column and measure:
                wx, wy, ww, wh = _pos_from_chart(chart, 860, 330)
                widget = copy.deepcopy(column)
                self._bind_chart(widget, title, wx, wy, ww, wh)
                widget["dataSettings"]["columns"] = [
                    self._dim("XAxis", table_name, x_field),
                    self._meas("YAxis", table_name, measure["name"]),
                ]
                self._set_series_ids(widget, measure["name"])
                self._apply_chart_style(widget, series_colors)
                if series_colors:
                    self._apply_series_colors(widget, series_colors)
                widgets.append(widget)
                max_content_bottom = max(max_content_bottom, wy + wh)
            elif chart_type in {"line", "area", "combo"} and line and measure:
                wx, wy, ww, wh = _pos_from_chart(chart, 860, 330)
                widget = copy.deepcopy(line)
                self._bind_chart(widget, title, wx, wy, ww, wh)
                widget["dataSettings"]["columns"] = [
                    self._dim("XAxis", table_name, x_field),
                    self._meas("YAxis", table_name, measure["name"]),
                ]
                self._set_series_ids(widget, measure["name"])
                self._apply_chart_style(widget, series_colors)
                if series_colors:
                    self._apply_series_colors(widget, series_colors)
                widgets.append(widget)
                max_content_bottom = max(max_content_bottom, wy + wh)
            elif chart_type in {"table", "pivot_table"} and table:
                wx, wy, ww, wh = _pos_from_chart(chart, 1200, 360)
                widget = copy.deepcopy(table)
                self._bind_chart(widget, title, wx, wy, ww, wh)
                value_cols = [self._meas("Values", table_name, m["name"]) for m in measures[:4]]
                widget["dataSettings"]["columns"] = [self._dim("Rows", table_name, dimension_columns[0])] + value_cols
                widgets.append(widget)
                max_content_bottom = max(max_content_bottom, wy + wh)

        if not widgets and column and first_measure:
            widget = copy.deepcopy(column)
            self._bind_chart(widget, str(payload.get("dashboard_title") or "Data Agent"), MARGIN, grid_y, 900, 360)
            widget["dataSettings"]["columns"] = [
                self._dim("XAxis", table_name, dimension_columns[0]),
                self._meas("YAxis", table_name, first_measure["name"]),
            ]
            self._set_series_ids(widget, first_measure["name"])
            widgets.append(widget)
            grid_y += 380
            max_content_bottom = max(max_content_bottom, grid_y)

        has_explicit_table = any(_chart_type(chart) in {"table", "pivot_table"} for chart in chart_items)
        if table and measures and has_explicit_table:
            widget = copy.deepcopy(table)
            table_y = max(grid_y + MARGIN, max_content_bottom + MARGIN)
            self._bind_chart(widget, "Данные", MARGIN, table_y, CANVAS_W - 2 * MARGIN, 360)
            widget["dataSettings"]["columns"] = (
                [self._dim("Rows", table_name, col) for col in dimension_columns[:3]]
                + [self._meas("Values", table_name, m["name"]) for m in measures[:5]]
            )
            widgets.append(widget)

        for z_index, widget in enumerate(widgets, start=1):
            widget["guid"] = _guid()
            widget["zIndex"] = z_index
            if not isinstance(widget.get("metadata"), str):
                widget["metadata"] = json.dumps(widget.get("metadata") or {}, ensure_ascii=False)
        return widgets

    def _template_widget(self, widgets: list[dict[str, Any]], widget_type: str) -> dict[str, Any] | None:
        return next((widget for widget in widgets if widget.get("Type") == widget_type), None)

    def _bind_chart(self, widget: dict[str, Any], title: str, x: int, y: int, width: int, height: int) -> None:
        widget["position"] = {"x": x, "y": y}
        widget["size"] = {"width": width, "height": height}
        widget.setdefault("title", {})["enabled"] = True
        widget["title"]["text"] = str(title)
        widget["title"].setdefault("textStyle", {})["color"] = "rgba(35,42,52,1)"
        widget["title"]["textStyle"]["fontSize"] = 16
        widget["title"]["textStyle"]["isBold"] = True
        widget.setdefault("background", {}).setdefault("color", {})["color"] = "rgba(255,255,255,1)"

    def _dim(self, role: str, table_name: str, name: str) -> dict[str, Any]:
        return self._column_binding(role, table_name, name, 0)

    def _meas(self, role: str, table_name: str, name: str) -> dict[str, Any]:
        return self._column_binding(role, table_name, name, 1)

    def _column_binding(self, role: str, table_name: str, name: str, column_type: int) -> dict[str, Any]:
        return {
            "type": column_type,
            "dataRoleName": role,
            "tableName": table_name,
            "name": name,
            "displayName": name,
            "expression": None,
            "summarizeBy": 2,
            "formatString": None,
            "conditionalFormatting": {"font": None, "background": None, "icons": None},
        }

    def _set_series_ids(self, widget: dict[str, Any], measure_name: str) -> None:
        for series in widget.get("series") or []:
            series["id"] = measure_name

    def _apply_kpi_style(self, widget: dict[str, Any], accent_hex: str | None) -> None:
        """Style a column widget as a compact KPI card with white surface and dark labels."""
        widget.setdefault("background", {})["enabled"] = True
        widget["background"].setdefault("color", {})["colorType"] = 0
        widget["background"]["color"]["color"] = "rgba(255,255,255,1)"

        for series in widget.get("series") or []:
            series.setdefault("color", {})["colorType"] = 0
            series["color"]["color"] = "rgba(226,238,255,1)"

        widget.setdefault("legend", {})["enabled"] = False
        for axis_name in ("xAxis", "yAxis", "yAxisOpposite"):
            axis = widget.get(axis_name)
            if isinstance(axis, dict):
                axis["lineEnabled"] = False
                axis.setdefault("labels", {})["enabled"] = False
                axis.setdefault("grid", {})["enabled"] = False

        labels = widget.setdefault("dataLabels", {})
        labels["enabled"] = True
        labels["formatter"] = "Math.round(@value.y).toString().replace(/(?!^)(?=(?:\\d{3})+(?:\\.|$))/gm, ' ')"
        labels.setdefault("textStyle", {})["color"] = "rgba(22,28,36,1)"
        labels["textStyle"]["fontSize"] = 34
        labels["textStyle"]["isBold"] = True

    def _apply_chart_style(self, widget: dict[str, Any], colors: list[str] | None = None) -> None:
        widget.setdefault("background", {}).setdefault("color", {})["color"] = "rgba(255,255,255,1)"
        for axis_name in ("xAxis", "yAxis", "yAxisOpposite"):
            axis = widget.get(axis_name)
            if not isinstance(axis, dict):
                continue
            axis["lineEnabled"] = False
            axis.setdefault("labels", {})["enabled"] = True
            axis["labels"].setdefault("textStyle", {})["color"] = "rgba(78,85,96,1)"
            axis["labels"]["textStyle"]["fontSize"] = 13
            axis.setdefault("grid", {})["enabled"] = axis_name == "yAxis"

        labels = widget.get("dataLabels")
        if isinstance(labels, dict):
            labels["enabled"] = True
            labels["formatter"] = "Math.round(@value.y).toString().replace(/(?!^)(?=(?:\\d{3})+(?:\\.|$))/gm, ' ')"
            labels.setdefault("textStyle", {})["color"] = "rgba(46,54,66,1)"
            labels["textStyle"]["fontSize"] = 13
            labels["textStyle"]["isBold"] = True

        palette = colors or ["#4085D9"]
        for index, series in enumerate(widget.get("series") or []):
            color = palette[index % len(palette)]
            if isinstance(color, str) and color.startswith("#"):
                try:
                    r = int(color[1:3], 16)
                    g = int(color[3:5], 16)
                    b = int(color[5:7], 16)
                except Exception:
                    continue
                series.setdefault("color", {})["colorType"] = 0
                series["color"]["color"] = f"rgba({r},{g},{b},1)"

    def _apply_pie_style(self, widget: dict[str, Any]) -> None:
        widget.setdefault("legend", {})["enabled"] = True
        widget["legend"].setdefault("textStyle", {})["color"] = "rgba(31,41,55,1)"
        widget["legend"]["textStyle"]["fontSize"] = 13
        widget["overriddenCode"] = """
const darkText = '#1f2937';
const mutedText = '#4b5563';
const series = (w.series || []).map(function (item) {
    return Object.assign({}, item, {
        type: 'pie',
        innerSize: '58%',
        borderWidth: 0,
        dataLabels: {
            enabled: true,
            distance: 14,
            connectorColor: '#94a3b8',
            connectorWidth: 1,
            formatter: function () {
                return this.y ? Highcharts.numberFormat(this.y, 0, '.', ' ') : '';
            },
            style: {
                color: darkText,
                fontSize: '13px',
                fontWeight: '700',
                textOutline: 'none'
            }
        },
        showInLegend: true
    });
});

Highcharts.chart({
    chart: Object.assign({}, w.general, {
        type: 'pie',
        backgroundColor: 'transparent',
        spacing: [8, 12, 8, 12]
    }),
    title: { text: null },
    credits: { enabled: false },
    tooltip: w.tooltip,
    legend: Object.assign({}, w.legend, {
        enabled: true,
        itemStyle: {
            color: mutedText,
            fontSize: '13px',
            fontWeight: '500'
        },
        itemHoverStyle: { color: darkText }
    }),
    plotOptions: Object.assign({}, w.plotOptions, {
        pie: {
            innerSize: '58%',
            borderWidth: 0,
            allowPointSelect: true,
            cursor: 'pointer',
            dataLabels: {
                enabled: true,
                distance: 14,
                connectorColor: '#94a3b8',
                connectorWidth: 1,
                formatter: function () {
                    return this.y ? Highcharts.numberFormat(this.y, 0, '.', ' ') : '';
                },
                style: {
                    color: darkText,
                    fontSize: '13px',
                    fontWeight: '700',
                    textOutline: 'none'
                }
            },
            showInLegend: true
        }
    }),
    series: series
});
"""

    def _apply_kpi_code(self, widget: dict[str, Any]) -> None:
        widget["overriddenCode"] = """
const point = (w.series && w.series[0] && w.series[0].data && w.series[0].data[0]) || {};
const rawValue = typeof point === 'number' ? point : (point.y ?? point.value ?? 0);
const value = Number(rawValue) || 0;
const formatted = Highcharts.numberFormat(value, 0, '.', ' ');

Highcharts.chart({
    chart: Object.assign({}, w.general, {
        type: 'column',
        backgroundColor: 'transparent',
        spacing: [8, 8, 8, 8]
    }),
    title: { text: null },
    xAxis: { visible: false },
    yAxis: { visible: false, min: 0 },
    legend: { enabled: false },
    tooltip: { enabled: false },
    credits: { enabled: false },
    plotOptions: {
        series: {
            animation: false,
            enableMouseTracking: false,
            borderWidth: 0,
            dataLabels: {
                enabled: true,
                inside: true,
                formatter: function () { return formatted; },
                style: {
                    color: '#1f2937',
                    fontSize: '34px',
                    fontWeight: '700',
                    textOutline: 'none'
                }
            }
        },
        column: { borderRadius: 8, pointPadding: 0.18, groupPadding: 0.2 }
    },
    series: [{
        name: '',
        data: [{ y: Math.max(value, 1), color: '#dbeafe' }]
    }]
});
"""

    def _apply_series_colors(self, widget: dict[str, Any], colors: list[str]) -> None:
        """Apply per-series colors extracted from vision to Visiology widget."""
        for i, series in enumerate(widget.get("series") or []):
            if i >= len(colors):
                break
            hex_color = colors[i]
            if not hex_color or not hex_color.startswith("#"):
                continue
            try:
                r = int(hex_color[1:3], 16)
                g = int(hex_color[3:5], 16)
                b = int(hex_color[5:7], 16)
                rgba = f"rgba({r},{g},{b},1)"
            except Exception:
                continue
            series.setdefault("color", {})["colorType"] = 0
            series["color"]["color"] = rgba

    def _validate_widgets(self, dashboard_guid: str) -> list[dict[str, Any]]:
        dashboard = self.client.get(
            f"/dashboard-service/api/workspaces/{self.config.workspace_id}/dashboards/{dashboard_guid}"
        )
        self._raise_for_response(dashboard, "Не удалось получить опубликованный dashboard для проверки")
        result: list[dict[str, Any]] = []
        for widget in dashboard.json().get("sheets", [{}])[0].get("widgets", []):
            if widget.get("Type") not in {"HighchartsPieChart", "HighchartsColumnChart", "HighchartsChart", "OlapTable", "Filter"}:
                continue
            guid = widget.get("guid")
            response = self.client.get(
                f"/dashboard-service/api/workspaces/{self.config.workspace_id}/dashboards/"
                f"{dashboard_guid}/widgets/{guid}/data/by-widget-id"
            )
            ok = response.status_code < 400
            body: Any = None
            try:
                body = response.json()
            except Exception:
                body = response.text[:200]
            if isinstance(body, dict) and body.get("message"):
                ok = False
            result.append({
                "widget_guid": guid,
                "type": widget.get("Type"),
                "title": (widget.get("title") or {}).get("text"),
                "ok": ok,
                "status_code": response.status_code,
                "message": body.get("message") if isinstance(body, dict) else None,
            })
        return result

    def _find_dashboard_by_name(self, name: str) -> str | None:
        response = self.client.get(f"/dashboard-service/api/workspaces/{self.config.workspace_id}/dashboards")
        if response.status_code >= 400:
            return None
        dashboards = response.json()
        if isinstance(dashboards, dict):
            dashboards = dashboards.get("dashboards") or dashboards.get("items") or dashboards.get("data") or []
        for item in dashboards or []:
            if not isinstance(item, dict):
                continue
            if item.get("name") == name or item.get("title") == name:
                guid = item.get("guid") or item.get("dashboardGuid") or item.get("id")
                if guid:
                    return str(guid)
        return None

    def _rewrite_url(self, url: str) -> str:
        parsed = urlparse(url)
        base = urlparse(self.config.api_base_url)
        if parsed.hostname == self.config.host_header and base.hostname:
            return urlunparse(parsed._replace(scheme=base.scheme, netloc=base.netloc))
        return url

    def _json_or_text(self, response: httpx.Response) -> Any:
        try:
            return response.json()
        except Exception:
            return response.text.strip().strip('"')

    def _raise_for_response(self, response: httpx.Response, message: str) -> None:
        if response.status_code < 400:
            return
        detail = response.text[:1000]
        raise RuntimeError(f"{message}: HTTP {response.status_code}: {detail}")


def publish_to_visiology(payload: dict[str, Any], settings: Settings) -> dict[str, Any]:
    client = VisiologyClient(VisiologyPublishConfig.from_settings(settings))
    try:
        return client.publish_dashboard(payload)
    finally:
        client.close()
