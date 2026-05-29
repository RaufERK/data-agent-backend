#!/usr/bin/env python3
"""Isolate which SetEaxMd call resets Foresight chart dataRange."""
from __future__ import annotations

import json
import re
from pathlib import Path

from playwright.sync_api import sync_playwright


BASE = "http://127.0.0.1:8110/fp10.x"
REPO = "FS_DEMO"
USER = "FP_ADMIN"
PASS = "FP_ADMIN"
OUT = Path("/tmp/probe_visualizer_datarange_steps")
OUT.mkdir(parents=True, exist_ok=True)

PP_HEADERS = {
    "Content-Type": "application/json; charset=UTF-8",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}

CSV_DATA = """category,revenue
A,100
B,200
C,300
D,150
E,250
"""


def pp(req_ctx, body: dict) -> dict:
    response = req_ctx.post(
        f"{BASE}/app/PPService.axd",
        data=json.dumps(body, ensure_ascii=False),
        headers=PP_HEADERS,
    )
    text = response.text()
    if response.status != 200:
        raise RuntimeError(f"PPService {response.status}: {text[:1200]}")
    return json.loads(text)


def get_chart(req_ctx, eax_id: str) -> dict:
    result = pp(req_ctx, {"GetEaxMd": {"tEax": {"id": eax_id}, "tArg": {"pattern": {"chart": True}}}})
    return result.get("GetEaxMdResult", {}).get("meta", {}).get("chart", {})


def add_chart_and_import(page, csv_path: Path) -> None:
    try:
        page.locator("#InsertCategory").click(timeout=5000)
    except Exception:
        page.mouse.click(220, 42)
    page.wait_for_timeout(500)
    page.mouse.click(286, 98)
    page.wait_for_timeout(700)
    page.mouse.click(290, 154)
    page.wait_for_timeout(3000)

    page.get_by_text("Data import", exact=False).last.click(timeout=5000, force=True)
    page.wait_for_timeout(2000)
    try:
        page.get_by_text("File with data", exact=False).click(timeout=5000, force=True)
    except Exception:
        page.mouse.click(585, 450)
    page.wait_for_timeout(1000)

    try:
        with page.expect_file_chooser(timeout=5000) as chooser:
            page.get_by_text("Next >", exact=True).last.click(timeout=5000, force=True)
        chooser.value.set_files(str(csv_path))
    except Exception:
        page.get_by_text("Next >", exact=True).last.click(timeout=5000, force=True)
        page.wait_for_timeout(3000)
        with page.expect_file_chooser(timeout=5000) as chooser:
            page.get_by_text("Browse", exact=False).click(timeout=5000, force=True)
        chooser.value.set_files(str(csv_path))

    page.wait_for_timeout(5000)
    page.get_by_text("Next >", exact=True).last.click(timeout=5000, force=True)
    page.wait_for_timeout(5000)
    page.get_by_text("Import", exact=True).last.click(timeout=5000, force=True)
    page.wait_for_timeout(10000)
    try:
        page.get_by_text("OK", exact=True).last.click(timeout=5000, force=True)
    except Exception:
        pass
    page.wait_for_timeout(3000)
    try:
        page.get_by_text("Finish", exact=True).last.click(timeout=5000, force=True)
    except Exception:
        pass
    page.wait_for_timeout(3000)


def main() -> None:
    log: list[dict] = []
    csv_path = OUT / "probe.csv"
    csv_path.write_text(CSV_DATA, encoding="utf-8")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1600, "height": 900})
        page = ctx.new_page()
        state: dict[str, object] = {"root_id": None, "adhoc_id": None, "eax_ids": [], "cube_keys": []}

        def on_request(req) -> None:
            post = req.post_data or ""
            if state["root_id"] is None and "!M!Root" in post:
                match = re.search(r"([A-Z0-9]+!M!Root)", post)
                if match:
                    state["root_id"] = match.group(1)
            if state["adhoc_id"] is None and '"tAdHocId":{"id":"' in post:
                match = re.search(r'"tAdHocId":\{"id":"([^"]+)"\}', post)
                if match:
                    state["adhoc_id"] = match.group(1)
            cube_match = re.search(r'"cube":\{"obDesc":\{.*?"k":(\d+)', post)
            if cube_match:
                keys = state["cube_keys"]
                key = int(cube_match.group(1))
                if isinstance(keys, list) and key not in keys:
                    keys.append(key)
            eax_match = re.search(r'"tEax":\{"id":"([^"]+!DSO![^"]+)"\}', post)
            if eax_match:
                eax_ids = state["eax_ids"]
                eax_id = eax_match.group(1)
                if isinstance(eax_ids, list) and eax_id not in eax_ids:
                    eax_ids.append(eax_id)

        page.on("request", on_request)
        page.goto(f"{BASE}/app/login.html#repo={REPO}", wait_until="networkidle", timeout=60000)
        page.fill('input[name="username"]', USER)
        page.fill('input[type="password"]', PASS)
        page.keyboard.press("Enter")
        page.wait_for_timeout(5000)

        try:
            page.get_by_text("Dashboards", exact=True).click(timeout=5000)
        except Exception:
            page.mouse.click(512, 420)
        for _ in range(20):
            if state["root_id"]:
                break
            page.wait_for_timeout(500)

        try:
            page.get_by_text("New", exact=True).last.click(timeout=3000, force=True)
        except Exception:
            page.mouse.click(515, 580)
        for _ in range(20):
            if state["adhoc_id"]:
                break
            page.wait_for_timeout(500)

        add_chart_and_import(page, csv_path)
        eax_ids = state["eax_ids"]
        if not isinstance(eax_ids, list) or not eax_ids:
            raise RuntimeError("No EAX id captured after UI import")
        eax_id = str(eax_ids[0])

        chart = get_chart(ctx.request, eax_id)
        log.append({"step": "after_ui_import", "dataRange": chart.get("dataRange"), "hiChart": chart.get("hiChart")})

        pp(ctx.request, {"SetEaxMd": {"tEax": {"id": eax_id}, "tArg": {
            "pattern": {"setChart": {"meta": {"hiChart": json.dumps({
                "chart": {"defaultSeriesType": "line"},
                "plotOptions": {"series": {}},
                "template": None,
            }, ensure_ascii=False)}}},
            "meta": {},
        }}})
        chart = get_chart(ctx.request, eax_id)
        log.append({"step": "after_setChart_only", "dataRange": chart.get("dataRange"), "hiChart": chart.get("hiChart")})

        mode_meta = {
            "chart": {"enabled": True, "visible": True, "active": True, "viewOrder": 0},
            "grid": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
            "speedometer": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
            "bubbleChart": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
            "bubbleTree": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
            "treeMap": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
            "mapChart": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
        }
        pp(ctx.request, {"SetEaxMd": {"tEax": {"id": eax_id}, "tArg": {
            "pattern": {
                "grid": True,
                "chart": True,
                "bubbleChart": True,
                "bubbleTree": True,
                "treeMap": True,
                "mapChart": True,
                "speedometer": True,
            },
            "meta": mode_meta,
            "metaGet": {"chart": True, "grid": True, "speedometer": True},
        }}})
        chart = get_chart(ctx.request, eax_id)
        log.append({"step": "after_visualizer_mode_only", "dataRange": chart.get("dataRange"), "hiChart": chart.get("hiChart")})

        page.screenshot(path=str(OUT / "final.png"), full_page=True)
        browser.close()

    (OUT / "log.json").write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"out": str(OUT), "log": log}, ensure_ascii=False))


if __name__ == "__main__":
    main()
