#!/usr/bin/env python3
"""E2E: загрузить датасет в агент → сгенерировать дашборд → опубликовать в Visiology → скриншоты обоих.

Usage:
    python scripts/e2e_visiology.py [--backend http://127.0.0.1:8000]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

OUT = Path("/tmp/e2e_visiology")
OUT.mkdir(parents=True, exist_ok=True)

DATASET_PATH = Path(__file__).parent.parent / "public/datasets/crm_requests_export.xlsx"

# Тема дашборда для генерации
DASHBOARD_TOPIC = "анализ CRM-заявок по стадии, типу и подразделению"


def log(msg: str) -> None:
    print(f"[e2e] {msg}", flush=True)


def screenshot(url: str, out_path: Path, wait_ms: int = 4000, login: tuple | None = None) -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("playwright не установлен — пропускаю скриншот")
        return False

    log(f"Скриншот: {url} → {out_path}")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--ignore-certificate-errors"])
        ctx = browser.new_context(
            viewport={"width": 1600, "height": 900},
            ignore_https_errors=True,
        )
        page = ctx.new_page()

        if login:
            # Авторизация на Visiology через Keycloak login form
            login_url, username, password = login
            page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)
            try:
                page.fill("input[name='username'], #username", username, timeout=5000)
                page.fill("input[name='password'], #password", password, timeout=5000)
                page.click("input[type='submit'], button[type='submit']", timeout=5000)
                time.sleep(3)
            except Exception as e:
                log(f"  Авторизация не потребовалась или ошибка: {e}")

        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(wait_ms)
        page.screenshot(path=str(out_path), full_page=False)
        browser.close()
    log(f"  Сохранён: {out_path}")
    return True


def screenshot_agent_dashboard(frontend_url: str, session_id: str, dashboard: dict, out_path: Path) -> bool:
        try:
                from playwright.sync_api import sync_playwright
        except ImportError:
                log("playwright не установлен — пропускаю скриншот агента")
                return False

        project = {
                "id": "proj_e2e_visiology",
                "name": "genbi_access_requests",
                "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "files": [],
                "imageFile": None,
                "issues": [],
                "detailTables": [],
                "selectedERDModel": "star",
                "erdGenerated": False,
                "dashboardBuilt": True,
                "widgets": [],
                "dashboardCharts": dashboard.get("charts") or [],
                "status": "dashboard_built",
                "petalStatuses": {
                        "data": "green",
                        "detail": "grey",
                        "mart": "grey",
                        "model": "green",
                        "mockup": "grey",
                        "dashboard": "green",
                },
                "petalEnabled": {
                        "data": True,
                        "detail": False,
                        "mart": False,
                        "model": True,
                        "mockup": False,
                        "dashboard": True,
                },
        }

        log(f"Скриншот агента: {frontend_url} → {out_path}")
        with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=["--ignore-certificate-errors"])
                ctx = browser.new_context(viewport={"width": 1600, "height": 1400})
                page = ctx.new_page()
                page.goto(frontend_url, wait_until="networkidle", timeout=60000)
                page.evaluate(
                        """
                        ([seedProject, seedSessionId]) => {
                            localStorage.setItem('data_agent_projects_v1', JSON.stringify({
                                projects: [seedProject],
                                activeProjectId: seedProject.id,
                            }));
                            localStorage.setItem('data_agent_session_v1', seedSessionId);
                        }
                        """,
                        [project, session_id],
                )
                page.reload(wait_until="networkidle", timeout=60000)
                page.evaluate(
                        """
                        () => {
                            const target = Array.from(document.querySelectorAll('div,button')).find((el) => {
                                const text = (el.textContent || '').trim();
                                return text === '4' && getComputedStyle(el).cursor === 'pointer';
                            });
                            if (!target) {
                                throw new Error('dashboard step not found');
                            }
                            target.click();
                        }
                        """
                )
                page.wait_for_selector("text=Общее количество заявок", timeout=30000)
                page.wait_for_timeout(1500)
                page.screenshot(path=str(out_path), full_page=False)
                browser.close()

        log(f"  Сохранён: {out_path}")
        return True


def run(backend: str) -> None:
    client = httpx.Client(base_url=backend, timeout=300)

    # ── 1. Создать сессию ───────────────────────────────────────────────────────
    log("1. Создаю сессию...")
    r = client.post("/api/sessions")
    r.raise_for_status()
    session_id = r.json()["session_id"]
    log(f"   session_id = {session_id}")

    # ── 2. Загрузить датасет ────────────────────────────────────────────────────
    log(f"2. Загружаю {DATASET_PATH.name}...")
    with open(DATASET_PATH, "rb") as f:
        r = client.post(
            f"/api/sessions/{session_id}/upload",
            files={"file": (DATASET_PATH.name, f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            timeout=60,
        )
    r.raise_for_status()
    upload = r.json()
    table_name = upload["table_name"]
    log(f"   table={table_name}, rows={upload['row_count']}, cols={len(upload['columns'])}")

    # ── 3. Сгенерировать дашборд ────────────────────────────────────────────────
    log(f"3. Генерирую дашборд по теме: «{DASHBOARD_TOPIC}»...")
    r = client.post(
        f"/api/sessions/{session_id}/dashboard",
        json={"topic": DASHBOARD_TOPIC},
        timeout=120,
    )
    r.raise_for_status()
    dashboard = r.json()
    charts = dashboard.get("charts") or []
    log(f"   Получено виджетов: {len(charts)}")
    for c in charts:
        log(f"     [{c.get('chart_type','?')}] {c.get('slice_name') or c.get('title','')}")

    # Сохраним JSON дашборда для отладки
    (OUT / "dashboard_spec.json").write_text(json.dumps(dashboard, ensure_ascii=False, indent=2))

    # ── 4. Получить данные таблицы для передачи в Visiology ─────────────────────
    # Читаем файл напрямую (query API не сериализует Timestamp)
    log("4. Читаю данные таблицы из файла...")
    columns, rows = _read_dataset(DATASET_PATH)
    log(f"   Получено строк: {len(rows)}, колонок: {len(columns)}")

    # ── 5. Собрать payload для Visiology ────────────────────────────────────────
    log("5. Собираю payload для Visiology...")
    dashboard_title = "CRM Заявки — Анализ"
    slim_columns, slim_rows = _build_crm_slim_table(columns, rows)
    log(f"   Таблица: {len(slim_rows)} строк, колонки: {slim_columns}")
    payload = _build_payload(dashboard_title, slim_columns, slim_rows, charts)
    (OUT / "visiology_payload.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    log(f"   charts в payload: {len(payload['charts'])}, kpi_rows: {len(payload.get('kpi_rows', []))}")

    # ── 6. Опубликовать в Visiology ─────────────────────────────────────────────
    log("6. Публикую в Visiology (может занять ~60с)...")
    t0 = time.time()
    r = client.post("/api/export/dashboard/visiology/publish", json=payload, timeout=300)
    elapsed = time.time() - t0
    if r.status_code >= 400:
        log(f"   ОШИБКА HTTP {r.status_code}: {r.text[:1000]}")
        sys.exit(1)
    result = r.json()
    log(f"   Готово за {elapsed:.1f}с")
    (OUT / "visiology_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))

    dashboard_url = result.get("dashboard_url", "")
    designer_url = result.get("designer_url", "")
    widget_validation = result.get("widget_validation") or []
    ok_count = sum(1 for w in widget_validation if w.get("ok"))
    fail_count = len(widget_validation) - ok_count

    log(f"   dashboard_url: {dashboard_url}")
    log(f"   designer_url:  {designer_url}")
    log(f"   Widget validation: {ok_count} OK, {fail_count} FAIL")
    for w in widget_validation:
        mark = "✓" if w.get("ok") else "✗"
        log(f"     {mark} [{w.get('type','?')}] {w.get('title','')}  msg={w.get('message')}")

    # ── 7. Скриншот агента данных ───────────────────────────────────────────────
    log("7. Скриншот дашборда в агенте данных...")
    agent_shot = OUT / "screenshot_agent.png"
    # Ищем фронтенд на типичных портах
    agent_ui_url = None
    for port in (5174, 5173, 4173, 3001):
        try:
            test = httpx.get(f"http://127.0.0.1:{port}/", timeout=3)
            html = test.text[:500]
            if test.status_code < 400 and ("data" in html.lower() or "агент" in html.lower() or "upload" in html.lower() or "vite" in html.lower() or "root" in html):
                agent_ui_url = f"http://127.0.0.1:{port}/"
                break
        except Exception:
            continue

    if not agent_ui_url:
        log("   Фронтенд не найден — пропускаю скриншот агента")
    else:
        screenshot_agent_dashboard(agent_ui_url, session_id, dashboard, agent_shot)

    # ── 8. Скриншот Visiology ───────────────────────────────────────────────────
    log("8. Скриншот дашборда в Visiology...")
    visiology_shot = OUT / "screenshot_visiology.png"
    if dashboard_url:
        login_args = ("https://demo.visiology.su/v3/", "user", "Visiology3User")
        screenshot(dashboard_url, visiology_shot, wait_ms=6000, login=login_args)
    else:
        log("   dashboard_url пустой — пропускаю")

    # ── Итог ────────────────────────────────────────────────────────────────────
    log("")
    log("=" * 60)
    log("РЕЗУЛЬТАТ:")
    log(f"  Dataset:         {table_name} ({upload['row_count']} строк)")
    log(f"  Виджетов в агенте: {len(charts)}")
    log(f"  Visiology URL:   {dashboard_url}")
    log(f"  Designer URL:    {designer_url}")
    log(f"  Widget OK/FAIL:  {ok_count}/{fail_count}")
    log(f"  Скриншот агента: {agent_shot}")
    log(f"  Скриншот Visiology: {visiology_shot}")
    log(f"  Артефакты:       {OUT}/")
    log("=" * 60)

    if fail_count > 0:
        log(f"\nВНИМАНИЕ: {fail_count} виджет(ов) не загрузились в Visiology.")
        log("Попробуй открыть dashboard_url вручную через несколько секунд — FE может ещё индексировать.")
        sys.exit(2)
    else:
        log("\nВсе виджеты OK — задача выполнена!")


def _build_payload(
    title: str,
    columns: list[str],
    rows: list[dict],
    charts: list[dict],
) -> dict:
    """Собрать payload для publishVisiologyDashboard из агрегированных данных."""
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

    _type_map = {
        "bar": "bar", "column": "bar", "dist_bar": "bar",
        "bar_horizontal": "bar_horizontal", "hbar": "bar_horizontal",
        "line": "line", "area": "area",
        "pie": "pie", "donut": "donut",
        "big_number": "big_number", "kpi": "big_number",
        "table": "table", "pivot_table": "pivot_table",
        "scatter": "scatter", "funnel": "funnel",
    }

    # Меру используем 'count' (1 на строку, Visiology агрегирует через SUM)
    MEASURE = "count"
    DIM_COLS = ["Стадия", "Тип", "Подразделение", "Тип доступа", stage_type_column]

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
        return DIM_COLS[index % len(DIM_COLS)]

    kpi_rows = []
    chart_items = []
    dim_idx = 0
    visual_idx = 0

    for i, c in enumerate(charts):
        ct_raw = str(c.get("chart_type") or c.get("viz_type") or c.get("actualType") or c.get("type") or "bar")
        ct = _type_map.get(ct_raw, ct_raw)

        title_chart = c.get("slice_name") or c.get("title") or f"Виджет {i+1}"

        x = pick_dimension(title_chart, dim_idx)

        if ct in {"big_number", "kpi"}:
            kpi_rows.append({
                "title": title_chart,
                "metric_name": MEASURE,
                "y_field": MEASURE,
                "value": c.get("value"),
                "color": c.get("color"),
                "position": {
                    "left": len(kpi_rows) * 0.25,
                    "top": 0.0,
                    "width": 0.25,
                    "height": 0.15,
                },
            })
        else:
            chart_items.append({
                "chart_type": ct,
                "slice_name": title_chart,
                "x_field": x,
                "y_field": MEASURE,
                "position": c.get("position") or {
                    "left": (visual_idx % 2) * 0.5,
                    "top": 0.19 + (visual_idx // 2) * 0.26,
                    "width": 0.5,
                    "height": 0.23,
                },
                "series_colors": c.get("series_colors") or None,
            })
            dim_idx += 1
            visual_idx += 1

    return {
        "dashboard_title": title,
        "tables": [table],
        "charts": chart_items,
        "kpi_rows": kpi_rows,
    }


def _build_crm_slim_table(columns: list[str], rows: list[dict]) -> tuple[list[str], list[dict]]:
    """Keep only the CRM dimensions that are known to publish reliably in Visiology."""

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

    slim_rows = []
    for row in rows:
        slim_row = {label: str(row.get(source) or "Не указано") for source, label in selected if source}
        slim_row["count"] = 1
        slim_rows.append(slim_row)

    return [label for _, label in selected] + ["count"], slim_rows


def _read_dataset(path: Path) -> tuple[list[str], list[dict]]:
    """Read xlsx/csv → (columns, rows) with all values JSON-safe."""
    import pandas as pd
    df = pd.read_excel(path) if path.suffix.lower() in {".xlsx", ".xls"} else pd.read_csv(path)
    # Convert timestamps to ISO strings, NaN → None
    for col in df.columns:
        if df[col].dtype.kind == "M":  # datetime
            df[col] = df[col].dt.strftime("%Y-%m-%d").where(df[col].notna(), None)
    cols = list(df.columns)
    rows = []
    for _, row in df.iterrows():
        rows.append({c: (None if (val is None or (isinstance(val, float) and __import__('math').isnan(val))) else val)
                     for c, val in row.items()})
    return cols, rows


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    run(args.backend)
