#!/usr/bin/env python3
"""Minimal proof: ChangeDimSelection(All) fixes dataRange after CSV import.

Streamlined single-purpose probe — just verify the fix works, then we'll
wire it into foresight_service.py.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8110/fp10.x"
REPO = "FS_DEMO"
USER = "FP_ADMIN"
PASS = "FP_ADMIN"
OUT = Path("/tmp/probe_verify_fix")
OUT.mkdir(parents=True, exist_ok=True)

PP_HEADERS = {
    "Content-Type": "application/json; charset=UTF-8",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}

CSV_DATA = """period,category,value
2024-01,A,100
2024-01,B,200
2024-01,C,300
2024-01,D,150
2024-01,E,250
"""


def pp(req_ctx, body: dict) -> dict:
    response = req_ctx.post(
        f"{BASE}/app/PPService.axd",
        data=json.dumps(body, ensure_ascii=False),
        headers=PP_HEADERS,
    )
    text = response.text()
    if response.status != 200:
        raise RuntimeError(f"PPService {response.status}: {text[:800]}")
    return json.loads(text)


def get_dr(req_ctx, eax_id: str) -> dict:
    r = pp(req_ctx, {"GetEaxMd": {"tEax": {"id": eax_id}, "tArg": {"pattern": {"chart": True}}}})
    return r.get("GetEaxMdResult", {}).get("meta", {}).get("chart", {}).get("dataRange", {})


def get_dims(req_ctx, eax_id: str) -> list[dict]:
    r = pp(req_ctx, {"GetEaxMd": {"tEax": {"id": eax_id}, "tArg": {"pattern": {
        "dims": True,
        "dimArg": {
            "elsArg": {"totalCount": True, "selectionInfo": True, "filter": {"levels": 0, "elementsGroup": True}, "pattern": {"attributes": "*"}},
            "pattern": {"getDescr": True, "getIsAllSelected": True},
        },
    }}}})
    dims_raw = r.get("GetEaxMdResult", {}).get("meta", {}).get("dims", {})
    its = dims_raw.get("its", {}).get("it", []) if isinstance(dims_raw, dict) else []
    return [{"id": d.get("id"), "name": (d.get("descr") or {}).get("name", "?"), "key": d.get("key"), "isAllSelected": d.get("isAllSelected")} for d in its]


def main() -> None:
    state: dict = {"eax_ids": []}
    csv_path = OUT / "probe.csv"
    csv_path.write_text(CSV_DATA, encoding="utf-8")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1600, "height": 900})
        page = ctx.new_page()

        def on_req(req):
            if "PPService.axd" in req.url:
                post = req.post_data or ""
                m = re.search(r'"tEax":\{"id":"([^"]+!DSO![^"]+)"\}', post)
                if m and m.group(1) not in state["eax_ids"]:
                    state["eax_ids"].append(m.group(1))

        page.on("request", on_req)

        # LOGIN with retry
        for attempt in range(3):
            page.goto(f"{BASE}/app/login.html#repo={REPO}", wait_until="networkidle", timeout=60000)
            if "login" in page.url or page.locator('input[name="username"]').count() > 0:
                break
            time.sleep(3)

        page.fill('input[name="username"]', USER)
        page.fill('input[type="password"]', PASS)
        page.keyboard.press("Enter")
        page.wait_for_timeout(12000)
        page.screenshot(path=str(OUT / "01_after_login.png"))

        # Check login success - look for dashboard content, not login page markers
        current_url = page.url
        page_content = page.content()
        print(f"URL after login: {current_url}")
        print(f"Page title: {page.title()}")

        # Retry login if still on login page (url still has login.html)
        if "login.html" in current_url or page.locator('input[name="username"]').count() > 0:
            print("Still on login page, retrying...")
            page.wait_for_timeout(5000)
            try:
                page.fill('input[name="username"]', USER)
                page.fill('input[type="password"]', PASS)
                page.get_by_text("Log in", exact=True).click(timeout=5000)
            except Exception:
                page.keyboard.press("Enter")
            page.wait_for_timeout(12000)
            page.screenshot(path=str(OUT / "01b_retry_login.png"))
            current_url = page.url

        if "login.html" in current_url and page.locator('input[name="username"]').count() > 0:
            print("Login definitively failed")
            browser.close()
            return

        print(f"Logged in, URL: {current_url}")

        # DASHBOARDS
        for txt in ["Dashboards", "Дашборды", "Панели"]:
            try:
                page.get_by_text(txt, exact=True).click(timeout=4000)
                break
            except Exception:
                pass
        page.wait_for_timeout(3000)

        # NEW DASHBOARD
        for txt in ["New", "Новый", "Создать"]:
            try:
                page.get_by_text(txt, exact=True).last.click(timeout=3000, force=True)
                break
            except Exception:
                pass
        page.wait_for_timeout(3000)
        page.screenshot(path=str(OUT / "02_new_dashboard.png"))

        # INSERT CHART widget — use #InsertCategory (known working sequence)
        page.screenshot(path=str(OUT / "02b_before_insert.png"))
        try:
            page.locator("#InsertCategory").click(timeout=5000)
            print("Clicked #InsertCategory")
        except Exception as e:
            print(f"#InsertCategory not found: {e}, trying INSERT tab click")
            try:
                page.get_by_text("INSERT", exact=True).click(timeout=3000, force=True)
            except Exception:
                pass
        page.wait_for_timeout(800)
        page.screenshot(path=str(OUT / "02c_after_insert_click.png"))

        # In the dropdown/ribbon, click Chart visualizer
        page.mouse.click(286, 98)
        page.wait_for_timeout(700)
        page.screenshot(path=str(OUT / "02d_after_chart_click.png"))

        # Click on canvas to place widget
        page.mouse.click(290, 154)
        page.wait_for_timeout(5000)
        page.screenshot(path=str(OUT / "03_chart_inserted.png"))

        # Wait for EAX id
        for _ in range(30):
            if state["eax_ids"]:
                break
            page.wait_for_timeout(500)
        print(f"EAX ids after insert: {state['eax_ids']}")

        # DATA IMPORT — use same working sequence as probe_capture_all_selection.py
        page.screenshot(path=str(OUT / "04_before_import.png"))

        # Click "Data import" button
        try:
            page.get_by_text("Data import", exact=False).last.click(timeout=5000, force=True)
            print("Clicked 'Data import'")
        except Exception as e:
            print(f"Data import not found: {e}")

        page.wait_for_timeout(2000)
        page.screenshot(path=str(OUT / "05_data_import.png"))

        # Click "File with data"
        try:
            page.get_by_text("File with data", exact=False).click(timeout=5000, force=True)
            print("Clicked 'File with data'")
        except Exception:
            page.mouse.click(585, 450)
        page.wait_for_timeout(1000)
        page.screenshot(path=str(OUT / "06_file_with_data.png"))

        # Upload file — prefer expect_file_chooser triggered by "Next >"
        uploaded = False
        try:
            with page.expect_file_chooser(timeout=5000) as fc:
                page.get_by_text("Next >", exact=True).last.click(timeout=5000, force=True)
            fc.value.set_files(str(csv_path))
            uploaded = True
            print("Uploaded via Next > chooser")
        except Exception as e:
            print(f"Next > chooser failed: {e}")

        if not uploaded:
            # Fallback: click Next > then Browse
            try:
                page.get_by_text("Next >", exact=True).last.click(timeout=5000, force=True)
                page.wait_for_timeout(3000)
                with page.expect_file_chooser(timeout=5000) as fc:
                    page.get_by_text("Browse", exact=False).click(timeout=5000, force=True)
                fc.value.set_files(str(csv_path))
                uploaded = True
                print("Uploaded via Browse fallback")
            except Exception as e:
                print(f"Browse fallback failed: {e}")

        if not uploaded:
            page.screenshot(path=str(OUT / "upload_failed.png"))
            print("All upload methods failed, aborting")
            browser.close()
            return

        page.wait_for_timeout(5000)
        page.screenshot(path=str(OUT / "07_after_upload.png"))

        # Column mapping — Next >
        try:
            page.get_by_text("Next >", exact=True).last.click(timeout=5000, force=True)
            print("Clicked Next > for column mapping")
        except Exception:
            pass
        page.wait_for_timeout(5000)
        page.screenshot(path=str(OUT / "08_column_mapping.png"))

        # Import
        try:
            page.get_by_text("Import", exact=True).last.click(timeout=5000, force=True)
            print("Clicked Import")
        except Exception:
            pass
        page.wait_for_timeout(12000)
        page.screenshot(path=str(OUT / "09_importing.png"))

        # OK / Finish dialogs
        for txt in ["OK", "Finish", "Готово"]:
            try:
                page.get_by_text(txt, exact=True).last.click(timeout=3000, force=True)
                print(f"Clicked: {txt}")
            except Exception:
                pass
        page.wait_for_timeout(3000)

        eax_id = state["eax_ids"][0] if state["eax_ids"] else None
        print(f"\nFinal EAX id: {eax_id}")

        if not eax_id:
            page.screenshot(path=str(OUT / "no_eax_id.png"))
            print("No EAX id captured")
            browser.close()
            return

        # ===== KEY TEST: ChangeDimSelection(All) =====
        dr_before = get_dr(ctx.request, eax_id)
        print(f"\ndataRange BEFORE: type={dr_before.get('type')}, w={dr_before.get('width')}, h={dr_before.get('height')}")

        dims = get_dims(ctx.request, eax_id)
        print(f"Dims ({len(dims)}):")
        for d in dims:
            print(f"  {d['name']}: key={d['key']}, isAllSelected={d['isAllSelected']}, id={d['id']}")

        # Call ChangeDimSelection for all dims
        for d in dims:
            dim_id = d.get("id") or f"{eax_id}!{d['key']}"
            try:
                r = pp(ctx.request, {"BatchExec": {"tArg": {"its": {"it": [
                    {"ChangeDimSelection": {
                        "tDim": {"id": dim_id},
                        "tArg": {
                            "elSelectOp": "Select",
                            "elRelative": "All",
                            "elKeys": {"it": []},
                            "ignoreMissingKeys": False,
                            "pattern": {"attributes": "*"},
                            "schemaNoApply": True,
                        },
                    }},
                ]}}}})
                print(f"  ChangeDimSelection(All) for {d['name']}: OK")
            except Exception as e:
                print(f"  ChangeDimSelection(All) for {d['name']}: ERROR {e}")

        # Refresh
        try:
            ref = pp(ctx.request, {"SetEaxMd": {
                "tEax": {"id": eax_id},
                "tArg": {
                    "pattern": {"grid": True},
                    "meta": {"grid": {"dataDisplayMode": "Interactive"}},
                    "refresh": {"chart": True, "fetchData": True, "saveData": False},
                    "metaGet": {"chart": True},
                },
            }})
            ref_dr = ref.get("SetEaxMdResult", {}).get("meta", {}).get("chart", {}).get("dataRange", {})
            print(f"\ndataRange in refresh response: type={ref_dr.get('type')}, w={ref_dr.get('width')}, h={ref_dr.get('height')}")
        except Exception as e:
            print(f"Refresh error: {e}")

        page.wait_for_timeout(3000)

        dr_after = get_dr(ctx.request, eax_id)
        print(f"\ndataRange AFTER: type={dr_after.get('type')}, w={dr_after.get('width')}, h={dr_after.get('height')}")

        page.screenshot(path=str(OUT / "10_final.png"), full_page=True)
        result = {
            "eax_id": eax_id,
            "dr_before": dr_before,
            "dr_after": dr_after,
            "dims": dims,
        }
        (OUT / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nResult saved: {OUT}/result.json")

        browser.close()


if __name__ == "__main__":
    main()
