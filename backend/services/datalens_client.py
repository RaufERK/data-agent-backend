"""Publish generated Data Agent dashboards to Yandex DataLens."""
from __future__ import annotations

import copy
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from backend.builders.datalens_export import DataLensExportBuilder
from backend.builders.datalens_table_helpers import _auto_fields
from backend.config import Settings

logger = logging.getLogger("data_agent.datalens")


def _slug(value: Any, fallback: str = "data-agent") -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9_-]+", "-", text, flags=re.IGNORECASE)
    text = re.sub(r"-+", "-", text).strip("-")
    return text or fallback


def _extract_entry_id(response: dict[str, Any]) -> str:
    entry = response.get("entry")
    if isinstance(entry, dict):
        value = entry.get("entryId") or entry.get("id")
        if value:
            return str(value)
    for key in ("entryId", "dashboardId", "widgetId", "id"):
        value = response.get(key)
        if value:
            return str(value)
    raise ValueError(f"DataLens response does not contain entry id: {response}")


def _is_number(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    try:
        float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return False
    return True


def _field_guid(name: str) -> str:
    slug = re.sub(r"[^a-z0-9_]+", "_", str(name).lower(), flags=re.IGNORECASE).strip("_")
    return f"{slug or 'field'}_{uuid.uuid4().hex[:6]}"


def _column_names(table: dict[str, Any]) -> list[str]:
    columns = [str(col) for col in (table.get("columns") or [])]
    rows = table.get("rows") or []
    if not columns and rows and isinstance(rows[0], dict):
        columns = [str(col) for col in rows[0].keys()]
    return columns


def _row_value(row: Any, columns: list[str], column: str) -> Any:
    if isinstance(row, dict):
        return row.get(column)
    if isinstance(row, (list, tuple)) and column in columns:
        index = columns.index(column)
        if index < len(row):
            return row[index]
    return None


@dataclass(frozen=True)
class DataLensPublishConfig:
    api_base_url: str
    public_base_url: str
    iam_token: str
    oauth_token: str
    org_id: str
    cloud_id: str
    collection_id: str
    timeout: int
    native_connection_id: str
    native_connection_workbook_id: str
    native_connection_rev_id: str
    native_source_id: str

    @classmethod
    def from_settings(cls, settings: Settings) -> "DataLensPublishConfig":
        return cls(
            api_base_url=settings.datalens_api_base_url.rstrip("/"),
            public_base_url=settings.datalens_public_base_url.rstrip("/"),
            iam_token=settings.datalens_iam_token.strip(),
            oauth_token=settings.datalens_oauth_token.strip(),
            org_id=settings.datalens_org_id.strip(),
            cloud_id=settings.datalens_cloud_id.strip(),
            collection_id=settings.datalens_collection_id.strip(),
            timeout=settings.datalens_timeout,
            native_connection_id=settings.datalens_native_connection_id.strip(),
            native_connection_workbook_id=settings.datalens_native_connection_workbook_id.strip(),
            native_connection_rev_id=settings.datalens_native_connection_rev_id.strip(),
            native_source_id=settings.datalens_native_source_id.strip(),
        )


class DataLensClient:
    def __init__(self, config: DataLensPublishConfig):
        self.config = config
        iam_token = config.iam_token or self._exchange_oauth_token(config.oauth_token)
        org_id = config.org_id or self._resolve_org_id(iam_token, config.cloud_id)
        if not iam_token:
            raise ValueError("Set DATALENS_IAM_TOKEN or DATALENS_OAUTH_TOKEN")
        if not org_id:
            raise ValueError("Set DATALENS_ORG_ID or DATALENS_CLOUD_ID")
        self.client = httpx.Client(
            base_url=config.api_base_url,
            timeout=httpx.Timeout(config.timeout),
            follow_redirects=True,
            headers={
                "Authorization": f"Bearer {iam_token}",
                "x-dl-org-id": org_id,
                "x-dl-api-version": "1",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    def close(self) -> None:
        self.client.close()

    def _exchange_oauth_token(self, oauth_token: str) -> str:
        if not oauth_token:
            return ""
        response = httpx.post(
            "https://iam.api.cloud.yandex.net/iam/v1/tokens",
            json={"yandexPassportOauthToken": oauth_token},
            timeout=httpx.Timeout(self.config.timeout),
        )
        if response.is_success:
            return str(response.json().get("iamToken") or "")
        raise RuntimeError(f"Failed to exchange Yandex OAuth token: HTTP {response.status_code}: {response.text}")

    def _resolve_org_id(self, iam_token: str, cloud_id: str) -> str:
        if not iam_token or not cloud_id:
            return ""
        response = httpx.get(
            "https://resource-manager.api.cloud.yandex.net/resource-manager/v1/clouds",
            headers={"Authorization": f"Bearer {iam_token}"},
            timeout=httpx.Timeout(self.config.timeout),
        )
        if not response.is_success:
            raise RuntimeError(f"Failed to resolve Yandex organization id: HTTP {response.status_code}: {response.text}")
        clouds = response.json().get("clouds") or []
        for cloud in clouds:
            if str(cloud.get("id") or "") == cloud_id:
                return str(cloud.get("organizationId") or "")
        raise ValueError(f"Cloud {cloud_id} is not available for this Yandex token")

    def publish_dashboard(self, payload: dict[str, Any]) -> dict[str, Any]:
        title = str(payload.get("dashboard_title") or payload.get("title") or "Data Agent Dashboard").strip()
        stamp = datetime.now().strftime("%Y%m%d %H%M%S")
        workbook_title = f"{title} {stamp}"

        workbook = self._create_workbook(workbook_title)
        workbook_id = str(workbook["workbookId"])

        export_data = DataLensExportBuilder(payload).build()
        entries = export_data["export"]["entries"]
        widget_id_map = self._create_widgets(workbook_id, entries.get("widget") or {})
        dashboard = self._create_dashboard(workbook_id, entries["dash"]["1"], widget_id_map)
        dashboard_id = _extract_entry_id(dashboard)

        return {
            "status": "published",
            "workbook_id": workbook_id,
            "workbook_title": workbook_title,
            "dashboard_id": dashboard_id,
            "dashboard_title": title,
            "widget_count": len(widget_id_map),
            "widget_ids": list(widget_id_map.values()),
            "workbook_url": f"{self.config.public_base_url}/workbooks/{workbook_id}",
            "dashboard_url": f"{self.config.public_base_url}/dashboards/{dashboard_id}",
            "api_base_url": self.config.api_base_url,
        }

    def publish_native_dashboard(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Publish using native DataLens Connection -> Dataset -> Wizard chart objects.

        This mode needs an already reachable DataLens connection. DataLens runs
        dataset queries on its side, so local Data Agent tables are used only for
        field selection and dashboard layout metadata.
        """
        if not self.config.native_connection_id:
            raise ValueError("DATALENS_NATIVE_CONNECTION_ID is not configured")
        connection_workbook_id = self.config.native_connection_workbook_id
        if not connection_workbook_id:
            raise ValueError("DATALENS_NATIVE_CONNECTION_WORKBOOK_ID is not configured")

        title = str(payload.get("dashboard_title") or payload.get("title") or "Data Agent Dashboard").strip()
        stamp = datetime.now().strftime("%Y%m%d %H%M%S")
        dashboard_name = f"{title} native {stamp}"
        dataset_name = f"{title} dataset {stamp}"
        workbook_id = connection_workbook_id

        connection_id = self.config.native_connection_id
        connection_rev_id = self.config.native_connection_rev_id
        try:
            connection = self._get_connection(connection_id=connection_id, rev_id=connection_rev_id)
        except RuntimeError as exc:
            connection_entry = self._find_workbook_connection(workbook_id)
            if not connection_entry:
                raise RuntimeError(
                    f"Configured DataLens connection {connection_id} was not found, "
                    f"and workbook {workbook_id} has no available connection entries. "
                    "Create a connection in that workbook or update DATALENS_NATIVE_CONNECTION_ID."
                ) from exc
            connection_id = str(connection_entry.get("entryId") or "")
            connection_rev_id = str(connection_entry.get("revId") or "")
            logger.warning(
                "Configured DataLens connection %s is unavailable; using workbook connection %s",
                self.config.native_connection_id,
                connection_id,
            )
            connection = self._get_connection(connection_id=connection_id, rev_id=connection_rev_id)
        source_meta = self._select_connection_source(connection)
        payload = self._normalize_native_payload(payload)
        table = self._extract_primary_table(payload)
        fields = self._build_native_fields(source_meta, table)
        dataset = self._create_native_dataset(
            workbook_id=workbook_id,
            connection_id=connection_id,
            source_meta=source_meta,
            fields=fields,
            dataset_name=dataset_name,
        )
        dataset_id = _extract_entry_id(dataset)

        widget_id_map = self._create_native_wizard_charts(
            workbook_id=workbook_id,
            dataset_id=dataset_id,
            dataset_name=dataset_name,
            fields=fields,
            payload=payload,
            name_suffix=stamp,
        )
        dashboard = self._create_native_dashboard(workbook_id, dashboard_name, payload, widget_id_map)
        dashboard_id = _extract_entry_id(dashboard)

        return {
            "status": "published",
            "mode": "native",
            "workbook_id": workbook_id,
            "workbook_title": f"Connection workbook {workbook_id}",
            "connection_id": connection_id,
            "dataset_id": dataset_id,
            "dataset_name": dataset_name,
            "dashboard_id": dashboard_id,
            "dashboard_title": dashboard_name,
            "widget_count": len(widget_id_map),
            "widget_ids": list(widget_id_map.values()),
            "workbook_url": f"{self.config.public_base_url}/workbooks/{workbook_id}",
            "dashboard_url": f"{self.config.public_base_url}/dashboards/{dashboard_id}",
            "api_base_url": self.config.api_base_url,
        }

    def _rpc(self, method: str, body: dict[str, Any]) -> dict[str, Any]:
        response = self.client.post(f"/rpc/{method}", json=body)
        if response.is_success:
            return response.json()
        detail: Any = response.text
        try:
            detail = response.json()
        except ValueError:
            pass
        raise RuntimeError(f"DataLens {method} failed: HTTP {response.status_code}: {detail}")

    def _create_workbook(self, title: str) -> dict[str, Any]:
        return self._rpc(
            "createWorkbook",
            {
                "collectionId": self.config.collection_id or None,
                "title": title,
                "description": "Published from Data Agent",
            },
        )

    def _get_connection(self, *, connection_id: str | None = None, rev_id: str | None = None) -> dict[str, Any]:
        return self._rpc(
            "getConnection",
            {
                "connectionId": connection_id or self.config.native_connection_id,
                "workbookId": self.config.native_connection_workbook_id,
                "rev_id": rev_id if rev_id is not None else self.config.native_connection_rev_id,
            },
        )

    def _find_workbook_connection(self, workbook_id: str) -> dict[str, Any] | None:
        try:
            result = self._rpc(
                "getWorkbookEntries",
                {
                    "workbookId": workbook_id,
                    "includePermissionsInfo": False,
                    "page": 0,
                    "pageSize": 100,
                    "scope": "connection",
                    "orderBy": {"field": "createdAt", "direction": "desc"},
                    "filters": {"name": ""},
                },
            )
        except RuntimeError as exc:
            if "WORKBOOK_NOT_EXISTS" in str(exc) or "404" in str(exc):
                raise RuntimeError(
                    f"DataLens workbook {workbook_id!r} (DATALENS_NATIVE_CONNECTION_WORKBOOK_ID) "
                    "does not exist. Update this setting to a valid workbook that contains the connection."
                ) from exc
            raise
        entries = [
            entry for entry in (result.get("entries") or [])
            if str(entry.get("scope") or "") == "connection" and entry.get("entryId")
        ]
        return entries[0] if entries else None

    def _select_connection_source(self, connection: dict[str, Any]) -> dict[str, Any]:
        sources = connection.get("sources") or []
        if not sources:
            raise ValueError("DataLens native connection has no sources")
        if self.config.native_source_id:
            for source in sources:
                if str(source.get("id") or "") == self.config.native_source_id:
                    return source
            raise ValueError(f"DATALENS_NATIVE_SOURCE_ID={self.config.native_source_id} not found in connection")
        return sources[0]

    def _extract_primary_table(self, payload: dict[str, Any]) -> dict[str, Any]:
        tables = payload.get("tables") or []
        if tables and isinstance(tables[0], dict):
            return tables[0]
        raw_table = payload.get("raw_table")
        if isinstance(raw_table, dict):
            return {
                "table_name": raw_table.get("table_name") or "FactDashboardRaw",
                "columns": raw_table.get("columns") or [],
                "rows": raw_table.get("rows") or [],
            }
        return {"table_name": "data", "columns": [], "rows": []}

    def _normalize_native_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = copy.deepcopy(payload)
        if not normalized.get("tables") and isinstance(normalized.get("raw_table"), dict):
            raw_table = normalized["raw_table"]
            normalized["tables"] = [
                {
                    "table_name": raw_table.get("table_name") or "FactDashboardRaw",
                    "columns": raw_table.get("columns") or [],
                    "rows": raw_table.get("rows") or [],
                }
            ]

        table = self._extract_primary_table(normalized)
        column_names = _column_names(table)
        column_set = set(column_names)
        long_fact = {"widget_id", "widget_title", "category", "value"}.issubset(column_set)
        charts = []
        for index, chart_raw in enumerate(normalized.get("charts") or []):
            if not isinstance(chart_raw, dict):
                continue
            chart = dict(chart_raw)
            raw_type = str(chart.get("chart_type") or chart.get("viz_type") or chart.get("type") or "bar").lower()
            chart["chart_type"] = (
                "line" if "line" in raw_type else
                "pie" if "pie" in raw_type or "donut" in raw_type or "doughnut" in raw_type else
                "table" if "table" in raw_type else
                "big_number" if "big" in raw_type or "number" in raw_type or "kpi" in raw_type else
                "bar"
            )
            chart.setdefault("id", chart.get("title") or chart.get("slice_name") or f"chart-{index + 1}")
            chart.setdefault("slice_name", chart.get("title") or chart.get("name") or f"Chart {index + 1}")
            if long_fact:
                chart["x_field"] = chart.get("x_field") or "category"
                if chart["chart_type"] != "table":
                    chart["y_field"] = chart.get("y_field") or "value"
                chart["series_field"] = chart.get("series_field") or "series"
                filter_value = str(chart.get("filter_value") or chart.get("id") or "")
                id_values = {
                    str(_row_value(row, column_names, "widget_id"))
                    for row in (table.get("rows") or [])[:200]
                }
                if not chart.get("filter_field") or (
                    chart.get("filter_field") == "widget_id" and filter_value not in id_values
                ):
                    chart["filter_field"] = "widget_id" if filter_value in id_values else "widget_title"
            charts.append(chart)
        normalized["charts"] = charts
        return normalized

    def _build_native_fields(self, source_meta: dict[str, Any], table: dict[str, Any]) -> list[dict[str, Any]]:
        columns = _column_names(table)
        rows = table.get("rows") or []
        raw_schema = source_meta.get("raw_schema") or []
        if not columns:
            columns = [str(col.get("title") or col.get("name") or "") for col in raw_schema if col.get("title") or col.get("name")]
        sample_by_name: dict[str, list[Any]] = {col: [] for col in columns}
        for row in rows[:50]:
            for index, col in enumerate(columns):
                if isinstance(row, dict):
                    sample_by_name[col].append(row.get(col))
                elif isinstance(row, (list, tuple)) and index < len(row):
                    sample_by_name[col].append(row[index])
        fields: list[dict[str, Any]] = []
        for index, col in enumerate(columns):
            raw = raw_schema[index] if index < len(raw_schema) else {}
            source = str(raw.get("name") or col)
            title = str(raw.get("title") or col)
            values = [value for value in sample_by_name.get(col, []) if value not in (None, "")]
            data_type = "integer" if values and all(_is_number(value) for value in values) else "string"
            fields.append(
                {
                    "title": title,
                    "source": source,
                    "guid": _field_guid(title),
                    "data_type": data_type,
                    "cast": data_type,
                    "type": "DIMENSION",
                    "initial_data_type": str(raw.get("user_type") or data_type),
                }
            )
        return fields

    def _create_native_dataset(
        self,
        *,
        workbook_id: str,
        connection_id: str,
        source_meta: dict[str, Any],
        fields: list[dict[str, Any]],
        dataset_name: str,
    ) -> dict[str, Any]:
        source_id = str(uuid.uuid4())
        avatar_id = str(uuid.uuid4())
        raw_schema = [
            {
                "user_type": field["initial_data_type"],
                "lock_aggregation": False,
                "title": field["title"],
                "has_auto_aggregation": False,
                "description": "",
                "name": field["source"],
                "native_type": {
                    "name": field["initial_data_type"],
                    "native_type_class_name": "generic_native_type",
                },
                "nullable": True,
            }
            for field in fields
        ]
        result_schema = [
            {
                "aggregation": "none",
                "title": field["title"],
                "virtual": False,
                "hidden": False,
                "lock_aggregation": False,
                "autoaggregated": False,
                "valid": True,
                "has_auto_aggregation": False,
                "description": "",
                "cast": field["cast"],
                "type": "DIMENSION",
                "initial_data_type": field["initial_data_type"],
                "data_type": field["data_type"],
                "managed_by": "user",
                "guid": field["guid"],
                "ui_settings": "",
                "aggregation_locked": False,
                "avatar_id": avatar_id,
                "source": field["source"],
                "calc_mode": "direct",
                "formula": "",
                "guid_formula": "",
                "default_value": None,
                "value_constraint": None,
            }
            for field in fields
        ]
        source_type = str(source_meta.get("source_type") or "GSHEETS_V2")
        dataset = {
            "sources": [
                {
                    "id": source_id,
                    "title": source_meta.get("title") or dataset_name,
                    "virtual": False,
                    "source_type": source_type,
                    "raw_schema": raw_schema,
                    "valid": True,
                    "connection_id": connection_id,
                    "parameter_hash": "",
                    "index_info_set": None,
                    "parameters": {"origin_source_id": source_meta.get("id")},
                    "managed_by": "user",
                }
            ],
            "result_schema": result_schema,
            "obligatory_filters": [],
            "result_schema_aux": {"inter_dependencies": {"deps": []}},
            "preview_enabled": True,
            "template_enabled": False,
            "revision_id": None,
            "source_avatars": [
                {
                    "id": avatar_id,
                    "source_id": source_id,
                    "title": source_meta.get("title") or dataset_name,
                    "virtual": False,
                    "valid": True,
                    "managed_by": "user",
                    "is_root": True,
                }
            ],
            "rls": {},
            "rls2": {},
            "data_export_forbidden": False,
            "avatar_relations": [],
            "load_preview_by_default": True,
            "component_errors": {"items": []},
            "description": "Published from Data Agent native integration",
            "cache_invalidation_source": {
                "sql": None,
                "cache_invalidation_error": None,
                "mode": "off",
                "filters": [],
                "field": None,
            },
            "query_settings": {},
            "extract": {
                "data_dataset_revision": None,
                "status": "disabled",
                "last_completed": 0,
                "errors": [],
                "valid": True,
                "sorting": [],
                "mode": "disabled",
                "filters": [],
            },
        }
        return self._rpc("createDataset", {"created_via": "user", "workbook_id": workbook_id, "name": dataset_name, "dataset": dataset})

    def _field_by_title(self, fields: list[dict[str, Any]], title: str) -> dict[str, Any] | None:
        normalized = str(title or "").strip().lower()
        for field in fields:
            if str(field.get("title") or "").strip().lower() == normalized:
                return field
            if str(field.get("source") or "").strip().lower() == normalized:
                return field
        return None

    def _native_chart_fields(
        self,
        chart: dict[str, Any],
        fields: list[dict[str, Any]],
        table: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        columns = _column_names(table)
        rows = table.get("rows") or []
        auto_x, auto_y = _auto_fields(columns, rows)
        x_name = str(chart.get("x_field") or auto_x or (fields[0]["title"] if fields else ""))
        y_name = str(chart.get("y_field") or auto_y or (fields[-1]["title"] if fields else ""))
        x_field = self._field_by_title(fields, x_name) or (fields[0] if fields else {})
        y_field = self._field_by_title(fields, y_name) or (fields[-1] if fields else x_field)
        return x_field, y_field

    def _native_wizard_field(
        self,
        field: dict[str, Any],
        *,
        dataset_id: str,
        dataset_name: str,
        field_type: str,
        item_id: str,
    ) -> dict[str, Any]:
        data_type = str(field.get("data_type") or "string")
        aggregation = "sum" if field_type == "MEASURE" else "none"
        return {
            "ui_settings": "",
            "title": field.get("title") or field.get("source"),
            "has_auto_aggregation": False,
            "virtual": False,
            "hidden": False,
            "data_type": data_type,
            "valid": True,
            "lock_aggregation": False,
            "guid": field.get("guid"),
            "type": field_type,
            "aggregation": aggregation,
            "cast": field.get("cast") or data_type,
            "managed_by": "user",
            "initial_data_type": field.get("initial_data_type") or data_type,
            "autoaggregated": False,
            "description": "",
            "aggregation_locked": False,
            "avatar_id": None,
            "source": field.get("source"),
            "calc_mode": "direct",
            "formula": "",
            "guid_formula": "",
            "datasetId": dataset_id,
            "datasetName": dataset_name,
            "id": item_id,
        }

    def _make_wizard_filter(
        self,
        filter_field: dict[str, Any],
        filter_value: str,
        dataset_id: str,
        dataset_name: str,
    ) -> dict[str, Any]:
        field_item = self._native_wizard_field(
            filter_field,
            dataset_id=dataset_id,
            dataset_name=dataset_name,
            field_type="DIMENSION",
            item_id="filter-widget-id",
        )
        return {
            "guid": filter_field.get("guid"),
            "datasetId": dataset_id,
            "disabled": False,
            "filter": {
                "operation": {"code": "EQ"},
                "value": {"type": "value", "value": filter_value},
            },
            "type": "DIMENSION",
            **field_item,
        }

    def _make_wizard_chart_data(
        self,
        *,
        chart: dict[str, Any],
        chart_type: str,
        dataset_id: str,
        dataset_name: str,
        fields: list[dict[str, Any]],
        table: dict[str, Any],
    ) -> dict[str, Any]:
        x_field, y_field = self._native_chart_fields(chart, fields, table)
        x_item = self._native_wizard_field(x_field, dataset_id=dataset_id, dataset_name=dataset_name, field_type="DIMENSION", item_id="dimension-1")
        y_item = self._native_wizard_field(y_field, dataset_id=dataset_id, dataset_name=dataset_name, field_type="MEASURE", item_id="measure-1")

        # Build widget_id filter so each chart only sees its own rows
        filters: list[dict[str, Any]] = []
        filter_field_name = str(chart.get("filter_field") or "widget_id")
        filter_value = str(chart.get("filter_value") or chart.get("id") or "")
        columns = _column_names(table)
        if filter_field_name == "widget_id" and columns and "widget_id" in columns:
            values = {str(_row_value(row, columns, "widget_id")) for row in (table.get("rows") or [])[:200]}
            if filter_value not in values and "widget_title" in columns:
                filter_field_name = "widget_title"
        if filter_value:
            wid_field = self._field_by_title(fields, filter_field_name)
            if wid_field:
                filters.append(self._make_wizard_filter(wid_field, filter_value, dataset_id, dataset_name))
        visualization_id = "line" if chart_type == "line" else "pie" if chart_type in {"pie", "donut"} else "column"
        placeholders = []
        if visualization_id == "pie":
            placeholders = [
                {"id": "colors", "type": "colors", "title": "section_colors", "items": [x_item], "settings": {}},
                {"id": "measures", "type": "measures", "title": "section_measures", "items": [y_item], "settings": {}},
            ]
        else:
            placeholders = [
                {
                    "id": "x",
                    "type": "x",
                    "title": "section_x",
                    "items": [x_item],
                    "settings": {"title": "off", "grid": "on", "axisVisibility": "show", "axisModeMap": {x_item["guid"]: "discrete"}},
                },
                {
                    "id": "y",
                    "type": "y",
                    "title": "section_y",
                    "items": [y_item],
                    "settings": {"title": "off", "grid": "on", "axisVisibility": "show", "axisModeMap": {y_item["guid"]: "continuous"}},
                },
            ]
        return {
            "colors": [],
            "colorsConfig": {},
            "datasetsIds": [dataset_id],
            "datasetsPartialFields": [[{"guid": field["guid"], "title": field["title"], "calc_mode": "direct"} for field in fields]],
            "extraSettings": {"title": "", "titleMode": "show", "labelsPosition": "outside"},
            "filters": filters,
            "geopointsConfig": {},
            "hierarchies": [],
            "labels": [],
            "links": [],
            "segments": [],
            "shapes": [],
            "shapesConfig": {},
            "sort": [],
            "tooltips": [],
            "type": "datalens",
            "updates": [],
            "version": "15",
            "visualization": {
                "id": visualization_id,
                "type": visualization_id,
                "name": f"label_visualization-{visualization_id}",
                "iconProps": {"id": f"vis{visualization_id.title()}", "width": "24"},
                "allowFilters": True,
                "allowColors": True,
                "allowSort": True,
                "allowSegments": True,
                "allowLabels": True,
                "placeholders": placeholders,
            },
            "convert": False,
        }

    def _create_native_wizard_charts(
        self,
        *,
        workbook_id: str,
        dataset_id: str,
        dataset_name: str,
        fields: list[dict[str, Any]],
        payload: dict[str, Any],
        name_suffix: str,
    ) -> dict[str, str]:
        table = self._extract_primary_table(payload)
        widget_id_map: dict[str, str] = {}
        seen_names: set[str] = set()
        for index, chart in enumerate(payload.get("charts") or []):
            local_id = str(chart.get("id") or index + 1)
            base_name = str(chart.get("slice_name") or chart.get("name") or chart.get("title") or f"Chart {index + 1}")
            name = f"{base_name} native {name_suffix}"
            if name in seen_names:
                name = f"{name} - {index + 1}"
            seen_names.add(name)
            raw_type = str(chart.get("chart_type") or chart.get("viz_type") or chart.get("type") or "bar").lower()
            chart_type = "line" if "line" in raw_type else "pie" if "pie" in raw_type or "donut" in raw_type else "bar"
            entry = {
                "template": "datalens",
                "workbookId": workbook_id,
                "name": name,
                "annotation": {"description": ""},
                "data": self._make_wizard_chart_data(
                    chart=chart,
                    chart_type=chart_type,
                    dataset_id=dataset_id,
                    dataset_name=dataset_name,
                    fields=fields,
                    table=table,
                ),
                "links": {"dataset": dataset_id},
            }
            result = self._rpc("createWizardChart", entry)
            widget_id_map[local_id] = _extract_entry_id(result)
        return widget_id_map

    def _create_native_dashboard(
        self,
        workbook_id: str,
        title: str,
        payload: dict[str, Any],
        widget_id_map: dict[str, str],
    ) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        layout: list[dict[str, Any]] = []
        for index, (local_id, chart_id) in enumerate(widget_id_map.items()):
            item_id = f"n{index + 1}"
            item_title = str(local_id)
            items.append(
                {
                    "id": item_id,
                    "type": "widget",
                    "namespace": "default",
                    "data": {
                        "hideTitle": True,
                        "tabs": [
                            {
                                "id": f"t{index + 1}",
                                "title": item_title,
                                "chartId": chart_id,
                                "isDefault": True,
                                "params": {},
                                "autoHeight": False,
                                "background": {"color": "transparent"},
                                "enableHint": False,
                                "hint": "",
                                "description": "",
                                "enableDescription": False,
                            }
                        ],
                    },
                }
            )
            layout.append({"i": item_id, "x": (index % 2) * 18, "y": (index // 2) * 12, "w": 18, "h": 12})
        dash = {
            "name": title,
            "annotation": {"description": ""},
            "meta": {},
            "data": {
                "salt": str(uuid.uuid4()),
                "schemeVersion": 8,
                "counter": len(items) + 1,
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
                "tabs": [{"id": "main", "title": title, "items": items, "layout": layout, "aliases": {}, "connections": []}],
            },
        }
        return self._rpc("createDashboard", {"mode": "publish", "entry": {"workbookId": workbook_id, **dash}})

    def _create_widgets(self, workbook_id: str, widgets: dict[str, Any]) -> dict[str, str]:
        widget_id_map: dict[str, str] = {}
        seen_names: set[str] = set()
        for local_id, wrapper in widgets.items():
            widget = (wrapper or {}).get("widget") or {}
            data = dict(widget.get("data") or {})
            data.setdefault("config", "module.exports = {};\n")
            base_name = str(widget.get("name") or f"Widget {local_id}")
            unique_name = base_name
            if unique_name in seen_names:
                unique_name = f"{base_name} - {local_id}"
            seen_names.add(unique_name)
            entry = {
                "workbookId": workbook_id,
                "name": unique_name,
                "type": widget.get("type") or "advanced-chart_node",
                "annotation": widget.get("annotation") or {"description": ""},
                "meta": {},
                "links": {},
                "data": data,
            }
            result = self._rpc("createEditorChart", {"mode": "publish", "entry": entry})
            widget_id_map[str(local_id)] = _extract_entry_id(result)
        return widget_id_map

    def _create_dashboard(
        self,
        workbook_id: str,
        dash_wrapper: dict[str, Any],
        widget_id_map: dict[str, str],
    ) -> dict[str, Any]:
        dash = copy.deepcopy((dash_wrapper or {}).get("dash") or {})
        dash_data = dash.get("data") or {}
        for tab in dash_data.get("tabs") or []:
            for item in tab.get("items") or []:
                for item_tab in ((item.get("data") or {}).get("tabs") or []):
                    local_chart_id = str(item_tab.get("chartId") or "")
                    if local_chart_id in widget_id_map:
                        item_tab["chartId"] = widget_id_map[local_chart_id]

        entry = {
            "workbookId": workbook_id,
            "name": dash.get("name") or "Data Agent Dashboard",
            "annotation": dash.get("annotation") or {"description": ""},
            "meta": dash.get("meta") or {},
            "data": dash_data,
        }
        return self._rpc("createDashboard", {"mode": "publish", "entry": entry})


def publish_to_datalens(payload: dict[str, Any], settings: Settings) -> dict[str, Any]:
    client = DataLensClient(DataLensPublishConfig.from_settings(settings))
    try:
        return client.publish_dashboard(payload)
    finally:
        client.close()


def publish_to_datalens_native(payload: dict[str, Any], settings: Settings) -> dict[str, Any]:
    client = DataLensClient(DataLensPublishConfig.from_settings(settings))
    try:
        return client.publish_native_dashboard(payload)
    finally:
        client.close()
