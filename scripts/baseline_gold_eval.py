"""Baseline evaluation pipeline for gold_dash images → Navigator import.

Runs 5 (or any set of) images through the full pipeline:
  1. POST /api/image/analyze         → analysis.json
  2. POST /api/export/dashboard/{navigator|xml}  → export_payload.json + navigator.xml
  3. POST /api/export/dashboard/navigator/import → import into Navigator DB
  4. Query Navigator DB for actual widget counts
  5. Screenshot Navigator dashboard URL
  6. LLM visual score (GigaChat-2-Max)
  7. Write per-case artifacts + summary CSV / JSON

Metrics:
  analysis_widgets     – widgets reported by analysis summary
  xml_widgets          – widgets in generated XML (t19/r count)
  navigator_widgets    – widgets actually in ui.tscreenwidget_v30
  widget_coverage      – navigator_widgets / xml_widgets
  unit_coverage        – KPI units present in Navigator xparams / expected
  data_render_coverage – stub: 1 - (empty_widgets / navigator_widgets)
  type_match_coverage  – widget type match after import
  visual_score         – LLM 1-10
  critical_issues      – list of serious losses

Usage:
    cd /home/user-tot/Desktop/data_agent
    .venv/bin/python scripts/baseline_gold_eval.py
    .venv/bin/python scripts/baseline_gold_eval.py --images 5.png 12.png 24.png
"""
from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import re
import shutil
import sys
import textwrap
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx
from openai import OpenAI
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.config import get_settings
from backend.builders.triplex_export import _TriplexExportBuilder
from backend.services.dashboard_vision import DashboardVisionService
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
    _run_json_sql,
)

BASE_URL = "http://127.0.0.1:8000"
IMAGES_DIR = Path("/home/user-tot/Desktop/  голд")
OUTPUT_DIR = ROOT / "eval_results" / "baseline_gold"

# Authenticated HTTP client (initialized in _get_client)
_HTTP_CLIENT: httpx.Client | None = None


def _get_client() -> httpx.Client:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is not None:
        return _HTTP_CLIENT
    email = os.environ.get("EVAL_EMAIL", "eval@local.dev")
    password = os.environ.get("EVAL_PASSWORD", "eval_password_2026")
    client = httpx.Client(timeout=300)
    # Try login first; register if user doesn't exist yet
    resp = client.post(f"{BASE_URL}/api/auth/login", json={"email": email, "password": password})
    if resp.status_code == 401:
        reg = client.post(f"{BASE_URL}/api/auth/register", json={"email": email, "password": password})
        reg.raise_for_status()
    elif resp.status_code != 200:
        resp.raise_for_status()
    _HTTP_CLIENT = client
    return client

UNIT_ATTR_RE = re.compile(r's(?:Unit|Postfix|Suffix)="([^"]+)"')

BASELINE_IMAGES = [
    "Screenshot from 2026-03-10 11-58-45.png",
    "Screenshot from 2026-03-10 11-58-54.png",
    "Screenshot from 2026-03-10 11-59-02.png",
    "Screenshot from 2026-03-10 11-59-08.png",
    "Screenshot from 2026-03-10 11-59-16.png",
    "Screenshot from 2026-03-10 11-59-23.png",
    "Screenshot from 2026-03-10 11-59-28.png",
    "Screenshot from 2026-03-10 11-59-33.png",
    "Screenshot from 2026-03-10 11-59-40.png",
    "Screenshot from 2026-03-10 11-59-46.png",
    "Screenshot from 2026-03-10 11-59-53.png",
    "Screenshot from 2026-03-10 11-59-57.png",
    "Screenshot from 2026-03-10 12-00-02.png",
    "Screenshot from 2026-03-10 12-00-26.png",
]


@dataclass
class CaseResult:
    case_id: str
    image: str
    # Detection
    analysis_widgets: int = 0
    analysis_charts: int = 0
    analysis_kpis: int = 0
    # XML
    xml_widgets: int = 0
    xml_bytes: int = 0
    # Navigator import
    navigator_widgets: int = 0
    widget_coverage: float = 0.0
    type_match_coverage: float = 0.0
    unit_coverage: float = 0.0
    data_render_coverage: float = 0.0
    # Textual details
    missing_widgets: str = ""
    type_mismatches: str = ""
    missing_units: str = ""
    # Navigator metadata
    navigator_url: str = ""
    dashboard_id: str = ""
    screen_id: str = ""
    # LLM judge
    visual_score: int = 0
    data_quality: str = ""
    kpi_fill_rate: str = ""
    llm_verdict: str = ""
    # Critical issues list (serialized as "; " joined string)
    critical_issues: str = ""
    # Artifacts
    screenshot: str = ""
    error: str = ""
    ok: bool = False


def _navigator_config() -> NavigatorImportConfig:
    s = get_settings()
    return NavigatorImportConfig(
        base_url=s.navigator_base_url,
        db_host=s.navigator_db_host,
        db_port=s.navigator_db_port,
        db_name=s.navigator_db_name,
        db_user=s.navigator_db_user,
        db_password=s.navigator_db_password,
        access_login=s.navigator_access_login,
    )


def _analyze_image(image_path: Path) -> dict[str, Any]:
    client = _get_client()
    with open(image_path, "rb") as f:
        suffix = image_path.suffix.lower()
        mime = "image/jpeg" if suffix in (".jpg", ".jpeg") else "image/png"
        resp = client.post(
            f"{BASE_URL}/api/image/analyze",
            files={"file": (image_path.name, f, mime)},
        )
    resp.raise_for_status()
    return resp.json()


def _build_export_payload(analysis: dict, title: str) -> dict[str, Any]:
    spec = analysis.get("spec") or {}
    vitrina = analysis.get("vitrina") or {}
    charts = spec.get("charts") or []
    kpis = spec.get("kpis") or []

    fact_rows = vitrina.get("FactDashboard") or []
    # Build title→widget_id mapping from vitrina so filter_value matches raw_table integers
    title_to_widget_id: dict[str, int] = {}
    title_to_widget_type: dict[str, str] = {}
    for row in fact_rows:
        wid = row.get("widget_id")
        wt = str(row.get("widget_title") or "").strip()
        if wid is not None and wt and wt not in title_to_widget_id:
            title_to_widget_id[wt] = int(wid)
        if wt and wt not in title_to_widget_type:
            # Prefer original_chart_type over widget_type (which is the frontend render type)
            ct = row.get("original_chart_type") or row.get("widget_type") or ""
            if ct:
                title_to_widget_type[wt] = str(ct).lower()

    # Grid constants: Navigator canvas is 12 columns wide.
    # Rows are in the same unit as widget heights (one unit ≈ 60px).
    # A full dashboard is roughly 20 row-units tall.
    GRID_COLS = 12
    GRID_ROWS = 20  # total rows in a typical dashboard

    def _pos_to_layout(name: str, pos: dict | None) -> dict | None:
        """Convert Vision 0-1 relative position to Navigator grid coords."""
        if not isinstance(pos, dict):
            return None
        left = pos.get("left", 0)
        top = pos.get("top", 0)
        width = pos.get("width", 0.5)
        height = pos.get("height", 0.2)
        col = round(left * GRID_COLS)
        row = round(top * GRID_ROWS)
        w = max(2, round(width * GRID_COLS))
        raw_h = round(height * GRID_ROWS)
        # Enforce minimum: small widgets (filters, dropdowns) stay at 2,
        # content widgets (charts, KPIs) need at least 3 rows.
        h = max(2, raw_h) if raw_h <= 2 else max(3, raw_h)
        return {"slice_name": name, "col": col, "row": row, "width": w, "height": h}

    def _safe_table_dataset(rows_raw: list[dict], columns_raw: list[str]) -> tuple[list[str], list[dict]]:
        seen: dict[str, int] = {}
        rename: dict[str, str] = {}
        safe_cols: list[str] = []
        for col in columns_raw:
            safe = re.sub(r"[^\w]+", "_", str(col), flags=re.UNICODE).strip("_") or "col"
            if safe[0].isdigit():
                safe = f"col_{safe}"
            if safe in seen:
                seen[safe] += 1
                safe = f"{safe}_{seen[safe]}"
            else:
                seen[safe] = 0
            rename[str(col)] = safe
            safe_cols.append(safe)
        safe_rows = [{rename.get(str(k), str(k)): v for k, v in row.items()} for row in rows_raw]
        return safe_cols, safe_rows

    def _is_placeholder_selector(ch: dict[str, Any], title: str, chart_type: str, has_real_table_rows: bool) -> bool:
        """Skip selector/tab noise that Vision returned as tiny synthetic tables."""
        if chart_type == "filter":
            return True
        if chart_type not in {"table", "pivot_table"} or has_real_table_rows:
            return False
        raw_type = str(ch.get("type") or ch.get("chart_type") or "").strip().lower()
        if raw_type == "filter":
            return True
        if raw_type:
            return False
        series = ch.get("series") or []
        if not (
            isinstance(series, list)
            and len(series) == 1
            and isinstance(series[0], dict)
            and str(series[0].get("name") or "").strip().lower() in {"значение", "value"}
        ):
            return False
        values = series[0].get("data") or series[0].get("values") or []
        if not isinstance(values, list) or len(values) > 3:
            return False
        pos = ch.get("position") or {}
        small = isinstance(pos, dict) and (
            float(pos.get("height") or 0) <= 0.12 or float(pos.get("width") or 0) <= 0.25
        )
        title_l = title.lower()
        selector_title = title_l in {
            "дата", "бренд", "авто", "регион", "регион сравнения",
            "все товары и услуги", "непродовольственные товары",
        }
        return small or selector_title

    def _title_key(value: Any) -> str:
        return re.sub(r"[^a-z0-9а-яё]+", "", str(value or "").lower())

    def _pos_iou(left: Any, right: Any) -> float:
        if not isinstance(left, dict) or not isinstance(right, dict):
            return 0.0
        try:
            lx = float(left.get("left", 0.0) or 0.0)
            ly = float(left.get("top", 0.0) or 0.0)
            lw = float(left.get("width", 0.0) or 0.0)
            lh = float(left.get("height", 0.0) or 0.0)
            rx = float(right.get("left", 0.0) or 0.0)
            ry = float(right.get("top", 0.0) or 0.0)
            rw = float(right.get("width", 0.0) or 0.0)
            rh = float(right.get("height", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0
        if lw <= 0 or lh <= 0 or rw <= 0 or rh <= 0:
            return 0.0
        inter_w = max(0.0, min(lx + lw, rx + rw) - max(lx, rx))
        inter_h = max(0.0, min(ly + lh, ry + rh) - max(ly, ry))
        intersection = inter_w * inter_h
        union = (lw * lh) + (rw * rh) - intersection
        return intersection / union if union > 0 else 0.0

    def _is_duplicate_kpi_visual(ch: dict[str, Any], title: str, chart_type: str) -> bool:
        if chart_type not in {"big_number", "big_number_total", "kpi", "gauge"}:
            return False
        title_key = _title_key(title)
        if not title_key:
            return False
        chart_pos = ch.get("position")
        for kpi in kpis:
            if not isinstance(kpi, dict):
                continue
            kpi_title = str(kpi.get("name") or kpi.get("metric_name") or "").strip()
            kpi_key = _title_key(kpi_title)
            if not kpi_key:
                continue
            same_title = title_key == kpi_key or title_key in kpi_key or kpi_key in title_key
            if not same_title:
                continue
            if _pos_iou(chart_pos, kpi.get("position")) >= 0.35:
                return True
            if kpi.get("value") is not None:
                return True
        return False

    def _title_tokens(value: Any) -> set[str]:
        return {
            token
            for token in re.findall(r"[0-9a-zа-яё]+", str(value or "").lower())
            if len(token) > 2 and token not in {"шт", "тыс", "млн", "руб", "проц", "value"}
        }

    def _has_related_chart_for_kpi(kpi_name: str) -> bool:
        kpi_tokens = _title_tokens(kpi_name)
        if not kpi_tokens:
            return False
        for ch in charts:
            if not isinstance(ch, dict):
                continue
            chart_type = str(ch.get("type") or ch.get("chart_type") or "").lower()
            if chart_type not in {"line", "area", "bar", "bar_horizontal", "combo"}:
                continue
            title = str(ch.get("title") or ch.get("name") or "").strip()
            title_l = title.lower()
            if "динамик" not in title_l and "trend" not in title_l and "измен" not in title_l:
                continue
            chart_tokens = _title_tokens(title)
            if len(kpi_tokens & chart_tokens) >= max(1, min(2, len(kpi_tokens))):
                return True
        return False

    chart_list = []
    layout = []
    extra_tables: list[dict[str, Any]] = []
    kpi_names = {
        str(k.get("name") or k.get("metric_name") or "").strip()
        for k in kpis
        if isinstance(k, dict)
    }
    _chart_type_labels = {
        "treemap": "Treemap", "funnel": "Воронка", "sankey": "Санки",
        "sunburst": "Sunburst", "radar": "Радар", "gauge": "Gauge",
        "scatter": "Scatter", "gantt": "Gantt", "country_map": "Карта",
        "candlestick": "Candlestick",
    }
    for idx, ch in enumerate(charts, start=1):
        t = str(ch.get("title") or ch.get("name") or "").strip()
        if not t:
            t = _chart_type_labels.get(str(ch.get("chart_type") or ch.get("type") or "").lower()) or f"Widget {idx}"
        # Use integer widget_id matching vitrina rows; fall back to sequential index
        widget_id = title_to_widget_id.get(t, idx)
        chart_type = title_to_widget_type.get(t) or str(ch.get("type") or ch.get("chart_type") or "bar").lower()
        dataset_name = "FactDashboardRaw"
        filter_field = "widget_id"
        filter_value = str(widget_id)
        rows_raw = ch.get("rows")
        has_real_table_rows = isinstance(rows_raw, list) and rows_raw and all(isinstance(r, dict) for r in rows_raw)
        if t in kpi_names and chart_type in {"table", "pivot_table"} and not has_real_table_rows:
            continue
        if _is_placeholder_selector(ch, t, chart_type, has_real_table_rows):
            continue
        if _is_duplicate_kpi_visual(ch, t, chart_type):
            continue
        if chart_type in {"table", "pivot_table"} and has_real_table_rows:
            dataset_name = f"TableWidget_{widget_id}"
            filter_field = ""
            filter_value = ""
            table_cols = list(ch.get("categories") or [])
            if not table_cols:
                seen_cols: list[str] = []
                for row in rows_raw:
                    for key in row.keys():
                        if key not in seen_cols:
                            seen_cols.append(key)
                table_cols = seen_cols
            safe_cols, safe_rows = _safe_table_dataset(rows_raw, table_cols)
            extra_tables.append({"table_name": dataset_name, "columns": safe_cols, "rows": safe_rows[:500]})
        chart_list.append({
            "id": str(widget_id),
            "slice_name": t,
            "title": t,
            "type": chart_type,
            "chart_type": chart_type,
            "viz_type": chart_type,
            "dataset": dataset_name,
            "table_name": dataset_name,
            "x_field": "category",
            "y_field": "value",
            "filter_field": filter_field,
            "filter_value": filter_value,
            "metric_fields": ["value"],
        })
        entry = _pos_to_layout(t, ch.get("position"))
        if entry:
            layout.append(entry)

    # Use vitrina FactKPIs when available — it contains sparkline_json, breakdown_json, etc.
    vitrina_kpis_by_name = {
        str(k.get("metric_name") or "").strip(): k
        for k in (vitrina.get("FactKPIs") or [])
        if isinstance(k, dict)
    }
    kpi_rows = []
    for kpi in kpis:
        kpi_name = str(kpi.get("name") or kpi.get("metric_name") or "").strip()
        vk = vitrina_kpis_by_name.get(kpi_name, {})
        row: dict[str, Any] = {
            "metric_name": kpi_name,
            "value": vk.get("value") or kpi.get("value") or 0,
            "unit": vk.get("unit") or kpi.get("unit") or "",
            "delta": kpi.get("delta") or None,
        }
        if vk.get("sparkline_json"):
            if not _has_related_chart_for_kpi(kpi_name):
                row["sparkline_json"] = vk["sparkline_json"]
        if vk.get("sparkline_type"):
            if row.get("sparkline_json"):
                row["sparkline_type"] = vk["sparkline_type"]
        if vk.get("breakdown_json"):
            row["breakdown_json"] = vk["breakdown_json"]
        kpi_rows.append(row)
        # KPI widgets get layout entries using the metric_name as slice_name
        entry = _pos_to_layout(kpi_name, kpi.get("position"))
        if entry:
            layout.append(entry)

    grid_layout = DashboardVisionService._build_grid_layout(spec)
    if not grid_layout:
        grid_layout = spec.get("grid_layout") if isinstance(spec.get("grid_layout"), list) else []
    if grid_layout:
        allowed_layout_names = {
            str(chart.get("slice_name") or "").strip()
            for chart in chart_list
            if isinstance(chart, dict)
        } | {
            str(row.get("metric_name") or "").strip()
            for row in kpi_rows
            if isinstance(row, dict)
        }
        normalized_grid_layout: list[dict[str, Any]] = []
        for entry in grid_layout:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("slice_name") or "").strip()
            if name.startswith("KPI:"):
                name = name.split(":", 1)[1].strip()
            if name not in allowed_layout_names:
                continue
            normalized = dict(entry)
            normalized["slice_name"] = name
            normalized_grid_layout.append(normalized)
        if normalized_grid_layout:
            layout = normalized_grid_layout

    # Union of all row keys — needed when different widget types add extra columns (e.g. candlestick open/high/low/close)
    seen_cols: dict = {}
    for _r in fact_rows:
        if isinstance(_r, dict):
            for _k in _r.keys():
                seen_cols.setdefault(_k, None)
    raw_cols = list(seen_cols.keys())

    return {
        "dashboard_title": title,
        "title": title,
        "subject_area_name": title,
        "slug": "data_agent_eval",
        "navigator_single_raw_source": True,
        "charts": chart_list,
        "kpi_rows": kpi_rows,
        "tables": [{"table_name": "FactDashboardRaw", "columns": raw_cols, "rows": fact_rows[:500]}, *extra_tables],
        "layout": layout if layout else [],
    }


def _xml_widgets(xml_bytes: bytes) -> list[dict[str, str]]:
    root = ET.fromstring(xml_bytes)
    result = []
    for row in root.findall("./data/t19/r"):
        result.append({
            "name": str(row.get("sname_ru") or ""),
            "type_id": str(row.get("nwidgettypeid") or ""),
            "xparams": str(row.get("xparams") or ""),
        })
    return result


def _kpi_units(payload: dict) -> dict[str, str]:
    result: dict[str, str] = {}
    for row in payload.get("kpi_rows") or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("metric_name") or row.get("title") or "").strip()
        unit = str(row.get("unit") or "").strip()
        if name and unit:
            result[name] = unit
    return result


def _query_screen_widgets(screen_id: int, config: NavigatorImportConfig) -> list[dict]:
    sql = f"""
SELECT COALESCE(json_agg(row_to_json(w) ORDER BY norder, nid), '[]'::json)::text
FROM (
    SELECT nid, norder, sname_ru, nwidgettypeid, xparams::text AS xparams
    FROM ui.tscreenwidget_v30
    WHERE nscreenid = {int(screen_id)}
) w;
"""
    result = _run_json_sql(sql, config, "Navigator widget lookup failed")
    return result if isinstance(result, list) else []


def _unit_misses(expected_units: dict[str, str], db_widgets: list[dict]) -> list[str]:
    by_name: dict[str, list[str]] = {}
    for row in db_widgets:
        by_name.setdefault(str(row.get("sname_ru") or ""), []).append(str(row.get("xparams") or ""))
    misses = []
    for name, unit in expected_units.items():
        xparams_list = by_name.get(name)
        if not xparams_list:
            misses.append(f"{name}:missing_widget")
        elif not any(unit in set(UNIT_ATTR_RE.findall(xparams)) for xparams in xparams_list):
            misses.append(f"{name}:{unit}")
    return misses


def _coverage(expected: set[str], actual: set[str]) -> float:
    if not expected:
        return 1.0
    return round(len(expected & actual) / len(expected), 4)


def _screenshot_navigator(url: str, output_path: Path, username: str = "admin", password: str = "admin") -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1920, "height": 1080},
                ignore_https_errors=True,
            )
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            # Login if the page shows a login form
            page.wait_for_timeout(2000)
            body_text = page.locator("body").inner_text(timeout=5000)
            if "Логин" in body_text or "Пароль" in body_text or "войти" in body_text.lower():
                page.mouse.click(960, 490)
                page.keyboard.press("Control+A")
                page.keyboard.type(username)
                page.mouse.click(960, 560)
                page.keyboard.press("Control+A")
                page.keyboard.type(password)
                page.mouse.click(960, 670)
                page.wait_for_timeout(5000)
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(6000)
            page.screenshot(path=str(output_path), full_page=True)
            browser.close()
        return True
    except Exception as e:
        print(f"    [screenshot_navigator] ERROR: {e}")
        return False


def _img_b64(path: Path) -> str:
    img = Image.open(path)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGB")
    # Downscale if very large to avoid API payload limits
    max_side = 2048
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side), Image.LANCZOS)
    import io
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    data = buf.getvalue()
    return f"data:image/jpeg;base64,{base64.b64encode(data).decode()}"


FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://127.0.0.1:3001")


def _screenshot_agent(image_path: Path, output_path: Path, timeout_ms: int = 180_000) -> bool:
    """Screenshot the AI agent Canvas after uploading image_path.

    Flow:
      1. Open frontend, find image file input, upload the file
      2. Wait for analysis SSE to finish — button "ПОСТРОИТЬ ДАШБОРД" becomes enabled
      3. Click it → app navigates to DashboardPage / Canvas
      4. Wait for Canvas to render (no spinner, widgets visible)
      5. Screenshot
    """
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1920, "height": 1080})
            page.goto(FRONTEND_URL, wait_until="networkidle", timeout=30_000)

            # Find any file input and upload the image
            file_input = page.locator("input[type='file']").first
            file_input.set_input_files(str(image_path))

            # Step 1: Wait for analysis to complete.
            # The button "ПОСТРОИТЬ ДАШБОРД" is rendered only after imageState='done'.
            # Analysis takes 30–90 s via SSE stream.
            build_btn = page.get_by_role("button", name=re.compile(r"ПОСТРОИТЬ\s+ДАШБОРД", re.IGNORECASE))
            build_btn.wait_for(state="visible", timeout=timeout_ms)
            # Also wait until it's enabled (not disabled while rawVisionData is loading)
            page.wait_for_function(
                """() => {
                    const btns = Array.from(document.querySelectorAll("button"));
                    return btns.some(b =>
                        /построить.*дашборд/i.test(b.textContent || "") && !b.disabled
                    );
                }""",
                timeout=timeout_ms,
            )

            # Step 2: Click → navigate to Canvas/DashboardPage
            build_btn.click()

            # Step 3: Wait for Canvas to render.
            # Canvas is ready when there are widget tiles or the "Опубликовать" button.
            page.wait_for_function(
                """() => {
                    const body = document.body.innerText || "";
                    // Still analyzing — not ready
                    if (body.includes("Анализируем") || body.includes("Строим")) return false;
                    // Canvas rendered: publish button or widget cards present
                    return body.includes("Опубликовать") || body.includes("Navigator")
                        || document.querySelectorAll("[class*='widget'], [class*='Widget'], [class*='card'], [class*='Card']").length > 2;
                }""",
                timeout=timeout_ms,
            )
            page.wait_for_timeout(3000)

            # Step 4: Clean up UI chrome for screenshot
            page.evaluate(
                """() => {
                    for (const el of Array.from(document.querySelectorAll("*"))) {
                        const rect = el.getBoundingClientRect();
                        const text = el.textContent || "";
                        // Hide left sidebar
                        if (rect.left < 85 && rect.height > 500 && rect.width < 95)
                            el.style.setProperty("display", "none", "important");
                        // Hide AI assistant panel on the right
                        if (text.includes("ИИ-ассистент") && rect.left > window.innerWidth * 0.55 && rect.height > 250)
                            el.style.setProperty("display", "none", "important");
                    }
                    document.documentElement.style.overflow = "hidden";
                    document.body.style.overflow = "hidden";
                    window.scrollTo(0, 0);
                }"""
            )
            page.wait_for_timeout(800)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(output_path), full_page=False)
            browser.close()
        return True
    except Exception as e:
        print(f"    [screenshot_agent] ERROR: {e}")
        return False


def _make_comparison(
    original: Path,
    agent_screenshot: Path | None,
    navigator_screenshot: Path | None,
    output: Path,
    score: int | None,
    verdict: str = "",
) -> None:
    """Three-panel comparison: ORIGINAL | AGENT | NAVIGATOR."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("    [comparison] PIL not available, skipping")
        return

    def _fit(img: "Image.Image", size: tuple[int, int], bg: tuple) -> "Image.Image":
        img = img.convert("RGB")
        img.thumbnail(size, Image.LANCZOS)
        canvas = Image.new("RGB", size, bg)
        x = (size[0] - img.width) // 2
        y = (size[1] - img.height) // 2
        canvas.paste(img, (x, y))
        return canvas

    panels: list[tuple[Path | None, str]] = [
        (original, "ОРИГИНАЛ"),
        (agent_screenshot, "АГЕНТ"),
        (navigator_screenshot, "NAVIGATOR"),
    ]
    panel_w, panel_h = 800, 580
    header_h = 52
    gutter = 14
    bg = (18, 22, 33)
    label_bg = (32, 40, 64)
    label_absent = (60, 30, 30)
    text_col = (245, 247, 250)
    muted_col = (178, 186, 204)
    n = len(panels)
    total_w = panel_w * n + gutter * (n + 1)
    total_h = panel_h + header_h + gutter * 2 + 36
    canvas = Image.new("RGB", (total_w, total_h), bg)
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 22)
        small = ImageFont.truetype("DejaVuSans.ttf", 15)
    except Exception:
        font = ImageFont.load_default()
        small = font

    for i, (path, label) in enumerate(panels):
        x = gutter + i * (panel_w + gutter)
        present = path is not None and path.exists()
        hdr_fill = label_bg if present else label_absent
        draw.rounded_rectangle((x, gutter, x + panel_w, header_h), radius=8, fill=hdr_fill)
        draw.text((x + 14, gutter + 12), label, font=font, fill=text_col)
        if i == 2 and score is not None:
            draw.text((x + panel_w - 180, gutter + 14), f"score {score}/10", font=small, fill=muted_col)
        img_y = header_h + gutter
        if present:
            try:
                img = Image.open(path)
                canvas.paste(_fit(img, (panel_w, panel_h), bg), (x, img_y))
            except Exception:
                pass

    if verdict:
        wrapped = textwrap.wrap(verdict, width=160)[:2]
        for li, line in enumerate(wrapped):
            draw.text((gutter, total_h - 30 + li * 18), line, font=small, fill=muted_col)

    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output)


_JUDGE_MODEL_OVERRIDE: str | None = None


def _llm_judge(
    original: Path,
    navigator_screenshot: Path | None,
    spec: dict,
) -> dict[str, Any]:
    s = get_settings()
    judge_model = _JUDGE_MODEL_OVERRIDE or s.cloudru_vision_judge_model
    client = OpenAI(base_url=s.cloudru_base_url, api_key=s.cloudru_api_key, timeout=s.cloudru_vision_judge_timeout)

    charts = [ch.get("title") or ch.get("name") or "" for ch in (spec.get("charts") or [])]
    kpis = [k.get("name") or k.get("metric_name") or "" for k in (spec.get("kpis") or [])]

    kpis_with_vals = [
        k for k in (spec.get("kpis") or [])
        if k.get("value") is not None
    ]
    kpis_no_val = [
        k for k in (spec.get("kpis") or [])
        if k.get("value") is None
    ]

    n_charts = len(charts)
    n_kpis_total = len(kpis_with_vals) + len(kpis_no_val)

    content: list[dict] = [
        {
            "type": "text",
            "text": (
                "Сравни два изображения: оригинальный дашборд и его импорт в Navigator.\n"
                "Отвечай только по тому, что видно на картинках.\n\n"
                f"Ожидается в Navigator: {n_charts} графиков, {n_kpis_total} KPI-блоков.\n"
                f"Названия графиков: {', '.join(charts) or 'нет'}\n"
                f"Названия KPI: {', '.join(k.get('name','') for k in kpis_with_vals + kpis_no_val) or 'нет'}\n\n"
                "Изображение 1 — оригинал:\n"
            ),
        },
        {"type": "image_url", "image_url": {"url": _img_b64(original)}},
    ]

    if navigator_screenshot and navigator_screenshot.exists():
        content.append({"type": "text", "text": "Изображение 2 — Navigator после импорта:\n"})
        content.append({"type": "image_url", "image_url": {"url": _img_b64(navigator_screenshot)}})

    content.append({
        "type": "text",
        "text": (
            "\nПосчитай и ответь строго в JSON (без markdown):\n"
            "{\n"
            f'  "charts_with_real_data": <целое число от 0 до {n_charts}: сколько графиков в Navigator показывают реальные данные, совпадающие с оригиналом — не случайные синтетические столбики и не пустые виджеты>,\n'
            f'  "kpis_with_real_value": <целое число от 0 до {n_kpis_total}: сколько KPI в Navigator показывают реальное ненулевое число из оригинала>,\n'
            '  "data_quality": <"real" если все данные реальные, "mixed" если часть, "synthetic" если всё синтетика, "empty" если всё пусто>,\n'
            '  "lost": [<список названий виджетов где данные пустые, синтетические или отсутствуют>],\n'
            '  "issues": [<список конкретных проблем, максимум 5>],\n'
            '  "verdict": <одно предложение — общий вывод>\n'
            "}\n"
            f"Важно: score НЕ нужен. Только факты по картинкам."
        ),
    })

    try:
        resp = client.chat.completions.create(
            model=judge_model,
            messages=[{"role": "user", "content": content}],
            max_tokens=1024,
            timeout=s.cloudru_vision_judge_timeout,
        )
        raw = (resp.choices[0].message.content or "").strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]
        result = json.loads(raw)
        # Compute score from factual counts: 50% charts, 50% KPIs
        charts_ok = int(result.get("charts_with_real_data") or 0)
        kpis_ok   = int(result.get("kpis_with_real_value") or 0)
        chart_ratio = charts_ok / n_charts if n_charts else 1.0
        kpi_ratio   = kpis_ok / n_kpis_total if n_kpis_total else 1.0
        score = round((chart_ratio * 0.6 + kpi_ratio * 0.4) * 10)
        result["score"] = score
        result.setdefault("preserved", [])
        result.setdefault("lost", result.get("lost") or [])
        return result
    except Exception as e:
        return {"score": 0, "preserved": [], "lost": [], "issues": [str(e)], "verdict": f"LLM failed: {e}"}


def run_case(image_path: Path, case_id: str, out_dir: Path, config: NavigatorImportConfig) -> CaseResult:
    case_dir = out_dir / case_id
    case_dir.mkdir(parents=True, exist_ok=True)

    r = CaseResult(case_id=case_id, image=image_path.name)
    title = f"BASELINE_{case_id}_{image_path.stem}"

    # Copy original image so results folder is self-contained for visual comparison
    orig_ext = image_path.suffix.lower()
    orig_copy = case_dir / f"original{orig_ext}"
    shutil.copy2(image_path, orig_copy)

    print(f"\n{'='*60}")
    print(f"  [{case_id}] {image_path.name}")
    print(f"{'='*60}")

    # ── Step 1: analyze ──────────────────────────────────────────
    print("  [1/5] Analyzing image...")
    try:
        analysis = _analyze_image(image_path)
        (case_dir / "analysis.json").write_text(json.dumps(analysis, ensure_ascii=False, indent=2))
        summary = analysis.get("summary") or {}
        r.analysis_widgets = summary.get("widgets", 0)
        r.analysis_charts = summary.get("charts_detected", 0)
        r.analysis_kpis = summary.get("kpis_detected", 0)
        print(f"     widgets={r.analysis_widgets}  charts={r.analysis_charts}  kpis={r.analysis_kpis}")
    except Exception as e:
        r.error = f"analyze failed: {e}"
        print(f"  ERROR: {e}")
        return r

    # ── Step 2: build XML ────────────────────────────────────────
    print("  [2/5] Building Navigator XML...")
    try:
        payload = _build_export_payload(analysis, title)
        (case_dir / "export_payload.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2))

        builder = _TriplexExportBuilder(payload)
        xml_bytes = builder.build_xml()
        (case_dir / "navigator.xml").write_bytes(xml_bytes)

        expected_widgets = _xml_widgets(xml_bytes)
        r.xml_widgets = len(expected_widgets)
        r.xml_bytes = len(xml_bytes)
        expected_by_name = {w["name"]: w for w in expected_widgets}
        expected_names = set(expected_by_name)
        expected_units = _kpi_units(payload)
        print(f"     xml_widgets={r.xml_widgets}  xml_bytes={r.xml_bytes}")
    except Exception as e:
        r.error = f"xml build failed: {e}"
        print(f"  ERROR: {e}")
        return r

    # ── Step 3: Navigator import ─────────────────────────────────
    print("  [3/5] Importing to Navigator...")
    import_result: dict = {}
    try:
        import_result = import_xml_to_navigator(xml_bytes, config)
        (case_dir / "import_result.json").write_text(json.dumps(import_result, ensure_ascii=False, indent=2))

        actual_sa = resolve_imported_subject_area(builder.subject_area_id, builder.subject_area_name, config)
        sa_id = int(actual_sa["nid"])
        ensure_subject_area_access(sa_id, config)
        grant_subject_area_source_access(sa_id, config)

        dash_info = resolve_imported_dashboard(sa_id, config)
        actual_dash = dash_info.get("dashboard") or {}
        dash_id = int(actual_dash.get("nid") or dash_info.get("linked_dashboard_id"))
        r.dashboard_id = str(dash_id)

        screen = query_dashboard_screen(dash_id, config)
        screen_id = int(screen.get("screen_id"))
        r.screen_id = str(screen_id)

        r.navigator_url = build_dashboard_url(dash_id, screen_id, config)
        print(f"     dashboard_id={dash_id}  screen_id={screen_id}")
        print(f"     url={r.navigator_url}")

        db_widgets = _query_screen_widgets(screen_id, config)
        (case_dir / "db_widgets.json").write_text(json.dumps(db_widgets, ensure_ascii=False, indent=2))

        actual_by_name = {str(w.get("sname_ru") or ""): w for w in db_widgets}
        actual_names = set(actual_by_name)
        r.navigator_widgets = len(db_widgets)

        # widget_coverage
        missing = sorted(expected_names - actual_names)
        r.missing_widgets = "; ".join(missing)
        r.widget_coverage = _coverage(expected_names, actual_names)

        # type_match_coverage
        type_mismatches = []
        for name, exp in expected_by_name.items():
            act = actual_by_name.get(name)
            if not act:
                continue
            if str(exp.get("type_id") or "") != str(act.get("nwidgettypeid") or ""):
                type_mismatches.append(f"{name}:{exp['type_id']}->{act['nwidgettypeid']}")
        r.type_mismatches = "; ".join(type_mismatches)
        matched_count = max(0, len(expected_names & actual_names) - len(type_mismatches))
        r.type_match_coverage = round(matched_count / len(expected_names), 4) if expected_names else 1.0

        # unit_coverage
        unit_misses = _unit_misses(expected_units, db_widgets)
        r.missing_units = "; ".join(unit_misses)
        r.unit_coverage = (
            round((len(expected_units) - len(unit_misses)) / len(expected_units), 4)
            if expected_units else 1.0
        )

        # data_render_coverage: fraction of db_widgets without empty/null xparams
        non_empty = sum(1 for w in db_widgets if str(w.get("xparams") or "").strip() not in ("", "{}"))
        r.data_render_coverage = round(non_empty / len(db_widgets), 4) if db_widgets else 0.0

        print(
            f"     navigator_widgets={r.navigator_widgets}"
            f"  widget_cov={r.widget_coverage}"
            f"  type_cov={r.type_match_coverage}"
            f"  unit_cov={r.unit_coverage}"
            f"  render_cov={r.data_render_coverage}"
        )
        if missing:
            print(f"     missing: {missing[:5]}")
        if type_mismatches:
            print(f"     type_mismatches: {type_mismatches[:3]}")

    except Exception as e:
        r.error = f"navigator import failed: {e}"
        print(f"  ERROR: {e}")
        # Still continue to screenshot / LLM with what we have

    # ── Step 4a: Screenshot Agent canvas ────────────────────────
    agent_ss: Path | None = None
    print("  [4a/6] Screenshotting Agent canvas...")
    agent_ss_path = case_dir / "agent_screenshot.png"
    if _screenshot_agent(image_path, agent_ss_path):
        agent_ss = agent_ss_path
        print(f"     saved: {agent_ss.name}")
    else:
        print("     failed (non-fatal)")

    # ── Step 4b: Screenshot Navigator ───────────────────────────
    nav_ss: Path | None = None
    if r.navigator_url:
        print("  [4b/6] Screenshotting Navigator dashboard...")
        nav_ss = case_dir / "navigator_screenshot.png"
        ok = _screenshot_navigator(r.navigator_url, nav_ss)
        if ok:
            r.screenshot = str(nav_ss)
            print(f"     saved: {nav_ss.name}")
        else:
            nav_ss = None
            print("     failed (non-fatal)")
    else:
        print("  [4b/6] Skipping Navigator screenshot (no URL)")

    # ── Step 5: LLM visual judge ─────────────────────────────────
    print("  [5/6] LLM visual judge...")
    spec = analysis.get("spec") or {}
    judgment = _llm_judge(image_path, nav_ss, spec)
    (case_dir / "judgment.json").write_text(json.dumps(judgment, ensure_ascii=False, indent=2))

    r.visual_score = int(judgment.get("score") or 0)
    r.data_quality = str(judgment.get("data_quality") or "")
    r.kpi_fill_rate = str(judgment.get("kpi_fill_rate") or "")
    r.llm_verdict = str(judgment.get("verdict") or "")
    lost = judgment.get("lost") or []
    issues = judgment.get("issues") or []

    # Build critical_issues: combine missing widgets + unit misses + LLM lost
    critical: list[str] = []
    if missing_widgets_list := [m for m in r.missing_widgets.split("; ") if m]:
        critical.append(f"missing widgets: {', '.join(missing_widgets_list[:3])}")
    if missing_units_list := [m for m in r.missing_units.split("; ") if m]:
        critical.append(f"missing units: {', '.join(missing_units_list[:3])}")
    if lost:
        critical.append(f"LLM lost: {', '.join(lost[:3])}")
    if issues:
        critical.extend(issues[:2])
    r.critical_issues = "; ".join(critical)

    print(f"     score={r.visual_score}/10  verdict={r.llm_verdict[:100]}")
    if lost:
        print(f"     lost: {lost[:3]}")

    # ── Step 6: Three-panel comparison image ─────────────────────
    print("  [6/6] Building comparison image...")
    orig_ext = image_path.suffix.lower()
    orig_copy = case_dir / f"original{orig_ext}"
    _make_comparison(
        original=orig_copy,
        agent_screenshot=agent_ss,
        navigator_screenshot=nav_ss,
        output=case_dir / "comparison.png",
        score=r.visual_score,
        verdict=r.llm_verdict,
    )
    print(f"     saved: comparison.png")

    r.ok = (
        r.widget_coverage >= 0.9
        and r.type_match_coverage >= 0.9
        and r.unit_coverage >= 0.9
        and not r.error
    )
    return r


def write_report(results: list[CaseResult], out_dir: Path) -> None:
    report_json = out_dir / "baseline_report.json"
    report_csv = out_dir / "baseline_report.csv"

    report_json.write_text(
        json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2), encoding="utf-8"
    )

    fieldnames = [
        "case_id", "image",
        "analysis_widgets", "xml_widgets", "navigator_widgets",
        "widget_coverage", "type_match_coverage", "unit_coverage", "data_render_coverage",
        "visual_score", "data_quality", "kpi_fill_rate", "critical_issues",
        "missing_widgets", "type_mismatches", "missing_units",
        "navigator_url", "screenshot", "llm_verdict", "error", "ok",
    ]
    with open(report_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in results:
            d = asdict(r)
            w.writerow({k: d.get(k, "") for k in fieldnames})

    print(f"\n{'='*60}")
    print("BASELINE SUMMARY")
    print(f"{'='*60}")
    print(f"{'case':<6} {'image':<38} {'a_w':>4} {'x_w':>4} {'n_w':>4} {'w_cov':>6} {'u_cov':>6} {'score':>6} {'data_q':<10}  issues")
    print("-" * 110)
    for r in results:
        mark = "✓" if r.ok else "✗"
        print(
            f"{mark} {r.case_id:<5} {r.image:<38}"
            f" {r.analysis_widgets:>4} {r.xml_widgets:>4} {r.navigator_widgets:>4}"
            f" {r.widget_coverage:>6.2f} {r.unit_coverage:>6.2f} {r.visual_score:>5}/10"
            f" {r.data_quality:<10}  {r.critical_issues[:50]}"
        )

    avg_score = [r.visual_score for r in results if r.visual_score > 0]
    if avg_score:
        print(f"\nAvg visual_score: {sum(avg_score)/len(avg_score):.1f}/10")
    avg_wcov = [r.widget_coverage for r in results if r.xml_widgets > 0]
    if avg_wcov:
        print(f"Avg widget_coverage: {sum(avg_wcov)/len(avg_wcov):.3f}")

    print(f"\nJSON: {report_json}")
    print(f"CSV:  {report_csv}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Baseline gold_dash evaluation")
    parser.add_argument("--images", nargs="*", help="Image filenames (relative to IMAGES_DIR)")
    parser.add_argument("--images-dir", default=str(IMAGES_DIR))
    parser.add_argument("--output", default=str(OUTPUT_DIR))
    parser.add_argument("--start-index", type=int, default=1, help="Start case numbering from this index (e.g. 18 → C18, C19, ...)")
    parser.add_argument("--judge-model", default=None, help="Override vision judge model (e.g. openai/gpt-5.4-nano)")
    parser.add_argument("--vision-model", default=None, help="Override vision analysis model, routes via Cloud.ru (e.g. openai/gpt-5.4-nano)")
    args = parser.parse_args()

    if args.judge_model:
        import scripts.baseline_gold_eval as _self
        _self._JUDGE_MODEL_OVERRIDE = args.judge_model

    if args.vision_model:
        import backend.services.cloudru_client as _cc
        _cc._VISION_MODEL_OVERRIDE = args.vision_model

    images_dir = Path(args.images_dir)
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    image_names = args.images or BASELINE_IMAGES
    image_paths = [images_dir / name for name in image_names]

    missing = [p for p in image_paths if not p.exists()]
    if missing:
        print(f"ERROR: images not found: {[str(p) for p in missing]}")
        return 1

    print(f"Baseline eval: {len(image_paths)} images → {out_dir}")
    config = _navigator_config()

    results = []
    for i, img in enumerate(image_paths, args.start_index):
        case_id = f"C{i:02d}"
        try:
            r = run_case(img, case_id, out_dir, config)
        except Exception as e:
            r = CaseResult(case_id=case_id, image=img.name, error=str(e))
            print(f"  FATAL ERROR: {e}")
        results.append(r)

    write_report(results, out_dir)
    failed = [r for r in results if not r.ok]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
