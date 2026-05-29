#!/usr/bin/env python3
"""
Probe: after creating an API DSO with a cube bound, try selecting specific
FACTS elements (metric columns) via SetEaxMd.

Approach: create DSO via API → bind cube → try element selection patterns.
The cube has 3 columns: category (dim), revenue (fact), profit (fact).
We want DSO1 to show only revenue, DSO2 to show only profit.
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
OUT = Path("/tmp/probe_facts_sel")
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


def random_id() -> str:
    import random, string
    return "S" + "".join(random.choices(string.ascii_uppercase + string.digits, k=15))


def make_api_dso(req_ctx, base_url: str, adhoc_id: str, cube_key: int, chart_type: str = "bar") -> tuple[str, str]:
    """Create an API-based DSO and bind it to cube_key. Returns (dso_id, eax_id)."""
    dso_id = random_id()
    pp(req_ctx, {"SetAdHoc": {"tAdHocId": {"id": adhoc_id}, "tArg": {
        "meta": {"dataSourceObjects": {"its": {"it": [
            {"createNew": 2561, "id": dso_id, "slideKey": "1"}
        ]}}},
        "pattern": {"dataSourceObjects": "Add"},
        "metaGet": {"dataSourceObjects": "Get"},
    }}})
    eax_id = f"{adhoc_id}!DSO!{dso_id}"

    if chart_type != "table":
        # Set chart mode
        hi_type = {"bar": "column", "line": "line"}.get(chart_type, "column")
        pp(req_ctx, {"SetEaxMd": {"tEax": {"id": eax_id}, "tArg": {
            "pattern": {"setChart": {"meta": {"hiChart": json.dumps(
                {"chart": {"defaultSeriesType": hi_type}, "plotOptions": {"series": {}}, "template": None},
                ensure_ascii=False)}}},
            "meta": {},
        }}})
        pp(req_ctx, {"SetEaxMd": {"tEax": {"id": eax_id}, "tArg": {
            "pattern": {"chart": True, "grid": True, "speedometer": True},
            "meta": {
                "chart": {"enabled": True, "visible": True, "active": True, "viewOrder": 0},
                "grid": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
                "speedometer": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
                "bubbleChart": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
                "bubbleTree": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
                "treeMap": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
                "mapChart": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
            },
            "metaGet": {"chart": True, "grid": True, "speedometer": True},
        }}})

    # Bind cube
    r_bind = pp(req_ctx, {"SetEaxMd": {"tEax": {"id": eax_id}, "tArg": {
        "pattern": {"dataSources": "Set"},
        "meta": {"dataSources": {
            "its": {"it": [{"k": 0, "vis": True, "cube": {
                "obDesc": {"n": "", "i": "", "k": cube_key, "c": 0}
            }}]},
            "OpenOptions": "DataAndSelection",
        }},
        "refresh": {"fetchData": True, "map": True, "grid": True, "chart": True, "speedometer": True, "saveData": False},
        "metaGet": {"chart": True, "grid": True, "speedometer": True, "dataSources": "Get"},
    }}})

    return dso_id, eax_id


def main() -> None:
    log: list[dict] = []
    responses: list[dict] = []

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

        def on_resp(resp):
            if "PPService.axd" in resp.url:
                try:
                    b = resp.json()
                    if "SetEaxMd" in str(list(b.keys())):
                        pass  # too many
                    responses.append({"keys": list(b.keys()), "snippet": str(b)[:400]})
                except Exception:
                    pass

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
        log.append({"step": "init", "adhoc_id": state["adhoc_id"]})

        # Data import (to create the cube)
        try:
            page.locator("#InsertCategory").click(timeout=5000)
        except Exception:
            page.mouse.click(220, 42)
        page.wait_for_timeout(500)
        page.mouse.click(286, 98)
        page.wait_for_timeout(700)
        page.mouse.click(290, 154)
        page.wait_for_timeout(3000)

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
        metabase_id = state["metabase_id"]
        adhoc_id = state["adhoc_id"]
        log.append({"step": "post_import", "cube_key": cube_key})

        if not cube_key:
            log.append({"ABORT": "no cube_key"})
            (OUT / "log.json").write_text(json.dumps(log, indent=2, ensure_ascii=False))
            browser.close()
            return

        # ── Get dim keys via OpenCube ─────────────────────────────────────────
        cube_obj_id = f"{metabase_id}!{cube_key}"
        r_oc = pp(ctx.request, {"OpenCube": {"tOb": {"id": cube_obj_id}, "tArg": {
            "args": {}, "metaGet": {"dims": "Get", "dim": {"settings": True, "elems": True}}
        }}})
        cube_meta = r_oc.get("OpenCubeResult", {}).get("meta", {})
        dims = cube_meta.get("dims", {}).get("its", {}).get("it", [])
        facts_k = next((d["k"] for d in dims if d.get("settings", {}).get("standard", {}).get("isFacts")), None)
        category_k = next((d["k"] for d in dims if not d.get("settings", {}).get("standard", {}).get("isFacts")), None)
        log.append({"step": "dims", "facts_k": facts_k, "category_k": category_k,
                    "cube_meta_keys": list(cube_meta.keys())})

        # ── Try GetEaxEl to get FACTS elements ────────────────────────────────
        # The UI-created DSO eax_id from import
        ui_eax_ids = [eid for eid in state.get("eax_ids", []) if "!DSO!" in eid] if hasattr(state, 'get') else []
        # Actually we need to get it from state
        # Let's create an API DSO instead and check its dataSources
        dso1_id, eax1_id = make_api_dso(ctx.request, BASE, adhoc_id, cube_key, "bar")
        log.append({"step": "dso1_created", "dso_id": dso1_id, "eax_id": eax1_id[-20:]})

        # Get chart meta for dso1
        cm1 = get_chart_meta(ctx.request, eax1_id)
        log.append({"step": "dso1_chart_meta",
                    "dataRange": cm1.get("dataRange"),
                    "timeLineDimension": cm1.get("timeLineDimension"),
                    "objectivesDimension": cm1.get("objectivesDimension"),
                    "singleCellRangeMode": cm1.get("singleCellRangeMode")})

        # ── Try GetEaxEl on eax1 to see FACTS elements ────────────────────────
        for call_name, call_body in [
            ("GetEaxEl", {"GetEaxEl": {"tEax": {"id": eax1_id}, "tArg": {
                "dim": {"k": facts_k},
                "pattern": {"elems": "Get", "count": True},
            }}}),
            ("GetEaxEl_all", {"GetEaxEl": {"tEax": {"id": eax1_id}, "tArg": {
                "dim": {"k": facts_k},
                "pattern": {"elems": "GetAll"},
            }}}),
        ]:
            try:
                r = pp(ctx.request, call_body)
                log.append({"step": call_name, "response_keys": list(r.keys()),
                            "snippet": str(r)[:500]})
            except Exception as e:
                log.append({"step": call_name, "error": str(e)[:300]})

        # ── Try SetEaxEl to select only "revenue" element ─────────────────────
        # Elements in FACTS dimension correspond to columns: "revenue", "profit"
        # Try element name-based selection
        for sel_name, sel_body in [
            ("select_by_name_revenue", {"SetEaxMd": {"tEax": {"id": eax1_id}, "tArg": {
                "pattern": {"dims": {"its": {"it": [{"k": facts_k}]}}},
                "meta": {"dims": {"its": {"it": [{"k": facts_k, "elems": {"its": {"it": [
                    {"n": "revenue", "st": 1},
                ]}}}]}}},
                "refresh": {"fetchData": True, "chart": True, "saveData": False},
                "metaGet": {"chart": True},
            }}}),
            ("select_by_n_revenue_deselect_others", {"SetEaxMd": {"tEax": {"id": eax1_id}, "tArg": {
                "pattern": {"dims": {"its": {"it": [{"k": facts_k}]}}},
                "meta": {"dims": {"its": {"it": [{"k": facts_k, "elems": {"its": {"it": [
                    {"n": "revenue", "st": 1},
                    {"n": "profit", "st": 0},
                ]}}}]}}},
                "refresh": {"fetchData": True, "chart": True, "saveData": False},
                "metaGet": {"chart": True},
            }}}),
            ("deselectAll_then_revenue", {"SetEaxMd": {"tEax": {"id": eax1_id}, "tArg": {
                "pattern": {"dims": {"its": {"it": [{"k": facts_k}]}}},
                "meta": {"dims": {"its": {"it": [{"k": facts_k,
                    "deselect": True,
                    "elems": {"its": {"it": [{"n": "revenue", "st": 1}]}},
                }]}}},
                "refresh": {"fetchData": True, "chart": True, "saveData": False},
                "metaGet": {"chart": True},
            }}}),
        ]:
            try:
                r = pp(ctx.request, sel_body)
                inner = r.get("SetEaxMdResult", {}).get("meta", {}).get("chart", {})
                log.append({"step": sel_name,
                            "dataRange": inner.get("dataRange"),
                            "timeLineDimension": inner.get("timeLineDimension"),
                            "snippet": str(r)[:400]})
            except Exception as e:
                log.append({"step": sel_name, "error": str(e)[:300]})

        # Create dso2 for profit
        dso2_id, eax2_id = make_api_dso(ctx.request, BASE, adhoc_id, cube_key, "line")
        log.append({"step": "dso2_created", "dso_id": dso2_id})

        # Try SetEaxMd for dso2 to select only "profit"
        try:
            r = pp(ctx.request, {"SetEaxMd": {"tEax": {"id": eax2_id}, "tArg": {
                "pattern": {"dims": {"its": {"it": [{"k": facts_k}]}}},
                "meta": {"dims": {"its": {"it": [{"k": facts_k, "elems": {"its": {"it": [
                    {"n": "profit", "st": 1},
                    {"n": "revenue", "st": 0},
                ]}}}]}}},
                "refresh": {"fetchData": True, "chart": True, "saveData": False},
                "metaGet": {"chart": True},
            }}})
            inner2 = r.get("SetEaxMdResult", {}).get("meta", {}).get("chart", {})
            log.append({"step": "dso2_select_profit",
                        "dataRange": inner2.get("dataRange"),
                        "timeLineDimension": inner2.get("timeLineDimension")})
        except Exception as e:
            log.append({"step": "dso2_select_profit", "error": str(e)[:300]})

        # ── Set timeline dims for both DSOs ───────────────────────────────────
        for eax_id, name in [(eax1_id, "dso1"), (eax2_id, "dso2")]:
            if facts_k and category_k:
                try:
                    pp(ctx.request, {"SetEaxMd": {"tEax": {"id": eax_id}, "tArg": {
                        "pattern": {"chart": True},
                        "meta": {"chart": {
                            "timeLineDimension": {"k": category_k},
                            "objectivesDimension": {"k": facts_k},
                            "seriesInRows": True,
                        }},
                        "refresh": {"fetchData": True, "chart": True, "saveData": False},
                        "metaGet": {"chart": True},
                    }}})
                    log.append({"step": f"{name}_timeline_set", "ok": True})
                except Exception as e:
                    log.append({"step": f"{name}_timeline_set", "error": str(e)[:200]})

        # ── Layout both DSOs ──────────────────────────────────────────────────
        slide_key = random_id()
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
                                {"@tag": "layouts", "area": [
                                    {
                                        "@key": dso1_id,
                                        "block": {
                                            "@type": "Chart", "@key": dso1_id,
                                            "prop": [
                                                {"@tag": "name", "@val": "Revenue"},
                                                {"@tag": "background", "prop": [
                                                    {"@tag": "useBackground", "@val": "1"},
                                                    {"@tag": "backgroundColor", "@val": "#ffffff"},
                                                    {"@tag": "useGradient", "@val": "0"},
                                                    {"@tag": "gradientColor", "@val": "#c9c9c9"},
                                                    {"@tag": "gradientAngle", "@val": "270"},
                                                ]},
                                                {"@tag": "layout", "prop": [
                                                    {"@tag": "left", "@val": "2.00"},
                                                    {"@tag": "right", "@val": "51.00"},
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
                                    },
                                    {
                                        "@key": dso2_id,
                                        "block": {
                                            "@type": "Chart", "@key": dso2_id,
                                            "prop": [
                                                {"@tag": "name", "@val": "Profit"},
                                                {"@tag": "background", "prop": [
                                                    {"@tag": "useBackground", "@val": "1"},
                                                    {"@tag": "backgroundColor", "@val": "#ffffff"},
                                                    {"@tag": "useGradient", "@val": "0"},
                                                    {"@tag": "gradientColor", "@val": "#c9c9c9"},
                                                    {"@tag": "gradientAngle", "@val": "270"},
                                                ]},
                                                {"@tag": "layout", "prop": [
                                                    {"@tag": "left", "@val": "51.00"},
                                                    {"@tag": "right", "@val": "2.00"},
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
                                    },
                                ]},
                            ],
                        }},
                    }]}},
                }},
                "pattern": {"layout": {"activeSlideKey": True, "slides": "Change"}},
            }}})
            log.append({"step": "layout_set", "ok": True})
        except Exception as e:
            log.append({"step": "layout_set", "error": str(e)[:200]})

        # ── SaveObjectAs ──────────────────────────────────────────────────────
        uid = str(int(time.time()))[-6:]
        obj_id = f"DA_FACTS_SEL_{uid}"
        try:
            r_save = pp(ctx.request, {"SaveObjectAs": {
                "tObject": {"id": adhoc_id},
                "tArg": {"destination": {
                    "operation": "CreateNew",
                    "create": {
                        "name": f"Facts Sel Test {uid}",
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
            log.append({"step": "SaveObjectAs", "error": str(e)[:200]})

        if saved_key:
            view_url = f"{BASE}/app/dashboard.html#key={saved_key}&mode=view&name=Dashboard&repo={REPO}"
            try:
                page.goto(view_url, wait_until="networkidle", timeout=60000)
                page.wait_for_timeout(5000)
            except Exception:
                pass
        page.screenshot(path=str(OUT / "final_view.png"), full_page=True)

        browser.close()

    (OUT / "log.json").write_text(json.dumps(log, indent=2, ensure_ascii=False))
    print(json.dumps({"out": str(OUT), "steps": len(log)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
