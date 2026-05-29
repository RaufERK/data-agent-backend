#!/usr/bin/env python3
"""
Try different GetEaxMd patterns to find which one returns dims + chart metadata.
Uses an existing saved dashboard (key=22867, the working permanent one).
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
OUT = Path("/tmp/probe_getmd")
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
"""


def pp(req_ctx, body: dict) -> dict:
    r = req_ctx.post(f"{BASE}/app/PPService.axd", data=json.dumps(body, ensure_ascii=False), headers=PP_HEADERS)
    if r.status != 200:
        raise RuntimeError(f"PPService {r.status}: {r.text()[:800]}")
    return json.loads(r.text())


def main() -> None:
    log: list[dict] = []
    all_requests: list[dict] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1600, "height": 900})
        page = ctx.new_page()

        state: dict = {"root_id": None, "adhoc_id": None, "cube_keys": [], "eax_ids": []}

        def on_req(req):
            p = req.post_data or ""
            if "PPService.axd" in req.url and p:
                all_requests.append({"url": req.url, "post": p[:2000]})
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

        # Login
        page.goto(f"{BASE}/app/login.html#repo={REPO}", wait_until="networkidle", timeout=60000)
        page.fill('input[name="username"]', USER)
        page.fill('input[type="password"]', PASS)
        page.keyboard.press("Enter")
        page.wait_for_timeout(5000)

        # Go to Dashboards → New
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
        log.append({"step": "adhoc_id", "value": state["adhoc_id"]})

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
            log.append({"step": "ABORT", "reason": "import not found"})
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
        log.append({"step": "post_import", "cube_key": cube_key, "eax_id": eax_id})

        if not eax_id:
            log.append({"step": "ABORT", "reason": "no eax_id"})
            (OUT / "log.json").write_text(json.dumps(log, indent=2, ensure_ascii=False))
            browser.close()
            return

        # Now capture what the UI sends for GetEaxMd when user interacts
        # First, let's look at all_requests to see if any GetEaxMd happened
        get_eaxmd_reqs = [r for r in all_requests if "GetEaxMd" in r["post"]]
        log.append({"step": "ui_GetEaxMd_count", "count": len(get_eaxmd_reqs),
                    "samples": get_eaxmd_reqs[:3]})

        # Try all plausible GetEaxMd patterns
        patterns_to_try = [
            # Pattern from PPService.axd browser capture
            {"dims": {"its": True}, "chart": True},
            {"dims": True, "chart": True, "grid": True, "dataSources": "Get"},
            # Pattern 1 - simple
            {"chart": True},
            # Pattern 2 - dims as bool
            {"dims": True},
            # Pattern 3 - what we tried
            {"dims": True, "chart": True},
            # Pattern 4 - nested
            {"dims": {"its": {"it": True}}, "chart": {"hiChart": True}},
            # Pattern 5 - full
            {"chart": True, "grid": True, "speedometer": True, "dataSources": "Get", "dims": True},
        ]

        for i, p in enumerate(patterns_to_try):
            try:
                r = pp(ctx.request, {"GetEaxMd": {"tEax": {"id": eax_id}, "tArg": {"metaGet": p}}})
                meta = r.get("GetEaxMdResult", {}).get("tResult", {}).get("meta", {})
                log.append({
                    "step": f"GetEaxMd_pattern_{i}",
                    "pattern": p,
                    "meta_keys": list(meta.keys()),
                    "dims_excerpt": str(meta.get("dims", "MISSING"))[:500],
                    "chart_excerpt": str(meta.get("chart", "MISSING"))[:300],
                })
            except Exception as e:
                log.append({"step": f"GetEaxMd_pattern_{i}", "pattern": p, "error": str(e)})

        # Also try with GetObMd to see if we can get the cube's dims
        try:
            r_obmd = pp(ctx.request, {"GetObMd": {"tOb": {"k": cube_key}, "tArg": {
                "metaGet": {"dims": True, "cube": True}
            }}})
            log.append({"step": "GetObMd_dims", "response_keys": list(r_obmd.keys()),
                        "snippet": str(r_obmd)[:600]})
        except Exception as e:
            log.append({"step": "GetObMd_dims", "error": str(e)})

        # Save captured requests to see what Foresight UI actually sends
        (OUT / "all_requests.json").write_text(json.dumps(all_requests, indent=2, ensure_ascii=False))
        (OUT / "log.json").write_text(json.dumps(log, indent=2, ensure_ascii=False))
        browser.close()

    print(json.dumps({"out": str(OUT), "steps": len(log), "requests_captured": len(all_requests)}))


if __name__ == "__main__":
    main()
