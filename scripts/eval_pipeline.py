#!/usr/bin/env python3
"""
Evaluation pipeline for dashboard reconstruction quality.

For each test image:
1. POST to /api/image/analyze  → spec + vitrina
2. Screenshot the rendered dashboard in the browser (localhost:3001)
3. POST to /api/export/dashboard/datalens/publish  → DataLens URL
4. Optionally screenshot DataLens page
5. Use GigaChat-2-Max vision LLM to score similarity 1-10 and identify lost charts
6. Output JSON + CSV report

Usage:
    .venv/bin/python scripts/eval_pipeline.py
    .venv/bin/python scripts/eval_pipeline.py --input /path/to/images --output /path/to/results
"""
from __future__ import annotations

import base64
import csv
import json
import os
import re
import sys
import time
from urllib.parse import urlparse
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from dotenv import load_dotenv
from openai import OpenAI
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from backend.services.dashboard_vision import DashboardVisionService

BASE_URL = "http://127.0.0.1:8000"
FRONTEND_URL = "http://localhost:3001"
TESTS_DIR = Path("/home/user-tot/Desktop/pannels/TESTS")
OUTPUT_DIR = ROOT / "eval_results"

VISION_MODEL = os.environ.get("GIGACHAT_VISION_MODEL", "GigaChat-2-Max")
VISION_BASE_URL = os.environ.get("GPT2GIGA_URL", "http://localhost:8090/v1")
VISION_API_KEY = os.environ.get("GPT2GIGA_API_KEY", "dummy-local-key")


@dataclass
class EvalResult:
    image_name: str
    charts_detected: int = 0
    kpis_detected: int = 0
    widgets_total: int = 0
    agent_screenshot: str = ""
    datalens_url: str = ""
    datalens_screenshot: str = ""
    datalens_publish_ok: bool = False
    similarity_score: int = 0
    lost_charts: List[str] = field(default_factory=list)
    preserved_charts: List[str] = field(default_factory=list)
    issues: List[str] = field(default_factory=list)
    llm_verdict: str = ""
    error: str = ""


def image_to_b64(path: Path) -> str:
    with open(path, "rb") as f:
        data = f.read()
    ext = path.suffix.lower().lstrip(".")
    mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def screenshot_frontend(output_path: Path, image_path: Path, wait_ms: int = 8000) -> bool:
    """Upload image_path via file input, wait for dashboard to render, screenshot."""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1600, "height": 900})
            page.goto(FRONTEND_URL, wait_until="networkidle", timeout=15000)

            # Upload via hidden file input (set_input_files works on hidden inputs).
            # Prefer image-capable inputs; the app can also render dataset inputs.
            file_input = page.locator("input[type='file'][accept*='.png'], input[type='file'][accept*='image']").first
            if file_input.count() == 0:
                file_input = page.locator("input[type='file']").first
            file_input.set_input_files(str(image_path))

            # Wait for analysis to complete. It can take several minutes when
            # the Qwen normalization call reaches its external timeout.
            page.wait_for_timeout(2000)  # let upload register

            # Click "ПОСТРОИТЬ ДАШБОРД" button if present
            btn = page.get_by_role("button", name=re.compile("ПОСТРОИТЬ ДАШБОРД", re.IGNORECASE)).first
            btn.wait_for(state="visible", timeout=240000)
            btn.click()

            # Wait for the dashboard page, not just for "analysis finished".
            page.get_by_text("Опубликовать в DataLens").first.wait_for(timeout=60000)
            page.wait_for_function(
                """() => {
                    const text = document.body.innerText || "";
                    const stillAnalyzing = text.includes("Анализируем макет");
                    const hasDashboardExport = text.includes("Опубликовать в DataLens");
                    const widgetCards = Array.from(document.querySelectorAll("*")).filter((el) => {
                        const style = window.getComputedStyle(el);
                        const radius = parseFloat(style.borderRadius || "0");
                        const rect = el.getBoundingClientRect();
                        return radius >= 6 && rect.width > 220 && rect.height > 120;
                    }).length;
                    return hasDashboardExport && widgetCards >= 2 && !stillAnalyzing;
                }""",
                timeout=60000,
            )

            page.wait_for_timeout(3000)  # settle animations

            body_text = page.locator("body").inner_text(timeout=5000)
            chart_count = page.locator(".recharts-surface, .recharts-wrapper").count()
            if "Анализируем макет" in body_text:
                raise RuntimeError("frontend is still on analysis screen; refusing to save non-dashboard screenshot")
            if "Опубликовать в DataLens" not in body_text:
                raise RuntimeError(f"dashboard screen was not detected before screenshot (charts={chart_count})")

            page.set_viewport_size({"width": 2400, "height": 1800})
            page.evaluate(
                """() => {
                    const hideIf = (predicate) => {
                        for (const el of Array.from(document.querySelectorAll("*"))) {
                            const rect = el.getBoundingClientRect();
                            if (predicate(el, rect)) {
                                el.style.setProperty("display", "none", "important");
                            }
                        }
                    };
                    hideIf((el, rect) =>
                        (el.textContent || "").includes("ИИ-ассистент") &&
                        rect.width > 250 &&
                        rect.width < 650 &&
                        rect.height > 300 &&
                        rect.left > window.innerWidth * 0.55
                    );
                    hideIf((el, rect) =>
                        rect.left < 80 &&
                        rect.height > 500 &&
                        rect.width < 90
                    );
                    document.documentElement.style.overflow = "hidden";
                    document.body.style.overflow = "hidden";
                    window.scrollTo(0, 0);
                }"""
            )
            page.wait_for_timeout(1000)
            page.screenshot(path=str(output_path), full_page=False)
            browser.close()
        return True
    except Exception as e:
        print(f"  [screenshot_frontend] ERROR: {e}")
        return False


def screenshot_url(url: str, output_path: Path, wait_ms: int = 5000) -> bool:
    """Screenshot an arbitrary URL (e.g. DataLens)."""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1600, "height": 900})
            page.goto(url, wait_until="networkidle", timeout=20000)
            page.wait_for_timeout(wait_ms)
            page.screenshot(path=str(output_path), full_page=False)
            browser.close()
        return True
    except Exception as e:
        print(f"  [screenshot_url] ERROR: {e}")
        return False


def analyze_image(image_path: Path) -> Dict[str, Any]:
    """POST image to /api/image/analyze, return full response."""
    with open(image_path, "rb") as f:
        resp = httpx.post(
            f"{BASE_URL}/api/image/analyze",
            files={"file": (image_path.name, f, "image/jpeg" if image_path.suffix.lower() in (".jpg", ".jpeg") else "image/png")},
            timeout=180,
        )
    resp.raise_for_status()
    return resp.json()


def build_export_payload(analysis: Dict[str, Any], image_name: str) -> Dict[str, Any]:
    """Build export payload from analysis result for Navigator publish."""
    spec = analysis.get("spec") or {}
    vitrina = analysis.get("vitrina") or {}

    charts = spec.get("charts") or []
    kpis = spec.get("kpis") or []

    fact_rows = vitrina.get("FactDashboard") or []
    # Build title→widget_id mapping from vitrina so filter_value matches raw_table integers
    title_to_widget_id: Dict[str, int] = {}
    title_to_widget_type: Dict[str, str] = {}
    for row in fact_rows:
        wid = row.get("widget_id")
        wt = str(row.get("widget_title") or "").strip()
        if wid is not None and wt and wt not in title_to_widget_id:
            title_to_widget_id[wt] = int(wid)
        if wt and row.get("widget_type") and wt not in title_to_widget_type:
            title_to_widget_type[wt] = str(row.get("widget_type") or "").lower()

    def _safe_table_dataset(rows_raw: List[Dict[str, Any]], columns_raw: List[str]) -> tuple[List[str], List[Dict[str, Any]]]:
        seen: Dict[str, int] = {}
        rename: Dict[str, str] = {}
        safe_cols: List[str] = []
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

    def _is_placeholder_selector(ch: Dict[str, Any], title: str, chart_type: str, has_real_table_rows: bool) -> bool:
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

    def _is_duplicate_kpi_visual(ch: Dict[str, Any], title: str, chart_type: str) -> bool:
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

    chart_list = []
    extra_tables: List[Dict[str, Any]] = []
    kpi_names = {
        str(k.get("name") or k.get("metric_name") or "").strip()
        for k in kpis
        if isinstance(k, dict)
    }
    for idx, ch in enumerate(charts, start=1):
        title = str(ch.get("title") or ch.get("name") or "").strip()
        if not title:
            continue
        widget_id = title_to_widget_id.get(title, idx)
        chart_type = title_to_widget_type.get(title) or str(ch.get("type") or ch.get("chart_type") or "bar").lower()
        dataset_name = "FactDashboardRaw"
        filter_field = "widget_id"
        filter_value = str(widget_id)
        rows_raw = ch.get("rows")
        has_real_table_rows = isinstance(rows_raw, list) and rows_raw and all(isinstance(r, dict) for r in rows_raw)
        if title in kpi_names and chart_type in {"table", "pivot_table"} and not has_real_table_rows:
            continue
        if _is_placeholder_selector(ch, title, chart_type, has_real_table_rows):
            continue
        if _is_duplicate_kpi_visual(ch, title, chart_type):
            continue
        if chart_type in {"table", "pivot_table"} and has_real_table_rows:
            dataset_name = f"TableWidget_{widget_id}"
            filter_field = ""
            filter_value = ""
            table_cols = list(ch.get("categories") or [])
            if not table_cols:
                seen_cols: List[str] = []
                for row in rows_raw:
                    for key in row.keys():
                        if key not in seen_cols:
                            seen_cols.append(key)
                table_cols = seen_cols
            safe_cols, safe_rows = _safe_table_dataset(rows_raw, table_cols)
            extra_tables.append({"table_name": dataset_name, "columns": safe_cols, "rows": safe_rows[:500]})
        chart_list.append({
            "id": str(widget_id),
            "slice_name": title,
            "title": title,
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

    kpi_rows = []
    for kpi in kpis:
        kpi_rows.append({
            "metric_name": kpi.get("name") or kpi.get("metric_name") or "",
            "value": kpi.get("value") or 0,
            "unit": kpi.get("unit") or "",
            "delta": kpi.get("delta") or None,
        })

    layout = []
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
            layout.append(normalized)

    raw_cols = list(fact_rows[0].keys()) if fact_rows else []

    return {
        "dashboard_title": f"Eval: {image_name}",
        "title": f"Eval: {image_name}",
        "subject_area_name": f"Eval: {image_name}",
        "slug": "data_agent_eval",
        "navigator_single_raw_source": True,
        "charts": chart_list,
        "kpi_rows": kpi_rows,
        "tables": [{"table_name": "FactDashboardRaw", "columns": raw_cols, "rows": fact_rows[:500]}, *extra_tables],
        "layout": layout,
    }


def publish_to_datalens(payload: Dict[str, Any]) -> Dict[str, Any]:
    """POST to /api/export/dashboard/datalens/publish."""
    try:
        resp = httpx.post(
            f"{BASE_URL}/api/export/dashboard/datalens/publish",
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        return {"error": str(e)}


def _is_http_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def llm_judge(
    original_path: Path,
    agent_screenshot_path: Optional[Path],
    datalens_screenshot_path: Optional[Path],
    spec: Dict[str, Any],
) -> Dict[str, Any]:
    """Use GigaChat-2-Max to score the dashboard reconstruction quality."""
    client = OpenAI(base_url=VISION_BASE_URL, api_key=VISION_API_KEY)

    charts_in_spec = [
        ch.get("title") or ch.get("name") or ""
        for ch in (spec.get("charts") or [])
    ]
    kpis_in_spec = [
        k.get("name") or k.get("metric_name") or ""
        for k in (spec.get("kpis") or [])
    ]

    content = [
        {
            "type": "text",
            "text": (
                "Ты эксперт по BI-дашбордам. Оцени качество воспроизведения дашборда.\n\n"
                f"Обнаруженные виджеты (из оригинала):\n"
                f"- Графики: {', '.join(charts_in_spec) or 'нет'}\n"
                f"- KPI: {', '.join(kpis_in_spec) or 'нет'}\n\n"
                "Изображения:\n"
                "1) Оригинальный дашборд (фотография)\n"
            ),
        },
        {
            "type": "image_url",
            "image_url": {"url": image_to_b64(original_path)},
        },
    ]

    if agent_screenshot_path and agent_screenshot_path.exists():
        content.append({
            "type": "text",
            "text": "2) Дашборд в агенте данных (браузер, до публикации в DataLens):\n",
        })
        content.append({
            "type": "image_url",
            "image_url": {"url": image_to_b64(agent_screenshot_path)},
        })

    if datalens_screenshot_path and datalens_screenshot_path.exists():
        content.append({
            "type": "text",
            "text": "3) Результат в Yandex DataLens после публикации:\n",
        })
        content.append({
            "type": "image_url",
            "image_url": {"url": image_to_b64(datalens_screenshot_path)},
        })

    content.append({
        "type": "text",
        "text": (
            "\nОтветь СТРОГО в формате JSON (без markdown-блоков):\n"
            "{\n"
            '  "score": <число 1-10>,\n'
            '  "preserved": ["список сохранённых виджетов/графиков"],\n'
            '  "lost": ["список потерянных/искажённых виджетов"],\n'
            '  "issues": ["краткое описание проблем (не более 5)"],\n'
            '  "verdict": "краткое резюме (1-2 предложения)"\n'
            "}"
        ),
    })

    try:
        response = client.chat.completions.create(
            model=VISION_MODEL,
            messages=[{"role": "user", "content": content}],
            max_tokens=1024,
            timeout=90,
        )
        raw = response.choices[0].message.content or ""
        # Strip markdown code fences if present
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1]
            raw = raw.rsplit("```", 1)[0]
        return json.loads(raw)
    except Exception as e:
        return {
            "score": 0,
            "preserved": [],
            "lost": [],
            "issues": [str(e)],
            "verdict": f"LLM judge failed: {e}",
        }


def run_eval(image_path: Path, output_dir: Path) -> EvalResult:
    name = image_path.stem
    case_dir = output_dir / name
    case_dir.mkdir(parents=True, exist_ok=True)

    result = EvalResult(image_name=image_path.name)
    print(f"\n{'='*60}")
    print(f"  {image_path.name}")
    print(f"{'='*60}")

    # Step 1: analyze
    print("  [1/5] Analysing image via /api/image/analyze ...")
    try:
        analysis = analyze_image(image_path)
        (case_dir / "analysis.json").write_text(json.dumps(analysis, ensure_ascii=False, indent=2))
        summary = analysis.get("summary") or {}
        result.charts_detected = summary.get("charts_detected", 0)
        result.kpis_detected = summary.get("kpis_detected", 0)
        result.widgets_total = summary.get("widgets", 0)
        print(f"     charts={result.charts_detected}  kpis={result.kpis_detected}  widgets={result.widgets_total}")
    except Exception as e:
        result.error = f"analyze failed: {e}"
        print(f"  ERROR: {e}")
        return result

    # Step 2: screenshot data_agent frontend — upload file, wait for dashboard render
    print("  [2/5] Screenshotting data_agent frontend ...")
    agent_ss = case_dir / "agent_dashboard.png"
    agent_ss.unlink(missing_ok=True)
    ok = screenshot_frontend(agent_ss, image_path, wait_ms=8000)
    if ok:
        result.agent_screenshot = str(agent_ss)
        print(f"     saved: {agent_ss.name}")
    else:
        print("     failed (non-fatal)")

    # Step 3: publish to DataLens
    print("  [3/5] Building export payload and publishing to DataLens ...")
    payload = build_export_payload(analysis, image_path.name)
    (case_dir / "export_payload.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    dl_result = publish_to_datalens(payload)
    (case_dir / "datalens_result.json").write_text(json.dumps(dl_result, ensure_ascii=False, indent=2))

    dl_url = dl_result.get("url") or dl_result.get("dashboard_url") or ""
    if not dl_url:
        # try nested
        for v in dl_result.values():
            if _is_http_url(v) and "datalens" in v.lower():
                dl_url = v
                break
    if not _is_http_url(dl_url):
        dl_url = ""
    result.datalens_url = dl_url
    if "error" not in dl_result:
        result.datalens_publish_ok = True
        print(f"     published: {dl_url or '(no URL returned)'}")
    else:
        print(f"     publish error: {dl_result.get('error')}")

    # Step 4: screenshot DataLens
    if dl_url:
        print("  [4/5] Screenshotting DataLens result ...")
        dl_ss = case_dir / "datalens_dashboard.png"
        ok = screenshot_url(dl_url, dl_ss, wait_ms=5000)
        if ok:
            result.datalens_screenshot = str(dl_ss)
            print(f"     saved: {dl_ss.name}")
        else:
            print("     failed (non-fatal)")
    else:
        print("  [4/5] Skipping DataLens screenshot (no URL)")

    # Step 5: LLM judge
    print("  [5/5] LLM judge scoring ...")
    spec = analysis.get("spec") or {}
    agent_ss_path = Path(result.agent_screenshot) if result.agent_screenshot else None
    dl_ss_path = Path(result.datalens_screenshot) if result.datalens_screenshot else None
    judgment = llm_judge(image_path, agent_ss_path, dl_ss_path, spec)
    (case_dir / "judgment.json").write_text(json.dumps(judgment, ensure_ascii=False, indent=2))

    result.similarity_score = int(judgment.get("score") or 0)
    result.lost_charts = judgment.get("lost") or []
    result.preserved_charts = judgment.get("preserved") or []
    result.issues = judgment.get("issues") or []
    result.llm_verdict = judgment.get("verdict") or ""
    print(f"     score: {result.similarity_score}/10")
    print(f"     verdict: {result.llm_verdict[:120]}")
    if result.lost_charts:
        print(f"     lost: {result.lost_charts}")

    return result


def write_report(results: List[EvalResult], output_dir: Path) -> None:
    report = output_dir / "eval_report.json"
    csv_path = output_dir / "eval_report.csv"

    report.write_text(json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2))

    fieldnames = [
        "image_name", "charts_detected", "kpis_detected", "widgets_total",
        "similarity_score", "datalens_publish_ok", "datalens_url",
        "preserved_charts", "lost_charts", "issues", "llm_verdict", "error",
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            d = asdict(r)
            d["preserved_charts"] = " | ".join(d["preserved_charts"])
            d["lost_charts"] = " | ".join(d["lost_charts"])
            d["issues"] = " | ".join(d["issues"])
            writer.writerow({k: d[k] for k in fieldnames})

    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    scores = [r.similarity_score for r in results if r.similarity_score > 0]
    if scores:
        avg = sum(scores) / len(scores)
        print(f"Average score: {avg:.1f}/10  (over {len(scores)} images)")
    for r in results:
        mark = "✓" if r.similarity_score >= 7 else ("~" if r.similarity_score >= 4 else "✗")
        print(f"  {mark} {r.image_name:<45} {r.similarity_score}/10  lost={len(r.lost_charts)}")
    print(f"\nReport saved: {report}")
    print(f"CSV saved:    {csv_path}")


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Dashboard reconstruction eval pipeline")
    parser.add_argument("--input", default=str(TESTS_DIR), help="Directory with test images")
    parser.add_argument("--output", default=str(OUTPUT_DIR), help="Output directory for results")
    parser.add_argument("--image", help="Evaluate a single image (filename or full path)")
    args = parser.parse_args()

    input_dir = Path(args.input)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.image:
        img_path = Path(args.image) if Path(args.image).is_absolute() else input_dir / args.image
        images = [img_path]
    else:
        images = sorted(
            p for p in input_dir.iterdir()
            if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg"}
        )

    if not images:
        print(f"No images found in {input_dir}")
        sys.exit(1)

    print(f"Evaluating {len(images)} image(s) from {input_dir}")
    results = []
    for img in images:
        r = run_eval(img, output_dir)
        results.append(r)

    write_report(results, output_dir)


if __name__ == "__main__":
    main()
