#!/usr/bin/env python3
"""
Probe OpenCube to get dimension keys, then try SetEaxMd dim position swap.
Captures all response bodies too.
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
OUT = Path("/tmp/probe_opencube")
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
            em = re.search(r'"tEax":\{"id":"([^"]+!DSO![^"]+)"\}', p)
            if em:
                eid = em.group(1)
                if eid not in state["eax_ids"]:
                    state["eax_ids"].append(eid)

        def on_response(resp):
            if "PPService.axd" in resp.url:
                try:
                    body = resp.json()
                    responses.append({"url": resp.url, "body": body})
                except Exception:
                    pass

        page.on("request", on_req)
        page.on("response", on_response)

        # Login
        page.goto(f"{BASE}/app/login.html#repo={REPO}", wait_until="networkidle", timeout=60000)
        page.fill('input[name="username"]', USER)
        page.fill('input[type="password"]', PASS)
        page.keyboard.press("Enter")
        page.wait_for_timeout(5000)

        # Dashboards → New
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
        log.append({"step": "post_import", "cube_key": cube_key, "eax_id": eax_id})

        if not eax_id or not cube_key:
            log.append({"ABORT": "no eax_id or cube_key"})
            (OUT / "log.json").write_text(json.dumps(log, indent=2, ensure_ascii=False))
            browser.close()
            return

        # ── Step A: GetEaxMd with pattern (not metaGet) ──────────────────────
        try:
            r_get = pp(ctx.request, {"GetEaxMd": {"tEax": {"id": eax_id}, "tArg": {
                "pattern": {"chart": True}
            }}})
            log.append({"step": "GetEaxMd_pattern_chart", "response": r_get})
        except Exception as e:
            log.append({"step": "GetEaxMd_pattern_chart", "error": str(e)})

        # ── Step B: GetEaxMd with pattern dims ───────────────────────────────
        try:
            r_get2 = pp(ctx.request, {"GetEaxMd": {"tEax": {"id": eax_id}, "tArg": {
                "pattern": {"dims": {"its": {"it": []}}, "chart": True}
            }}})
            log.append({"step": "GetEaxMd_pattern_dims", "response": r_get2})
        except Exception as e:
            log.append({"step": "GetEaxMd_pattern_dims", "error": str(e)})

        # ── Step C: OpenCube to get dim keys ─────────────────────────────────
        cube_obj_id = f"{metabase_id}!{cube_key}" if metabase_id else None
        if cube_obj_id:
            try:
                r_oc = pp(ctx.request, {"OpenCube": {"tOb": {"id": cube_obj_id}, "tArg": {
                    "args": {}, "metaGet": {"dims": "Get", "dim": {"settings": True}}
                }}})
                log.append({"step": "OpenCube", "response_keys": list(r_oc.keys()),
                            "snippet": str(r_oc)[:2000]})
            except Exception as e:
                log.append({"step": "OpenCube", "error": str(e)})

        # ── Step D: Find OpenCube response in captured responses ──────────────
        for resp in responses:
            b = resp.get("body", {})
            if "OpenCubeResult" in b:
                log.append({"step": "UI_OpenCubeResult", "body": b})
                break
            if "GetCubeImportResult" in b:
                gc = b["GetCubeImportResult"]
                log.append({"step": "UI_GetCubeImportResult_snippet", "snippet": str(gc)[:1000]})

        # ── Step E: Try SetEaxMd with SelectDim / DeselectDim patterns ─────
        for op_name, op_body in [
            ("SelectDims_all", {"SetEaxMd": {"tEax": {"id": eax_id}, "tArg": {
                "pattern": {"selectDims": True},
                "meta": {"selectDims": {"all": True}},
                "refresh": {"fetchData": True, "chart": True, "saveData": False},
                "metaGet": {"chart": True},
            }}}),
            ("autoFill", {"SetEaxMd": {"tEax": {"id": eax_id}, "tArg": {
                "pattern": {"autoFill": True},
                "meta": {},
                "refresh": {"fetchData": True, "chart": True, "saveData": False},
                "metaGet": {"chart": True},
            }}}),
            ("SetEaxTable_expand", {"SetEaxMd": {"tEax": {"id": eax_id}, "tArg": {
                "pattern": {"grid": True},
                "meta": {"grid": {"expandAllDims": True}},
                "refresh": {"fetchData": True, "grid": True, "saveData": False},
                "metaGet": {"grid": True},
            }}}),
        ]:
            try:
                r = pp(ctx.request, op_body)
                result_key = list(r.keys())[0] if r else "?"
                inner = r.get(result_key, {})
                meta = inner.get("tResult", {}).get("meta", {}) if isinstance(inner, dict) else {}
                log.append({"step": op_name, "result_key": result_key,
                            "meta_keys": list(meta.keys()) if meta else [],
                            "snippet": str(r)[:500]})
            except Exception as e:
                log.append({"step": op_name, "error": str(e)})

        # ── Step F: Check what responses contain GetEaxMdResult ──────────────
        eax_responses = [r for r in responses if "GetEaxMd" in str(r.get("body", {}))]
        for er in eax_responses[:3]:
            log.append({"step": "captured_GetEaxMdResult", "body": er["body"]})

        (OUT / "log.json").write_text(json.dumps(log, indent=2, ensure_ascii=False))
        (OUT / "responses.json").write_text(json.dumps(responses[-20:], indent=2, ensure_ascii=False))
        browser.close()

    print(json.dumps({"out": str(OUT), "steps": len(log), "responses": len(responses)}))


if __name__ == "__main__":
    main()
