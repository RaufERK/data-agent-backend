#!/usr/bin/env python3
"""Probe whether save+reload restores the second Foresight data import path."""

from __future__ import annotations

import json
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright

from backend.foresight_service import (
    _cfg,
    _csv_for_chart_inline,
    _run_initial_import,
    _run_subsequent_import,
    _save_object_as,
)

SPEC = Path("artifacts/visiology_manual_bundle/dashboard_spec.json")
OUT = Path("/tmp/foresight_save_reload_second_import")
OUT.mkdir(parents=True, exist_ok=True)


def _num(value) -> float:
    try:
        return float(str(value).replace(",", ".").strip())
    except Exception:
        return 0.0


def _spec_to_payload_chart(chart: dict) -> dict:
    chart_type = chart.get("type", "bar")
    title = chart.get("title", "")

    if chart_type == "kpi":
        value = _num(chart.get("value", 0))
        return {
            "chart_type": "big_number",
            "title": title,
            "x_field": "label",
            "y_field": "value",
            "columns": [{"name": "label"}, {"name": "value"}],
            "rows": [{"label": title, "value": value}],
        }

    if chart_type == "pie":
        rows = [{"category": s["label"], "value": s["value"]} for s in (chart.get("slices") or [])]
        return {
            "chart_type": "pie",
            "title": title,
            "x_field": "category",
            "y_field": "value",
            "columns": [{"name": "category"}, {"name": "value"}],
            "rows": rows,
        }

    categories = chart.get("categories") or []
    series = chart.get("series") or []
    values = series[0].get("values", []) if series else []
    rows = [{"category": cat, "value": val} for cat, val in zip(categories, values)]
    return {
        "chart_type": "bar",
        "title": title,
        "x_field": "category",
        "y_field": "value",
        "columns": [{"name": "category"}, {"name": "value"}],
        "rows": rows,
    }


def main() -> None:
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    charts = [_spec_to_payload_chart(chart) for chart in (spec.get("charts") or [])[:2]]
    if len(charts) < 2:
        raise RuntimeError("Need at least two charts in dashboard_spec.json")

    with tempfile.TemporaryDirectory(prefix="foresight_probe_reload_") as tmpdir:
        tmp = Path(tmpdir)
        csv_paths = []
        for idx, chart in enumerate(charts):
            csv_path = tmp / f"chart_{idx}.csv"
            _csv_for_chart_inline(csv_path, chart)
            csv_paths.append(csv_path)

        object_id = f"DA_SAVE_RELOAD_{int(time.time())}"
        title = f"Data Agent SaveReload Probe {int(time.time())}"
        result: dict[str, object] = {
            "object_id": object_id,
            "title": title,
            "csv_paths": [str(path) for path in csv_paths],
        }

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1600, "height": 900})
            page = context.new_page()
            state: dict[str, object] = {"root_id": None, "adhoc_id": None, "cube_keys": [], "eax_ids": []}

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
                    cube_key = int(cube_match.group(1))
                    if cube_key not in state["cube_keys"]:
                        state["cube_keys"].append(cube_key)
                eax_match = re.search(r'"tEax":\{"id":"([^"]+!DSO![^"]+)"\}', post)
                if eax_match:
                    eax_id = eax_match.group(1)
                    if eax_id not in state["eax_ids"]:
                        state["eax_ids"].append(eax_id)

            page.on("request", on_request)

            base = _cfg.foresight_base_url.rstrip("/")
            page.goto(f"{base}/app/login.html#repo={_cfg.foresight_repo_id}", wait_until="networkidle", timeout=60000)
            page.fill('input[name="username"]', _cfg.foresight_repo_login)
            page.fill('input[type="password"]', _cfg.foresight_repo_password)
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
            if not state["root_id"]:
                raise RuntimeError("Could not capture Foresight Root session id")

            try:
                page.get_by_text("New", exact=True).last.click(timeout=3000, force=True)
            except Exception:
                page.mouse.click(515, 580)
            for _ in range(20):
                if state["adhoc_id"]:
                    break
                page.wait_for_timeout(500)
            if not state["adhoc_id"]:
                raise RuntimeError("Could not capture Foresight AdHoc id")

            cube_keys_before_first = list(state["cube_keys"])
            _run_initial_import(page, csv_paths[0])
            page.screenshot(path=str(OUT / "01_after_first_import.png"), full_page=True)

            new_first_keys = [k for k in state["cube_keys"] if k not in cube_keys_before_first]
            first_cube_key = max(new_first_keys) if new_first_keys else None
            result["first_cube_key"] = first_cube_key

            save_resp = _save_object_as(context.request, base, str(state["adhoc_id"]), title, object_id)
            save_obj = (
                save_resp.get("SaveObjectAsResult", {}).get("object")
                or save_resp.get("tResult", {}).get("ob")
                or {}
            )
            saved_key = save_obj.get("k") or save_obj.get("key")
            result["saved_key"] = saved_key
            result["save_response_excerpt"] = json.dumps(save_obj, ensure_ascii=False)
            if not saved_key:
                raise RuntimeError(f"SaveObjectAs did not return key: {save_resp}")

            edit_url = f"{base}/app/dashboard.html#key={saved_key}&mode=edit&name=Dashboard&repo={_cfg.foresight_repo_id}"
            result["edit_url"] = edit_url
            page.goto(edit_url, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(8000)
            page.screenshot(path=str(OUT / "02_after_reopen_edit.png"), full_page=True)

            cube_keys_before_second = list(state["cube_keys"])
            try:
                _run_subsequent_import(page, csv_paths[1])
                result["second_import_status"] = "ok"
            except Exception as exc:
                result["second_import_status"] = f"error: {exc}"

            page.wait_for_timeout(3000)
            page.screenshot(path=str(OUT / "03_after_second_import_attempt.png"), full_page=True)

            new_second_keys = [k for k in state["cube_keys"] if k not in cube_keys_before_second]
            result["new_cube_keys_after_second"] = new_second_keys
            result["all_cube_keys"] = state["cube_keys"]
            result["eax_ids"] = state["eax_ids"]
            result["final_url"] = page.url
            result["body_excerpt"] = page.locator("body").inner_text()[:4000]

            browser.close()

        (OUT / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()