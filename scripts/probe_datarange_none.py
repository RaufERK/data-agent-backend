#!/usr/bin/env python3
"""
Try setting dataRange type "None" (0,0,0,0) to match the working dashboard.
Also test timeLineDimension swap + seriesInRows.
Save and screenshot.
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
OUT = Path("/tmp/probe_datarange_none")
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
        raise RuntimeError(f"PPService {r.status}: {r.text()[:1200]}")
    return json.loads(r.text())


def get_chart_meta(req_ctx, eax_id: str) -> dict:
    r = pp(req_ctx, {"GetEaxMd": {"tEax": {"id": eax_id}, "tArg": {"pattern": {"chart": True}}}})
    return r.get("GetEaxMdResult", {}).get("meta", {}).get("chart", {})


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

        # ── Baseline ─────────────────────────────────────────────────────────
        chart0 = get_chart_meta(ctx.request, eax_id)
        log.append({"step": "baseline",
                    "dataRange": chart0.get("dataRange"),
                    "singleCellRangeMode": chart0.get("singleCellRangeMode"),
                    "timeLineDimension": chart0.get("timeLineDimension")})

        # ── Get dim keys ──────────────────────────────────────────────────────
        cube_obj_id = f"{metabase_id}!{cube_key}"
        r_oc = pp(ctx.request, {"OpenCube": {"tOb": {"id": cube_obj_id}, "tArg": {
            "args": {}, "metaGet": {"dims": "Get", "dim": {"settings": True}}
        }}})
        dims = r_oc.get("OpenCubeResult", {}).get("meta", {}).get("dims", {}).get("its", {}).get("it", [])
        facts_k = next((d["k"] for d in dims if d.get("settings", {}).get("standard", {}).get("isFacts")), None)
        category_k = next((d["k"] for d in dims if not d.get("settings", {}).get("standard", {}).get("isFacts")), None)
        log.append({"step": "dim_keys", "facts_k": facts_k, "category_k": category_k})

        # ── Try dataRange type "None" {0,0,0,0} ──────────────────────────────
        for dr_name, dr_val in [
            ("None_type", {"left": 0, "top": 0, "width": 0, "height": 0, "type": "None"}),
            ("EntireGrid_type", {"left": 0, "top": 0, "width": 0, "height": 0, "type": "EntireGrid"}),
            ("Cells_0", {"left": 0, "top": 0, "width": 0, "height": 0, "type": "Cells"}),
            ("MultiPart_0", {"left": 0, "top": 0, "width": 0, "height": 0, "type": "MultiPart"}),
        ]:
            try:
                r = pp(ctx.request, {"SetEaxMd": {"tEax": {"id": eax_id}, "tArg": {
                    "pattern": {"chart": True},
                    "meta": {"chart": {"dataRange": dr_val}},
                    "refresh": {"fetchData": True, "chart": True, "saveData": False},
                    "metaGet": {"chart": True},
                }}})
                inner_chart = r.get("SetEaxMdResult", {}).get("meta", {}).get("chart", {})
                log.append({"step": f"set_dataRange_{dr_name}",
                            "dataRange_result": inner_chart.get("dataRange"),
                            "singleCellRangeMode": inner_chart.get("singleCellRangeMode")})
            except Exception as e:
                log.append({"step": f"set_dataRange_{dr_name}", "error": str(e)[:300]})

        chart1 = get_chart_meta(ctx.request, eax_id)
        log.append({"step": "after_dataRange_attempts",
                    "dataRange": chart1.get("dataRange"),
                    "singleCellRangeMode": chart1.get("singleCellRangeMode")})

        # ── Combined: singleCellRangeMode + dataRange None + timeline swap ───
        if facts_k and category_k:
            try:
                r_combo = pp(ctx.request, {"SetEaxMd": {"tEax": {"id": eax_id}, "tArg": {
                    "pattern": {"chart": True},
                    "meta": {"chart": {
                        "singleCellRangeMode": "EntireGrid",
                        "dataRange": {"left": 0, "top": 0, "width": 0, "height": 0, "type": "None"},
                        "timeLineDimension": {"k": category_k},
                        "objectivesDimension": {"k": facts_k},
                        "seriesInRows": True,
                    }},
                    "refresh": {"fetchData": True, "chart": True, "saveData": False},
                    "metaGet": {"chart": True},
                }}})
                inner = r_combo.get("SetEaxMdResult", {}).get("meta", {}).get("chart", {})
                log.append({"step": "combo_set",
                            "dataRange": inner.get("dataRange"),
                            "singleCellRangeMode": inner.get("singleCellRangeMode"),
                            "timeLineDimension": inner.get("timeLineDimension"),
                            "objectivesDimension": inner.get("objectivesDimension")})
            except Exception as e:
                log.append({"step": "combo_set", "error": str(e)[:300]})

        chart2 = get_chart_meta(ctx.request, eax_id)
        log.append({"step": "after_combo",
                    "dataRange": chart2.get("dataRange"),
                    "singleCellRangeMode": chart2.get("singleCellRangeMode"),
                    "timeLineDimension": chart2.get("timeLineDimension"),
                    "objectivesDimension": chart2.get("objectivesDimension"),
                    "seriesInRows": chart2.get("seriesInRows")})

        # ── Add layout so we can see the chart in view ────────────────────────
        dso_id = eax_id.split("!DSO!")[-1]
        slide_key = "SL001"
        try:
            pp(ctx.request, {"SetAdHoc": {"tAdHocId": {"id": adhoc_id}, "tArg": {
                "meta": {"Md8": {
                    "activeSlideKey": 1,
                    "slides": {"its": {"it": [{
                        "key": 1,
                        "mainPanel": {"block": {
                            "@type": "Slide", "@key": slide_key,
                            "prop": [
                                {"@tag": "name", "@val": "Slide 1"},
                                {"@tag": "background", "prop": [
                                    {"@tag": "useBackground", "@val": "1"},
                                    {"@tag": "backgroundColor", "@val": "#f4f4f4"},
                                    {"@tag": "useGradient", "@val": "0"},
                                    {"@tag": "gradientColor", "@val": "#c9c9c9"},
                                    {"@tag": "gradientAngle", "@val": "270"},
                                ]},
                                {"@tag": "margins", "prop": {"@tag": "useMargins", "@val": "0"}},
                                {"@tag": "interactivity", "@val": "1"},
                                {"@tag": "decor", "prop": {"@tag": "paddings", "prop": [
                                    {"@tag": "usePaddings", "@val": "0"},
                                    {"@tag": "left", "@val": "10"},
                                    {"@tag": "right", "@val": "10"},
                                    {"@tag": "top", "@val": "10"},
                                    {"@tag": "bottom", "@val": "10"},
                                ]}},
                                {"@tag": "layouts", "area": [{
                                    "@key": dso_id,
                                    "block": {
                                        "@type": "Chart",
                                        "@key": dso_id,
                                        "prop": [
                                            {"@tag": "name", "@val": "Revenue Chart"},
                                            {"@tag": "background", "prop": [
                                                {"@tag": "useBackground", "@val": "1"},
                                                {"@tag": "backgroundColor", "@val": "#ffffff"},
                                                {"@tag": "useGradient", "@val": "0"},
                                                {"@tag": "gradientColor", "@val": "#c9c9c9"},
                                                {"@tag": "gradientAngle", "@val": "270"},
                                            ]},
                                            {"@tag": "layout", "prop": [
                                                {"@tag": "left", "@val": "5.00"},
                                                {"@tag": "right", "@val": "5.00"},
                                                {"@tag": "top", "@val": "5.00"},
                                                {"@tag": "bottom", "@val": "5.00"},
                                                {"@tag": "leftUnit", "@val": "%"},
                                                {"@tag": "rightUnit", "@val": "%"},
                                                {"@tag": "topUnit", "@val": "%"},
                                                {"@tag": "bottomUnit", "@val": "%"},
                                                {"@tag": "anchorLeft", "@val": "1"},
                                                {"@tag": "anchorTop", "@val": "1"},
                                                {"@tag": "anchorRight", "@val": "1"},
                                                {"@tag": "anchorBottom", "@val": "1"},
                                            ]},
                                            {"@tag": "margins", "prop": {"@tag": "useMargins", "@val": "1"}},
                                            {"@tag": "interactivity", "@val": "1"},
                                            {"@tag": "decor", "prop": [
                                                {"@tag": "cornerRadius", "@val": "5"},
                                                {"@tag": "useBorderRadius", "@val": "1"},
                                                {"@tag": "useBorder", "@val": "0"},
                                                {"@tag": "useShadow", "@val": "0"},
                                                {"@tag": "paddings", "prop": [
                                                    {"@tag": "usePaddings", "@val": "1"},
                                                    {"@tag": "left", "@val": "10"},
                                                    {"@tag": "right", "@val": "10"},
                                                    {"@tag": "top", "@val": "10"},
                                                    {"@tag": "bottom", "@val": "10"},
                                                ]},
                                            ]},
                                            {"@tag": "title", "prop": [
                                                {"@tag": "show", "@val": "1"},
                                                {"@tag": "font", "prop": [
                                                    {"@tag": "color", "@val": "#48494c"},
                                                    {"@tag": "family", "@val": "Arial"},
                                                    {"@tag": "isBold", "@val": "1"},
                                                    {"@tag": "size", "@val": "13"},
                                                ]},
                                                {"@tag": "align", "@val": "Left"},
                                            ]},
                                        ],
                                    },
                                }]},
                            ],
                        }},
                    }]}},
                }},
                "pattern": {"layout": {"activeSlideKey": True, "slides": "Change"}},
            }}})
            log.append({"step": "layout_set", "ok": True})
        except Exception as e:
            log.append({"step": "layout_set", "error": str(e)[:300]})

        # ── SaveObjectAs ──────────────────────────────────────────────────────
        uid = str(int(time.time()))[-6:]
        obj_id = f"DA_DR_NONE_{uid}"
        try:
            r_save = pp(ctx.request, {"SaveObjectAs": {
                "tObject": {"id": adhoc_id},
                "tArg": {"destination": {
                    "operation": "CreateNew",
                    "create": {
                        "name": f"DataRange None Test {uid}",
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
            log.append({"step": "SaveObjectAs", "saved_key": saved_key, "obj_id": obj_id})
        except Exception as e:
            saved_key = None
            log.append({"step": "SaveObjectAs", "error": str(e)[:300]})

        # ── View + screenshot ─────────────────────────────────────────────────
        if saved_key:
            view_url = f"{BASE}/app/dashboard.html#key={saved_key}&mode=view&name=Dashboard&repo={REPO}"
            try:
                page.goto(view_url, wait_until="networkidle", timeout=60000)
                page.wait_for_timeout(5000)
            except Exception:
                pass
        page.screenshot(path=str(OUT / "final_view.png"), full_page=True)

        # ── Final chart meta (in saved context) ──────────────────────────────
        try:
            chart_final = get_chart_meta(ctx.request, eax_id)
            log.append({"step": "chart_meta_final",
                        "dataRange": chart_final.get("dataRange"),
                        "singleCellRangeMode": chart_final.get("singleCellRangeMode"),
                        "timeLineDimension": chart_final.get("timeLineDimension"),
                        "objectivesDimension": chart_final.get("objectivesDimension"),
                        "seriesInRows": chart_final.get("seriesInRows")})
        except Exception as e:
            log.append({"step": "chart_meta_final", "error": str(e)[:200]})

        browser.close()

    (OUT / "log.json").write_text(json.dumps(log, indent=2, ensure_ascii=False))
    print(json.dumps({"out": str(OUT), "steps": len(log)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
