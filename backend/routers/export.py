"""Dashboard export endpoints for external BI systems."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from backend.config import get_settings
from backend.builders.datalens_export import DataLensExportBuilder
from backend.builders.foresight_export import ForesightExportBuilder
from backend.builders.triplex_export import _TriplexExportBuilder
from backend.foresight_service import (
    check_connection,
    clone_dashboard,
    clone_template_dashboard,
    publish_mvp_dashboard,
    publish_permanent_dashboard,
    publish_dashboard,
)
from backend.services.visiology_client import publish_to_visiology
from backend.services.datalens_client import publish_to_datalens, publish_to_datalens_native
from backend.services.navigator_import import (
    NavigatorImportConfig,
    build_dashboard_url,
    ensure_subject_area_access,
    grant_subject_area_source_access,
    import_xml_to_navigator,
    query_dashboard_screen,
    query_import_state,
    resolve_imported_dashboard,
    resolve_imported_subject_area,
)
from backend.services.auth import get_current_user

router = APIRouter(prefix="/export", tags=["export"], dependencies=[Depends(get_current_user)])
logger = logging.getLogger("data_agent.export")


def _navigator_import_config() -> NavigatorImportConfig:
    settings = get_settings()
    return NavigatorImportConfig(
        base_url=settings.navigator_base_url,
        db_host=settings.navigator_db_host,
        db_port=settings.navigator_db_port,
        db_name=settings.navigator_db_name,
        db_user=settings.navigator_db_user,
        db_password=settings.navigator_db_password,
        access_login=settings.navigator_access_login,
    )


def _slug(value: Any, fallback: str = "dashboard") -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9а-яё_-]+", "_", text, flags=re.IGNORECASE)
    text = re.sub(r"_+", "_", text).strip("_")
    return text or fallback


def _json_response(payload: Dict[str, Any], filename: str) -> Response:
    return Response(
        json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _build_superset_bundle(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Emit a portable Superset-oriented dashboard bundle.

    The original ai-platform project does not have a separate Superset exporter.
    This keeps the contract explicit: dashboard metadata, chart specs, layout,
    inline tables, and KPI rows are all preserved for downstream import tooling.
    """
    return {
        "version": "1.0",
        "type": "superset_dashboard_bundle",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dashboard": {
            "id": payload.get("dashboard_id"),
            "title": payload.get("dashboard_title") or payload.get("title") or "Dashboard",
            "slug": payload.get("slug") or "dashboard",
        },
        "charts": payload.get("charts") or [],
        "layout": payload.get("layout") or [],
        "tables": payload.get("tables") or [],
        "kpi_rows": payload.get("kpi_rows") or [],
        "chart_meta": payload.get("chart_meta") or {},
    }


@router.post("/dashboard/{target}")
def export_dashboard(target: str, payload: Dict[str, Any]):
    charts = payload.get("charts") or []
    tables = payload.get("tables") or []
    kpi_rows = payload.get("kpi_rows") or []
    if not charts and not tables and not kpi_rows:
        raise HTTPException(status_code=400, detail="Нет данных для экспорта")

    slug = _slug(payload.get("slug") or payload.get("dashboard_id") or payload.get("dashboard_title"))

    try:
        if target == "navigator":
            xml_bytes = _TriplexExportBuilder(payload).build_xml()
            return Response(
                xml_bytes,
                media_type="application/xml",
                headers={"Content-Disposition": f'attachment; filename="{slug}_navigator.xml"'},
            )

        if target == "datalens":
            result = DataLensExportBuilder(payload).build()
            return _json_response(result, f"{slug}_datalens.json")

        if target == "superset":
            result = _build_superset_bundle(payload)
            return _json_response(result, f"{slug}_superset.json")

        if target == "foresight":
            result = ForesightExportBuilder(payload).build()
            return _json_response(result, f"{slug}_foresight_bundle.json")
    except Exception as exc:
        logger.exception("Dashboard export failed target=%s", target)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    raise HTTPException(status_code=404, detail=f"Unsupported export target: {target}")


@router.post("/dashboard/visiology/publish")
def publish_dashboard_to_visiology(payload: Dict[str, Any]):
    charts = payload.get("charts") or []
    tables = payload.get("tables") or []
    kpi_rows = payload.get("kpi_rows") or []
    if not charts and not tables and not kpi_rows:
        raise HTTPException(status_code=400, detail="Нет данных для публикации")

    try:
        return publish_to_visiology(payload, get_settings())
    except Exception as exc:
        logger.exception("Visiology publish failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/dashboard/datalens/publish")
def publish_dashboard_to_datalens(payload: Dict[str, Any]):
    charts = payload.get("charts") or []
    tables = payload.get("tables") or []
    kpi_rows = payload.get("kpi_rows") or []
    if not charts and not tables and not kpi_rows:
        raise HTTPException(status_code=400, detail="Нет данных для публикации")

    try:
        return publish_to_datalens(payload, get_settings())
    except Exception as exc:
        logger.exception("DataLens publish failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/datalens/status")
def datalens_status():
    settings = get_settings()
    return {
        "api_base_url": settings.datalens_api_base_url,
        "public_base_url": settings.datalens_public_base_url,
        "org_id_configured": bool(settings.datalens_org_id),
        "cloud_id_configured": bool(settings.datalens_cloud_id),
        "iam_token_configured": bool(settings.datalens_iam_token),
        "oauth_token_configured": bool(settings.datalens_oauth_token),
        "collection_id": settings.datalens_collection_id or None,
        "native_connection_configured": bool(settings.datalens_native_connection_id),
        "native_connection_workbook_configured": bool(settings.datalens_native_connection_workbook_id),
        "supported_publish": ["workbook", "advanced_editor_charts", "dashboard", "native_dataset_wizard"],
        "ready": bool(
            (settings.datalens_org_id or settings.datalens_cloud_id)
            and (settings.datalens_iam_token or settings.datalens_oauth_token)
        ),
    }


@router.post("/dashboard/datalens/publish-native")
def publish_dashboard_to_datalens_native(payload: Dict[str, Any]):
    charts = payload.get("charts") or []
    tables = payload.get("tables") or []
    if not charts and not tables:
        raise HTTPException(status_code=400, detail="Нет данных для публикации")

    try:
        return publish_to_datalens_native(payload, get_settings())
    except Exception as exc:
        logger.exception("DataLens native publish failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/foresight/status")
def foresight_status():
    settings = get_settings()
    return {
        "base_url": settings.foresight_base_url,
        "ssh": {
            "host": settings.foresight_ssh_host,
            "port": settings.foresight_ssh_port,
            "user": settings.foresight_ssh_user,
            "password_configured": bool(settings.foresight_ssh_password),
        },
        "repository": {
            "id": settings.foresight_repo_id,
            "name": settings.foresight_repo_name,
            "db_server": settings.foresight_db_server,
            "db_name": settings.foresight_db_name,
            "login_configured": bool(settings.foresight_repo_login),
            "password_configured": bool(settings.foresight_repo_password),
        },
        "supported_exports": ["foresight_compiler_bundle", "foresight_native_clone", "foresight_template_clone"],
        "native_pefx_import_ready": bool(
            settings.foresight_ssh_password
            and settings.foresight_repo_login
            and settings.foresight_repo_password
        ),
    }


@router.get("/foresight/check")
def foresight_check():
    try:
        return check_connection()
    except Exception as exc:
        logger.exception("Foresight connection check failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/foresight/clone-demo")
def foresight_clone_demo(payload: Dict[str, Any] | None = None):
    payload = payload or {}
    try:
        return clone_dashboard(
            source_id=str(payload.get("source_id") or "IP_COVID19_RUS"),
            new_id=str(payload.get("new_id") or "DA_DASHBOARD"),
            new_name=str(payload.get("new_name") or "Data Agent: Аналитический дашборд"),
            parent_id=str(payload.get("parent_id") or "F_EXAMPLES"),
            export_copy=bool(payload.get("export_copy", True)),
        )
    except Exception as exc:
        logger.exception("Foresight dashboard clone failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/foresight/clone-template")
def foresight_clone_template(payload: Dict[str, Any] | None = None):
    payload = payload or {}
    try:
        return clone_template_dashboard(payload)
    except Exception as exc:
        logger.exception("Foresight template dashboard clone failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/foresight/publish-mvp")
def foresight_publish_mvp(payload: Dict[str, Any] | None = None):
    payload = payload or {}
    try:
        result = publish_mvp_dashboard(payload)
        screenshot = result.pop("screenshot_bytes", b"")
        result["screenshot_size"] = len(screenshot)
        return result
    except Exception as exc:
        logger.exception("Foresight MVP publish failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/foresight/publish")
def foresight_publish(payload: Dict[str, Any] | None = None):
    """Publish a Foresight dashboard from a real Data Agent payload (dynamic widget types and layout)."""
    payload = payload or {}
    try:
        # If real charts[] present — use dynamic publish; else fall back to permanent demo
        if payload.get("charts"):
            result = publish_dashboard(payload)
        else:
            result = publish_permanent_dashboard(payload)
        result.pop("screenshot_bytes", None)
        result.pop("save_response", None)
        return result
    except Exception as exc:
        logger.exception("Foresight publish failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/dashboard/navigator/import")
def import_dashboard_to_navigator(payload: Dict[str, Any]):
    charts = payload.get("charts") or []
    tables = payload.get("tables") or []
    kpi_rows = payload.get("kpi_rows") or []
    if not charts and not tables and not kpi_rows:
        raise HTTPException(status_code=400, detail="Нет данных для импорта")

    try:
        builder = _TriplexExportBuilder(payload)
        xml_bytes = builder.build_xml()
        config = _navigator_import_config()
        import_result = import_xml_to_navigator(xml_bytes, config)
        actual_subject_area = resolve_imported_subject_area(
            builder.subject_area_id,
            builder.subject_area_name,
            config,
        )
        actual_subject_area_id = int(actual_subject_area["nid"])
        access = ensure_subject_area_access(actual_subject_area_id, config)
        source_access = grant_subject_area_source_access(actual_subject_area_id, config)
        dashboard_info = resolve_imported_dashboard(actual_subject_area_id, config)
        actual_dashboard = dashboard_info.get("dashboard") or {}
        actual_dashboard_id = int(
            actual_dashboard.get("nid")
            if isinstance(actual_dashboard, dict) and actual_dashboard.get("nid") is not None
            else dashboard_info.get("linked_dashboard_id")
        )
        validation = query_import_state(actual_subject_area_id, actual_dashboard_id, config)
        screen = query_dashboard_screen(actual_dashboard_id, config)
        screen_id = screen.get("screen_id") if isinstance(screen, dict) else None
        dashboard_url = build_dashboard_url(actual_dashboard_id, screen_id, config)
        return {
            "subject_area_id": actual_subject_area_id,
            "dashboard_id": actual_dashboard_id,
            "requested_subject_area_id": builder.subject_area_id,
            "requested_dashboard_id": builder.dashboard_id,
            "subject_area": actual_subject_area,
            "dashboard": actual_dashboard,
            "screen": screen,
            "dashboard_url": dashboard_url,
            "xml_bytes": len(xml_bytes),
            "import_result": import_result,
            "access": access,
            "source_access": source_access,
            "validation": validation,
        }
    except FileNotFoundError as exc:
        logger.exception("Navigator import failed: psql is unavailable")
        raise HTTPException(status_code=500, detail="psql is not installed or not available in PATH") from exc
    except Exception as exc:
        logger.exception("Navigator import failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
