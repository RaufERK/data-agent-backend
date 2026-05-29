#!/usr/bin/env python3
"""Verify that ChangeDimSelection + SetEaxMd (fetchData) fixes dataRange.

We know the API call. Now we test it purely via API (no UI interaction after import).
Steps:
1. Create EAX widget via UI import
2. Check dataRange (expect 1x1)
3. Get dim IDs from GetEaxMd
4. Call ChangeDimSelection(elRelative=All) for each non-Values dim
5. Call SetEaxMd(fetchData=True) to trigger re-render
6. Check dataRange again (expect None or > 1x1)
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8110/fp10.x"
REPO = "FS_DEMO"
USER = "FP_ADMIN"
PASS = "FP_ADMIN"
OUT = Path("/tmp/probe_change_dim_api")
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
        raise RuntimeError(f"PPService {response.status}: {text[:1200]}")
    return json.loads(text)


def get_chart_meta(req_ctx, eax_id: str) -> dict:
    result = pp(req_ctx, {"GetEaxMd": {"tEax": {"id": eax_id}, "tArg": {"pattern": {"chart": True}}}})
    return result.get("GetEaxMdResult", {}).get("meta", {}).get("chart", {})


def get_dims(req_ctx, eax_id: str) -> list[dict]:
    """Return list of dim dicts with id, name, selectionInfo."""
    result = pp(req_ctx, {"GetEaxMd": {"tEax": {"id": eax_id}, "tArg": {"pattern": {
        "dims": True,
        "dimArg": {
            "elsArg": {"totalCount": True, "selectionInfo": True, "filter": {"levels": 0, "elementsGroup": True}, "pattern": {"attributes": "*"}},
            "pattern": {"getDescr": True, "getIsAllSelected": True, "getSelection": True},
        },
    }}}})
    dims_raw = result.get("GetEaxMdResult", {}).get("meta", {}).get("dims", {})
    its = dims_raw.get("its", {}).get("it", []) if isinstance(dims_raw, dict) else []
    dims = []
    for d in its:
        dims.append({
            "id": d.get("id"),
            "name": d.get("descr", {}).get("name") if isinstance(d.get("descr"), dict) else None,
            "key": d.get("key"),
            "selectionInfo": d.get("selectionInfo"),
            "isAllSelected": d.get("isAllSelected"),
        })
    return dims


def change_dim_selection_all(req_ctx, dim_id: str) -> dict:
    """Send ChangeDimSelection with elRelative=All."""
    body = {"BatchExec": {"tArg": {"its": {"it": [
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
    ]}}}}
    result = pp(req_ctx, body)
    return result


def refresh_eax(req_ctx, eax_id: str) -> dict:
    """Trigger data refresh on EAX."""
    result = pp(req_ctx, {"SetEaxMd": {
        "tEax": {"id": eax_id},
        "tArg": {
            "pattern": {"grid": True},
            "meta": {"grid": {"dataDisplayMode": "Interactive"}},
            "refresh": {
                "grid": False,
                "chart": True,
                "mapChart": False,
                "fetchData": True,
                "saveData": False,
            },
            "metaGet": {
                "chart": True,
                "dims": True,
                "dimArg": {
                    "elsArg": {"totalCount": True, "selectionInfo": True, "filter": {"levels": 0, "elementsGroup": True}, "pattern": {"attributes": "*"}},
                    "pattern": {"getDescr": True, "getIsAllSelected": True, "getSelection": True},
                },
                "specificRanges": {"all": True},
                "pivot": True,
            },
        },
    }})
    return result


def main() -> None:
    state: dict = {"eax_ids": [], "adhoc_id": None}
    csv_path = OUT / "probe.csv"
    csv_path.write_text(CSV_DATA, encoding="utf-8")
    log: list[dict] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1600, "height": 900})
        page = ctx.new_page()

        def on_request(req):
            if "PPService.axd" in req.url:
                post = req.post_data or ""
                eax_m = re.search(r'"tEax":\{"id":"([^"]+!DSO![^"]+)"\}', post)
                if eax_m:
                    eid = eax_m.group(1)
                    if eid not in state["eax_ids"]:
                        state["eax_ids"].append(eid)
                if state["adhoc_id"] is None:
                    m = re.search(r'"tAdHocId":\{"id":"([^"]+)"\}', post)
                    if m:
                        state["adhoc_id"] = m.group(1)

        page.on("request", on_request)

        # LOGIN
        page.goto(f"{BASE}/app/login.html#repo={REPO}", wait_until="networkidle", timeout=60000)
        page.fill('input[name="username"]', USER)
        page.fill('input[type="password"]', PASS)
        page.keyboard.press("Enter")
        page.wait_for_timeout(5000)

        # DASHBOARDS → NEW
        try:
            page.get_by_text("Dashboards", exact=True).click(timeout=5000)
        except Exception:
            page.mouse.click(512, 420)
        page.wait_for_timeout(3000)
        try:
            page.get_by_text("New", exact=True).last.click(timeout=3000, force=True)
        except Exception:
            page.mouse.click(515, 580)
        page.wait_for_timeout(3000)

        # INSERT CHART
        try:
            page.locator("#InsertCategory").click(timeout=5000)
        except Exception:
            page.mouse.click(220, 42)
        page.wait_for_timeout(500)
        page.mouse.click(286, 98)
        page.wait_for_timeout(700)
        page.mouse.click(290, 154)
        page.wait_for_timeout(4000)
        for _ in range(20):
            if state["eax_ids"]:
                break
            page.wait_for_timeout(500)

        # DATA IMPORT
        try:
            page.get_by_text("Data import", exact=False).last.click(timeout=5000, force=True)
        except Exception:
            pass
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
            try:
                page.get_by_text("Next >", exact=True).last.click(timeout=5000, force=True)
                page.wait_for_timeout(3000)
                with page.expect_file_chooser(timeout=5000) as chooser:
                    page.get_by_text("Browse", exact=False).click(timeout=5000, force=True)
                chooser.value.set_files(str(csv_path))
            except Exception as e:
                print(f"Upload failed: {e}")
        page.wait_for_timeout(5000)
        try:
            page.get_by_text("Next >", exact=True).last.click(timeout=5000, force=True)
        except Exception:
            pass
        page.wait_for_timeout(5000)
        try:
            page.get_by_text("Import", exact=True).last.click(timeout=5000, force=True)
        except Exception:
            pass
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

        eax_id = state["eax_ids"][0] if state["eax_ids"] else None
        print(f"EAX id: {eax_id}")

        # STEP 1: Check initial state
        chart = get_chart_meta(ctx.request, eax_id)
        dr = chart.get("dataRange", {})
        print(f"\nStep 1 - Initial dataRange: type={dr.get('type')}, w={dr.get('width')}, h={dr.get('height')}")
        log.append({"step": "initial", "dataRange": dr})

        # STEP 2: Get all dims
        dims = get_dims(ctx.request, eax_id)
        print(f"\nStep 2 - Dims found: {len(dims)}")
        for d in dims:
            print(f"  dim key={d['key']}, name={d['name']}, isAllSelected={d['isAllSelected']}, selInfo={d['selectionInfo']}")
            print(f"  dim id={d['id']}")

        # STEP 3: For each dim, call ChangeDimSelection(All)
        print("\nStep 3 - Selecting all elements for each dim...")
        for d in dims:
            dim_id = d.get("id")
            if not dim_id:
                # Construct from eax_id + key
                dim_id = f"{eax_id}!{d['key']}"
            print(f"  ChangeDimSelection All for dim: {d['name']} (id={dim_id})")
            try:
                result = change_dim_selection_all(ctx.request, dim_id)
                # Check result
                batch_result = result.get("BatchExecResult", {})
                print(f"    Result keys: {list(batch_result.keys()) if isinstance(batch_result, dict) else batch_result}")
            except Exception as e:
                print(f"    ERROR: {e}")

        # STEP 4: Refresh EAX
        print("\nStep 4 - Refreshing EAX data...")
        refresh_result = refresh_eax(ctx.request, eax_id)
        ref_meta = refresh_result.get("SetEaxMdResult", {}).get("meta", {})
        ref_chart = ref_meta.get("chart", {})
        ref_dr = ref_chart.get("dataRange", {})
        print(f"  dataRange from refresh response: type={ref_dr.get('type')}, w={ref_dr.get('width')}, h={ref_dr.get('height')}")
        log.append({"step": "after_refresh", "dataRange": ref_dr})

        # STEP 5: Verify with explicit GetEaxMd
        page.wait_for_timeout(3000)
        chart = get_chart_meta(ctx.request, eax_id)
        dr = chart.get("dataRange", {})
        print(f"\nStep 5 - Final GetEaxMd dataRange: type={dr.get('type')}, w={dr.get('width')}, h={dr.get('height')}")
        log.append({"step": "final_check", "dataRange": dr, "hiChart": chart.get("hiChart")})

        page.screenshot(path=str(OUT / "final.png"), full_page=True)
        (OUT / "log.json").write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nLog saved to {OUT}/log.json")
        print(json.dumps(log, ensure_ascii=False, indent=2))
        browser.close()


if __name__ == "__main__":
    main()
