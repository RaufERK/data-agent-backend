#!/usr/bin/env python3
"""
Probe dimension position swap + seriesInRows + dataRange persistence.

Tests (in sequence, same session, one fresh dashboard):
  1. Import CSV → capture cube_key + eax_id
  2. GetEaxMd → log dims structure (keys, positions)
  3. Try SetEaxMd dims position change (FACTS→Fixed, CATEGORY→Top)
  4. GetEaxMd after → check if positions persisted
  5. Try seriesInRows: false
  6. Try explicit dataRange {width:5, height:5}
  7. SaveObjectAs → open view URL → screenshot
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
OUT = Path("/tmp/probe_dim_pos")
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


def save_debug(page, name: str) -> None:
    page.screenshot(path=str(OUT / f"{name}.png"), full_page=True)
    (OUT / f"{name}.txt").write_text(page.locator("body").inner_text(), encoding="utf-8")


def main() -> None:
    log: list[dict] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1600, "height": 900})
        page = ctx.new_page()

        state: dict = {"root_id": None, "adhoc_id": None, "cube_keys": [], "eax_ids": []}

        def on_req(req):
            p = req.post_data or ""
            if state["root_id"] is None and "!M!Root" in p:
                m = re.search(r"([A-Z0-9]+!M!Root)", p)
                if m:
                    state["root_id"] = m.group(1)
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

        # ── Login ──────────────────────────────────────────────────────────────
        page.goto(f"{BASE}/app/login.html#repo={REPO}", wait_until="networkidle", timeout=60000)
        page.fill('input[name="username"]', USER)
        page.fill('input[type="password"]', PASS)
        page.keyboard.press("Enter")
        page.wait_for_timeout(5000)

        # ── Dashboards → New ───────────────────────────────────────────────────
        try:
            page.get_by_text("Dashboards", exact=True).click(timeout=5000)
        except Exception:
            page.mouse.click(512, 420)
        for _ in range(20):
            if state["root_id"]:
                break
            page.wait_for_timeout(500)
        log.append({"step": "root_id", "value": state["root_id"]})

        try:
            page.get_by_text("New", exact=True).last.click(timeout=3000, force=True)
        except Exception:
            page.mouse.click(515, 580)
        for _ in range(20):
            if state["adhoc_id"]:
                break
            page.wait_for_timeout(500)
        log.append({"step": "adhoc_id", "value": state["adhoc_id"]})

        # ── INSERT > Chart ──────────────────────────────────────────────────────
        try:
            page.locator("#InsertCategory").click(timeout=5000)
        except Exception:
            page.mouse.click(220, 42)
        page.wait_for_timeout(500)
        page.mouse.click(286, 98)
        page.wait_for_timeout(700)
        page.mouse.click(290, 154)
        page.wait_for_timeout(3000)
        save_debug(page, "01_chart_inserted")

        # ── Data import ────────────────────────────────────────────────────────
        try:
            page.get_by_text("Data import", exact=False).last.click(timeout=5000, force=True)
        except Exception:
            log.append({"step": "import_open", "error": "not found"})
            browser.close()
            return
        page.wait_for_timeout(2000)

        try:
            page.get_by_text("File with data", exact=False).click(timeout=5000, force=True)
        except Exception:
            page.mouse.click(585, 450)
        page.wait_for_timeout(1000)

        # Write CSV to temp file
        csv_path = OUT / "probe.csv"
        csv_path.write_text(CSV_DATA, encoding="utf-8")

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
            try:
                with page.expect_file_chooser(timeout=5000) as fc_info:
                    page.get_by_text("Browse", exact=False).click(timeout=5000, force=True)
                fc_info.value.set_files(str(csv_path))
            except Exception:
                log.append({"step": "file_chooser", "error": "failed"})
                save_debug(page, "01b_upload_fail")
                browser.close()
                return

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
        save_debug(page, "02_after_import")

        cube_key = max(state["cube_keys"]) if state["cube_keys"] else None
        eax_id = state["eax_ids"][0] if state["eax_ids"] else None
        log.append({"step": "post_import", "cube_key": cube_key, "eax_id": eax_id, "all_eax": state["eax_ids"]})

        if not eax_id or not cube_key:
            log.append({"step": "ABORT", "reason": "no eax_id or cube_key captured"})
            (OUT / "log.json").write_text(json.dumps(log, indent=2, ensure_ascii=False))
            browser.close()
            return

        adhoc_id = state["adhoc_id"]

        # ── STEP 2: GetEaxMd to learn dimension keys ───────────────────────────
        try:
            r_get = pp(ctx.request, {"GetEaxMd": {"tEax": {"id": eax_id}, "tArg": {
                "metaGet": {"dims": True, "chart": True, "grid": True},
            }}})
            dims_raw = r_get.get("GetEaxMdResult", {}).get("tResult", {}).get("meta", {}).get("dims", {})
            chart_raw = r_get.get("GetEaxMdResult", {}).get("tResult", {}).get("meta", {}).get("chart", {})
            log.append({"step": "GetEaxMd_before", "dims": dims_raw, "chart": chart_raw})
        except Exception as e:
            log.append({"step": "GetEaxMd_before", "error": str(e)})
            dims_raw = {}
            chart_raw = {}

        # Extract dimension keys
        facts_k = None
        category_k = None
        dims_items = dims_raw.get("its", {}).get("it", []) if isinstance(dims_raw, dict) else []
        for dim in dims_items:
            if isinstance(dim, dict):
                if dim.get("isFacts"):
                    facts_k = dim.get("k")
                elif "CATEGORY" in str(dim.get("id", "")):
                    category_k = dim.get("k")
        log.append({"step": "dim_keys", "facts_k": facts_k, "category_k": category_k})

        # ── STEP 3: Try SetEaxMd to swap dimension positions ───────────────────
        if facts_k and category_k:
            try:
                r_swap = pp(ctx.request, {"SetEaxMd": {"tEax": {"id": eax_id}, "tArg": {
                    "pattern": {"dims": {"its": {"it": [{"k": facts_k}, {"k": category_k}]}}},
                    "meta": {"dims": {"its": {"it": [
                        {"k": facts_k, "position": "Fixed"},
                        {"k": category_k, "position": "Top"},
                    ]}}},
                    "refresh": {"fetchData": True, "chart": True, "saveData": False},
                    "metaGet": {"dims": True, "chart": True},
                }}})
                log.append({"step": "SetEaxMd_swap_dims", "response": r_swap})
            except Exception as e:
                log.append({"step": "SetEaxMd_swap_dims", "error": str(e)})

            # ── STEP 4: GetEaxMd after swap → did positions persist? ───────────
            try:
                r_get2 = pp(ctx.request, {"GetEaxMd": {"tEax": {"id": eax_id}, "tArg": {
                    "metaGet": {"dims": True, "chart": True},
                }}})
                dims_after = r_get2.get("GetEaxMdResult", {}).get("tResult", {}).get("meta", {}).get("dims", {})
                log.append({"step": "GetEaxMd_after_swap", "dims": dims_after})
            except Exception as e:
                log.append({"step": "GetEaxMd_after_swap", "error": str(e)})

        # ── STEP 5: Try seriesInRows: false ────────────────────────────────────
        try:
            r_series = pp(ctx.request, {"SetEaxMd": {"tEax": {"id": eax_id}, "tArg": {
                "pattern": {"chart": True},
                "meta": {"chart": {"seriesInRows": False}},
                "refresh": {"fetchData": True, "chart": True, "saveData": False},
                "metaGet": {"chart": True},
            }}})
            chart_after_series = r_series.get("SetEaxMdResult", {}).get("tResult", {}).get("meta", {}).get("chart", {})
            log.append({"step": "seriesInRows_false", "chart_meta": chart_after_series})
        except Exception as e:
            log.append({"step": "seriesInRows_false", "error": str(e)})

        # ── STEP 6: Try explicit dataRange ────────────────────────────────────
        try:
            r_range = pp(ctx.request, {"SetEaxMd": {"tEax": {"id": eax_id}, "tArg": {
                "pattern": {"chart": True},
                "meta": {"chart": {"dataRange": {"left": 1, "top": 1, "width": 20, "height": 20}}},
                "refresh": {"fetchData": True, "chart": True, "saveData": False},
                "metaGet": {"chart": True},
            }}})
            chart_after_range = r_range.get("SetEaxMdResult", {}).get("tResult", {}).get("meta", {}).get("chart", {})
            log.append({"step": "explicit_dataRange", "chart_meta": chart_after_range})
        except Exception as e:
            log.append({"step": "explicit_dataRange", "error": str(e)})

        # ── STEP 6b: Try SetEaxMd with allElems / fullData patterns ──────────
        for pattern_name, pattern_val in [
            ("allElems", {"allElems": True}),
            ("fullData", {"fullData": True}),
            ("chart_allData", {"chart": {"allData": True}}),
        ]:
            try:
                r_x = pp(ctx.request, {"SetEaxMd": {"tEax": {"id": eax_id}, "tArg": {
                    "pattern": pattern_val,
                    "meta": pattern_val,
                    "refresh": {"fetchData": True, "chart": True, "saveData": False},
                    "metaGet": {"chart": True},
                }}})
                log.append({"step": f"pattern_{pattern_name}", "ok": True, "snippet": str(r_x)[:300]})
            except Exception as e:
                log.append({"step": f"pattern_{pattern_name}", "error": str(e)})

        # ── STEP 6c: Try SetEaxMd dims with fixed=false / expanded ──────────
        if facts_k:
            for pos_name, pos_val in [
                ("Left", "Left"),
                ("Top", "Top"),
                ("None", "None"),
            ]:
                try:
                    r_dp = pp(ctx.request, {"SetEaxMd": {"tEax": {"id": eax_id}, "tArg": {
                        "pattern": {"dims": {"its": {"it": [{"k": facts_k}]}}},
                        "meta": {"dims": {"its": {"it": [{"k": facts_k, "position": pos_val}]}}},
                        "refresh": {"fetchData": True, "chart": True, "saveData": False},
                        "metaGet": {"dims": True, "chart": True},
                    }}})
                    dims_resp = r_dp.get("SetEaxMdResult", {}).get("tResult", {}).get("meta", {}).get("dims", {})
                    chart_resp = r_dp.get("SetEaxMdResult", {}).get("tResult", {}).get("meta", {}).get("chart", {})
                    log.append({"step": f"facts_pos_{pos_name}", "dims": dims_resp, "chart_data_excerpt": str(chart_resp)[:300]})
                except Exception as e:
                    log.append({"step": f"facts_pos_{pos_name}", "error": str(e)})

        # ── STEP 7: SaveObjectAs ───────────────────────────────────────────────
        uid = str(int(time.time()))[-6:]
        obj_id = f"DA_DIM_PROBE_{uid}"
        try:
            r_save = pp(ctx.request, {"SaveObjectAs": {
                "tObject": {"id": adhoc_id},
                "tArg": {"destination": {
                    "operation": "CreateNew",
                    "create": {
                        "name": f"Dim Probe {uid}",
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
            log.append({"step": "SaveObjectAs", "error": str(e)})

        # ── Open view URL → screenshot ─────────────────────────────────────────
        if saved_key:
            view_url = f"{BASE}/app/dashboard.html#key={saved_key}&mode=view&name=Dashboard&repo={REPO}"
            try:
                page.goto(view_url, wait_until="networkidle", timeout=60000)
                page.wait_for_timeout(5000)
            except Exception:
                pass
        save_debug(page, "03_final_view")

        # ── Also GetEaxMd after save to check persistence ──────────────────────
        try:
            r_get3 = pp(ctx.request, {"GetEaxMd": {"tEax": {"id": eax_id}, "tArg": {
                "metaGet": {"dims": True, "chart": True},
            }}})
            dims_final = r_get3.get("GetEaxMdResult", {}).get("tResult", {}).get("meta", {}).get("dims", {})
            chart_final = r_get3.get("GetEaxMdResult", {}).get("tResult", {}).get("meta", {}).get("chart", {})
            log.append({"step": "GetEaxMd_after_save", "dims": dims_final, "chart": chart_final})
        except Exception as e:
            log.append({"step": "GetEaxMd_after_save", "error": str(e)})

        browser.close()

    (OUT / "log.json").write_text(json.dumps(log, indent=2, ensure_ascii=False))
    print(json.dumps({"out": str(OUT), "steps": len(log)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
