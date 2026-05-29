#!/usr/bin/env python3
"""E2E probe: publish a sample dashboard to Visiology and validate widget data.

Usage:
    python scripts/probe_visiology_publish.py [--url http://127.0.0.1:8000]

Steps:
    1. POST /api/export/dashboard/visiology/publish with a sample payload
    2. Print returned dataset_id, dashboard_url, widget_validation
    3. Open the dashboard URL in a browser and screenshot (requires --screenshot flag)
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import httpx

# ── Sample payload ──────────────────────────────────────────────────────────────

PAYLOAD = {
    "dashboard_title": "Probe Dashboard",
    "tables": [
        {
            "columns": ["category", "revenue", "profit", "orders"],
            "rows": [
                {"category": "Электроника", "revenue": 4200000, "profit": 1100000, "orders": 1250},
                {"category": "Мода",        "revenue": 1800000, "profit":  600000, "orders":  950},
                {"category": "Продукты",    "revenue": 3100000, "profit":  900000, "orders": 1750},
                {"category": "Спорт",       "revenue": 2400000, "profit":  750000, "orders": 1100},
                {"category": "Авто",        "revenue": 5500000, "profit": 1400000, "orders":  680},
            ],
        }
    ],
    "charts": [
        {
            "chart_type": "bar",
            "slice_name": "Выручка по категориям",
            "x_field": "category",
            "y_field": "revenue",
            "position": {"left": 0.0, "top": 0.0, "width": 0.5, "height": 0.45},
        },
        {
            "chart_type": "pie",
            "slice_name": "Доля прибыли",
            "x_field": "category",
            "y_field": "profit",
            "position": {"left": 0.5, "top": 0.0, "width": 0.5, "height": 0.45},
        },
        {
            "chart_type": "line",
            "slice_name": "Заказы",
            "x_field": "category",
            "y_field": "orders",
            "position": {"left": 0.0, "top": 0.45, "width": 1.0, "height": 0.45},
        },
    ],
    "kpi_rows": [
        {
            "title": "Итого выручка",
            "metric_name": "total_revenue",
            "y_field": "revenue",
            "color": "#4085D9",
            "position": None,
        }
    ],
}


def run(base_url: str, screenshot: bool) -> None:
    print(f"Posting to {base_url}/api/export/dashboard/visiology/publish ...")
    t0 = time.time()
    resp = httpx.post(
        f"{base_url}/api/export/dashboard/visiology/publish",
        json=PAYLOAD,
        timeout=300,
    )
    elapsed = time.time() - t0
    print(f"  → HTTP {resp.status_code}  ({elapsed:.1f}s)")

    if resp.status_code >= 400:
        print("FAIL:", resp.text[:2000])
        sys.exit(1)

    data = resp.json()
    print(json.dumps(data, ensure_ascii=False, indent=2))

    widget_validation = data.get("widget_validation") or []
    ok_count = sum(1 for w in widget_validation if w.get("ok"))
    fail_count = len(widget_validation) - ok_count
    print(f"\nWidget validation: {ok_count} OK, {fail_count} FAIL")
    for w in widget_validation:
        status = "✓" if w.get("ok") else "✗"
        print(f"  {status} [{w.get('type', '?')}] {w.get('title', '')}  msg={w.get('message')}")

    if fail_count > 0:
        print("\nSome widgets FAILED — DAX not resolving yet.")
        sys.exit(2)

    dashboard_url = data.get("dashboard_url", "")
    print(f"\nDashboard URL: {dashboard_url}")

    if screenshot and dashboard_url:
        _take_screenshot(dashboard_url)

    print("\nProbe PASSED")


def _take_screenshot(url: str) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright not installed, skipping screenshot")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        page.goto(url, wait_until="networkidle", timeout=30000)
        time.sleep(3)
        out = "/tmp/visiology_probe_screenshot.png"
        page.screenshot(path=out, full_page=False)
        browser.close()
        print(f"Screenshot saved: {out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Visiology publish E2E probe")
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="Backend base URL")
    parser.add_argument("--screenshot", action="store_true", help="Take a screenshot of the dashboard")
    args = parser.parse_args()
    run(args.url, args.screenshot)
