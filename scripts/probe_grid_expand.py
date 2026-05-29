#!/usr/bin/env python3
"""
Probe grid expansion and chart dataRange after expansion.

Key insight from GetEaxMd:
  - singleCellRangeMode: "EntireGrid" is stored but dataRange stays 1×1
  - The grid (pivot table) must be expanded first, then the chart picks it up
  - Try various grid expansion methods and check chart dataRange after each

Also probe:
  - timeLineDimension/objectivesDimension swap via SetEaxMd
  - seriesInRows = false
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
OUT = Path("/tmp/probe_grid_expand")
OUT.mkdir(parents=True, exist_ok=True)

PP_HEADERS = {
    "Content-Type": "application/json; charset=UTF-8",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}

CSV_DATA = """category,revenue,profit
A,100,10
B,200,20
C,300,30
D,150,15
E,250,25
"""


def pp(req_ctx, body: dict) -> dict:
    r = req_ctx.post(f"{BASE}/app/PPService.axd", data=json.dumps(body, ensure_ascii=False), headers=PP_HEADERS)
    if r.status != 200:
        raise RuntimeError(f"PPService {r.status}: {r.text()[:800]}")
    return json.loads(r.text())


def get_chart_meta(req_ctx, eax_id: str) -> dict:
    r = pp(req_ctx, {"GetEaxMd": {"tEax": {"id": eax_id}, "tArg": {"pattern": {"chart": True}}}})
    return r.get("GetEaxMdResult", {}).get("meta", {}).get("chart", {})


def get_grid_meta(req_ctx, eax_id: str) -> dict:
    r = pp(req_ctx, {"GetEaxMd": {"tEax": {"id": eax_id}, "tArg": {"pattern": {"grid": True}}}})
    return r.get("GetEaxMdResult", {}).get("meta", {}).get("grid", {})


def save_debug(page, name: str) -> None:
    page.screenshot(path=str(OUT / f"{name}.png"), full_page=True)


def main() -> None:
    log: list[dict] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1600, "height": 900})
        page = ctx.new_page()

        state: dict = {"root_id": None, "adhoc_id": None, "cube_keys": [], "eax_ids": [],
                       "metabase_id": None}

        def on_req(req):
            p = req.post_data or ""
            if state["root_id"] is None and "!M!Root" in p:
                m = re.search(r"([A-Z0-9]+!M!Root)", p)
                if m:
                    state["root_id"] = m.group(1)
                    state["metabase_id"] = m.group(1).replace("!M!Root", "!M")
            if state["adhoc_id"] is None and '"tAdHocId":{"id":"' in p:
                m = re.search(r'"tAdHocId":\{"id":"([^"]+)"\}', p)
                if m:
                    state["adhoc_id"] = m.group(1)
            cm = re.search(r'"cube":\{"obDesc":\{.*?"k":(\d+)', p)
            if cm:
                k = int(cm.group(1))
                if k not in state["cube_keys"]:
                    state["cube_keys"].append(k)
            em = re.search(r'"tEax":\{"id":"([^"]+!DSO![^"]+)"\}', p)
            if em:
                eid = em.group(1)
                if eid not in state["eax_ids"]:
                    state["eax_ids"].append(eid)

        page.on("request", on_req)

        # Login
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

        # INSERT > Chart
        try:
            page.locator("#InsertCategory").click(timeout=5000)
        except Exception:
            page.mouse.click(220, 42)
        page.wait_for_timeout(500)
        page.mouse.click(286, 98)
        page.wait_for_timeout(700)
        page.mouse.click(290, 154)
        page.wait_for_timeout(3000)

        # Data import
        csv_path = OUT / "probe.csv"
        csv_path.write_text(CSV_DATA, encoding="utf-8")

        try:
            page.get_by_text("Data import", exact=False).last.click(timeout=5000, force=True)
        except Exception:
            log.append({"ABORT": "import not found"})
            browser.close()
            return
        page.wait_for_timeout(2000)

        try:
            page.get_by_text("File with data", exact=False).click(timeout=5000, force=True)
        except Exception:
            page.mouse.click(585, 450)
        page.wait_for_timeout(1000)

        try:
            with page.expect_file_chooser(timeout=5000) as fc_info:
                page.get_by_text("Next >", exact=True).last.click(timeout=5000, force=True)
            fc_info.value.set_files(str(csv_path))
        except Exception:
            try:
                page.get_by_text("Next >", exact=True).last.click(timeout=5000, force=True)
            except Exception:
                page.mouse.click(970, 727)
            page.wait_for_timeout(3000)
            with page.expect_file_chooser(timeout=5000) as fc_info:
                page.get_by_text("Browse", exact=False).click(timeout=5000, force=True)
            fc_info.value.set_files(str(csv_path))

        page.wait_for_timeout(5000)
        try:
            page.get_by_text("Next >", exact=True).last.click(timeout=5000, force=True)
        except Exception:
            page.mouse.click(970, 727)
        page.wait_for_timeout(5000)
        try:
            page.get_by_text("Import", exact=True).last.click(timeout=5000, force=True)
        except Exception:
            page.mouse.click(1055, 727)
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

        cube_key = max(state["cube_keys"]) if state["cube_keys"] else None
        eax_id = state["eax_ids"][0] if state["eax_ids"] else None
        metabase_id = state["metabase_id"]
        adhoc_id = state["adhoc_id"]
        log.append({"step": "post_import", "cube_key": cube_key, "eax_id": eax_id})

        if not eax_id or not cube_key:
            log.append({"ABORT": "no eax_id or cube_key"})
            (OUT / "log.json").write_text(json.dumps(log, indent=2, ensure_ascii=False))
            browser.close()
            return

        # ── Baseline chart meta ───────────────────────────────────────────────
        chart0 = get_chart_meta(ctx.request, eax_id)
        log.append({"step": "chart_meta_baseline",
                    "dataRange": chart0.get("dataRange"),
                    "seriesInRows": chart0.get("seriesInRows"),
                    "singleCellRangeMode": chart0.get("singleCellRangeMode"),
                    "timeLineDimension": chart0.get("timeLineDimension"),
                    "objectivesDimension": chart0.get("objectivesDimension")})

        # ── Get dim keys via OpenCube ─────────────────────────────────────────
        cube_obj_id = f"{metabase_id}!{cube_key}"
        try:
            r_oc = pp(ctx.request, {"OpenCube": {"tOb": {"id": cube_obj_id}, "tArg": {
                "args": {}, "metaGet": {"dims": "Get", "dim": {"settings": True}}
            }}})
            dims = r_oc.get("OpenCubeResult", {}).get("meta", {}).get("dims", {}).get("its", {}).get("it", [])
            facts_k = next((d["k"] for d in dims if d.get("settings", {}).get("standard", {}).get("isFacts")), None)
            category_k = next((d["k"] for d in dims if not d.get("settings", {}).get("standard", {}).get("isFacts")), None)
            log.append({"step": "OpenCube_dims", "facts_k": facts_k, "category_k": category_k, "dims": dims})
        except Exception as e:
            log.append({"step": "OpenCube_dims", "error": str(e)})
            facts_k = None
            category_k = None

        # ── Try grid expansion methods ────────────────────────────────────────

        # Method 1: Set grid to show all dims expanded (all elements selected)
        try:
            r_g1 = pp(ctx.request, {"SetEaxMd": {"tEax": {"id": eax_id}, "tArg": {
                "pattern": {"grid": True},
                "meta": {"grid": {
                    "expandAllDims": True,
                    "showAllElements": True,
                }},
                "refresh": {"fetchData": True, "grid": True, "chart": True, "saveData": False},
                "metaGet": {"chart": True, "grid": True},
            }}})
            inner = r_g1.get("SetEaxMdResult", {})
            meta = inner.get("meta", {})
            log.append({"step": "grid_expandAll",
                        "chart_dataRange": meta.get("chart", {}).get("dataRange"),
                        "grid_snippet": str(meta.get("grid", {}))[:300]})
        except Exception as e:
            log.append({"step": "grid_expandAll", "error": str(e)})

        chart1 = get_chart_meta(ctx.request, eax_id)
        log.append({"step": "chart_after_expandAll",
                    "dataRange": chart1.get("dataRange"),
                    "singleCellRangeMode": chart1.get("singleCellRangeMode")})

        # Method 2: Use SetEaxMd with "dataSources" SelectAll
        if facts_k and category_k:
            try:
                r_g2 = pp(ctx.request, {"SetEaxMd": {"tEax": {"id": eax_id}, "tArg": {
                    "pattern": {"dims": {"its": {"it": [{"k": facts_k}, {"k": category_k}]}}},
                    "meta": {"dims": {"its": {"it": [
                        {"k": facts_k, "allSelected": True},
                        {"k": category_k, "allSelected": True},
                    ]}}},
                    "refresh": {"fetchData": True, "grid": True, "chart": True, "saveData": False},
                    "metaGet": {"chart": True},
                }}})
                meta2 = r_g2.get("SetEaxMdResult", {}).get("meta", {})
                log.append({"step": "dims_allSelected",
                            "chart_dataRange": meta2.get("chart", {}).get("dataRange"),
                            "snippet": str(r_g2)[:400]})
            except Exception as e:
                log.append({"step": "dims_allSelected", "error": str(e)})

        chart2 = get_chart_meta(ctx.request, eax_id)
        log.append({"step": "chart_after_allSelected",
                    "dataRange": chart2.get("dataRange"),
                    "singleCellRangeMode": chart2.get("singleCellRangeMode")})

        # Method 3: SetEaxMd chart with range spanning full data
        try:
            r_g3 = pp(ctx.request, {"SetEaxMd": {"tEax": {"id": eax_id}, "tArg": {
                "pattern": {"chart": True},
                "meta": {"chart": {
                    "range": {"left": 1, "top": 1, "width": 100, "height": 100},
                    "dataRange": {
                        "left": 1, "top": 1, "width": 100, "height": 100,
                        "type": "EntireGrid",
                    },
                }},
                "refresh": {"fetchData": True, "chart": True, "saveData": False},
                "metaGet": {"chart": True},
            }}})
            meta3 = r_g3.get("SetEaxMdResult", {}).get("meta", {})
            log.append({"step": "chart_range_100x100",
                        "dataRange": meta3.get("chart", {}).get("dataRange"),
                        "snippet": str(r_g3)[:400]})
        except Exception as e:
            log.append({"step": "chart_range_100x100", "error": str(e)})

        chart3 = get_chart_meta(ctx.request, eax_id)
        log.append({"step": "chart_after_range_100x100",
                    "dataRange": chart3.get("dataRange")})

        # Method 4: SetEaxMd timeLineDimension / objectivesDimension swap
        # From GetEaxMd: timeLineDimension=FACTS (X-axis), objectivesDimension=CATEGORY (series)
        # We want: timeLineDimension=CATEGORY (X-axis), objectivesDimension=FACTS or none
        if facts_k and category_k:
            try:
                r_g4 = pp(ctx.request, {"SetEaxMd": {"tEax": {"id": eax_id}, "tArg": {
                    "pattern": {"chart": True},
                    "meta": {"chart": {
                        "timeLineDimension": {"k": category_k},
                        "objectivesDimension": {"k": facts_k},
                    }},
                    "refresh": {"fetchData": True, "chart": True, "saveData": False},
                    "metaGet": {"chart": True},
                }}})
                meta4 = r_g4.get("SetEaxMdResult", {}).get("meta", {})
                chart4 = meta4.get("chart", {})
                log.append({"step": "swap_timeline_objectives",
                            "timeLineDimension": chart4.get("timeLineDimension"),
                            "objectivesDimension": chart4.get("objectivesDimension"),
                            "dataRange": chart4.get("dataRange"),
                            "snippet": str(r_g4)[:600]})
            except Exception as e:
                log.append({"step": "swap_timeline_objectives", "error": str(e)})

        chart4b = get_chart_meta(ctx.request, eax_id)
        log.append({"step": "chart_after_swap",
                    "timeLineDimension": chart4b.get("timeLineDimension"),
                    "objectivesDimension": chart4b.get("objectivesDimension"),
                    "dataRange": chart4b.get("dataRange"),
                    "seriesInRows": chart4b.get("seriesInRows")})

        # Method 5: seriesInRows = false
        try:
            r_g5 = pp(ctx.request, {"SetEaxMd": {"tEax": {"id": eax_id}, "tArg": {
                "pattern": {"chart": True},
                "meta": {"chart": {"seriesInRows": False}},
                "refresh": {"fetchData": True, "chart": True, "saveData": False},
                "metaGet": {"chart": True},
            }}})
            meta5 = r_g5.get("SetEaxMdResult", {}).get("meta", {})
            log.append({"step": "seriesInRows_false",
                        "seriesInRows": meta5.get("chart", {}).get("seriesInRows"),
                        "dataRange": meta5.get("chart", {}).get("dataRange")})
        except Exception as e:
            log.append({"step": "seriesInRows_false", "error": str(e)})

        # Method 6: switch to grid (table) mode — grid might expand to show all data, then switch back
        try:
            # Switch to grid
            pp(ctx.request, {"SetEaxMd": {"tEax": {"id": eax_id}, "tArg": {
                "pattern": {"chart": True, "grid": True},
                "meta": {
                    "chart": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
                    "grid": {"enabled": True, "visible": True, "active": True, "viewOrder": 0},
                },
                "metaGet": {"chart": True, "grid": True},
            }}})
            page.wait_for_timeout(1000)
            grid_m = get_grid_meta(ctx.request, eax_id)
            log.append({"step": "grid_mode_activated", "grid_snippet": str(grid_m)[:400]})

            # Try expand all in grid
            pp(ctx.request, {"SetEaxMd": {"tEax": {"id": eax_id}, "tArg": {
                "pattern": {"grid": True},
                "meta": {"grid": {"expandAllDims": True}},
                "refresh": {"fetchData": True, "grid": True, "saveData": False},
                "metaGet": {"grid": True},
            }}})
            page.wait_for_timeout(1000)

            # Switch back to chart
            pp(ctx.request, {"SetEaxMd": {"tEax": {"id": eax_id}, "tArg": {
                "pattern": {"chart": True, "grid": True},
                "meta": {
                    "chart": {"enabled": True, "visible": True, "active": True, "viewOrder": 0},
                    "grid": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
                },
                "refresh": {"fetchData": True, "chart": True, "saveData": False},
                "metaGet": {"chart": True},
            }}})
            page.wait_for_timeout(1000)

            chart6 = get_chart_meta(ctx.request, eax_id)
            log.append({"step": "chart_after_grid_expand_and_back",
                        "dataRange": chart6.get("dataRange"),
                        "singleCellRangeMode": chart6.get("singleCellRangeMode")})
        except Exception as e:
            log.append({"step": "grid_expand_and_back", "error": str(e)})

        # ── SaveObjectAs and screenshot ───────────────────────────────────────
        uid = str(int(time.time()))[-6:]
        obj_id = f"DA_GRID_EXP_{uid}"
        try:
            r_save = pp(ctx.request, {"SaveObjectAs": {
                "tObject": {"id": adhoc_id},
                "tArg": {"destination": {
                    "operation": "CreateNew",
                    "create": {
                        "name": f"Grid Expand Probe {uid}",
                        "id": obj_id,
                        "parent": {"i": "", "n": "", "k": 0, "c": 0, "p": 0, "h": False},
                        "permanent": True,
                    },
                    "keepMoniker": True,
                }},
            }})
            ob = (r_save.get("SaveObjectAsResult", {}).get("object") or
                  r_save.get("tResult", {}).get("ob") or {})
            saved_key = ob.get("k") or ob.get("key")
            log.append({"step": "SaveObjectAs", "saved_key": saved_key})
        except Exception as e:
            saved_key = None
            log.append({"step": "SaveObjectAs", "error": str(e)})

        if saved_key:
            view_url = f"{BASE}/app/dashboard.html#key={saved_key}&mode=view&name=Dashboard&repo={REPO}"
            try:
                page.goto(view_url, wait_until="networkidle", timeout=60000)
                page.wait_for_timeout(5000)
            except Exception:
                pass
        save_debug(page, "final_view")

        # Final chart meta check
        try:
            chart_final = get_chart_meta(ctx.request, eax_id)
            log.append({"step": "chart_meta_final",
                        "dataRange": chart_final.get("dataRange"),
                        "timeLineDimension": chart_final.get("timeLineDimension"),
                        "objectivesDimension": chart_final.get("objectivesDimension"),
                        "seriesInRows": chart_final.get("seriesInRows")})
        except Exception as e:
            log.append({"step": "chart_meta_final", "error": str(e)})

        browser.close()

    (OUT / "log.json").write_text(json.dumps(log, indent=2, ensure_ascii=False))
    print(json.dumps({"out": str(OUT), "steps": len(log)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
