#!/usr/bin/env python3
"""
Check the working permanent dashboard (key=22867) eax_id's chart meta.
Also probe dim positions. Compare with our test dashboards.
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
OUT = Path("/tmp/probe_working_dash")
OUT.mkdir(parents=True, exist_ok=True)

PP_HEADERS = {
    "Content-Type": "application/json; charset=UTF-8",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}

WORKING_DASH_KEY = 22867


def pp(req_ctx, body: dict) -> dict:
    r = req_ctx.post(f"{BASE}/app/PPService.axd", data=json.dumps(body, ensure_ascii=False), headers=PP_HEADERS)
    if r.status != 200:
        raise RuntimeError(f"PPService {r.status}: {r.text()[:800]}")
    return json.loads(r.text())


def main() -> None:
    log: list[dict] = []
    all_requests: list[dict] = []
    all_responses: list[dict] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1600, "height": 900})
        page = ctx.new_page()

        state: dict = {"root_id": None, "eax_ids": [], "adhoc_ids": [], "metabase_id": None}

        def on_req(req):
            p = req.post_data or ""
            if "PPService.axd" in req.url and p:
                all_requests.append({"post": p[:3000]})
            if state["root_id"] is None and "!M!Root" in p:
                m = re.search(r"([A-Z0-9]+!M!Root)", p)
                if m:
                    state["root_id"] = m.group(1)
                    state["metabase_id"] = m.group(1).replace("!M!Root", "!M")
            em = re.search(r'"tEax":\{"id":"([^"]+!DSO![^"]+)"\}', p)
            if em:
                eid = em.group(1)
                if eid not in state["eax_ids"]:
                    state["eax_ids"].append(eid)
            am = re.search(r'"tAdHocId":\{"id":"([^"]+)"\}', p)
            if am:
                aid = am.group(1)
                if aid not in state["adhoc_ids"]:
                    state["adhoc_ids"].append(aid)

        def on_resp(resp):
            if "PPService.axd" in resp.url:
                try:
                    b = resp.json()
                    all_responses.append(b)
                except Exception:
                    pass

        page.on("request", on_req)
        page.on("response", on_resp)

        # Login
        page.goto(f"{BASE}/app/login.html#repo={REPO}", wait_until="networkidle", timeout=60000)
        page.fill('input[name="username"]', USER)
        page.fill('input[type="password"]', PASS)
        page.keyboard.press("Enter")
        page.wait_for_timeout(5000)
        for _ in range(20):
            if state["root_id"]:
                break
            page.wait_for_timeout(500)
        log.append({"step": "root_id", "value": state["root_id"]})

        # Navigate directly to the working dashboard
        view_url = f"{BASE}/app/dashboard.html#key={WORKING_DASH_KEY}&mode=view&name=Dashboard&repo={REPO}"
        page.goto(view_url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(8000)

        page.screenshot(path=str(OUT / "working_dash_view.png"), full_page=True)
        log.append({"step": "eax_ids_captured", "eax_ids": state["eax_ids"]})
        log.append({"step": "adhoc_ids_captured", "adhoc_ids": state["adhoc_ids"]})

        # For each captured eax_id, get chart meta
        for eax_id in state["eax_ids"][:5]:
            try:
                r = pp(ctx.request, {"GetEaxMd": {"tEax": {"id": eax_id}, "tArg": {"pattern": {"chart": True}}}})
                chart = r.get("GetEaxMdResult", {}).get("meta", {}).get("chart", {})
                log.append({
                    "step": "eax_chart_meta",
                    "eax_id": eax_id[-20:],
                    "dataRange": chart.get("dataRange"),
                    "singleCellRangeMode": chart.get("singleCellRangeMode"),
                    "timeLineDimension": chart.get("timeLineDimension"),
                    "objectivesDimension": chart.get("objectivesDimension"),
                    "seriesInRows": chart.get("seriesInRows"),
                })
            except Exception as e:
                log.append({"step": "eax_chart_meta", "eax_id": eax_id[-20:], "error": str(e)})

        # Check what the GetAdHoc response contains for the working dash
        for resp in all_responses:
            if "GetAdHocResult" in resp:
                log.append({"step": "GetAdHocResult_snippet",
                            "snippet": str(resp)[:2000]})
                break

        # Also check if there are any SetEaxMd in the responses revealing what was set
        for req in all_requests:
            if "SetEaxMd" in req["post"] and "singleCellRangeMode" in req["post"]:
                log.append({"step": "SetEaxMd_with_singleCellRangeMode", "post": req["post"][:500]})
            if "SetEaxMd" in req["post"] and "timeLineDimension" in req["post"]:
                log.append({"step": "SetEaxMd_with_timeLineDimension", "post": req["post"][:500]})

        browser.close()

    (OUT / "log.json").write_text(json.dumps(log, indent=2, ensure_ascii=False))
    print(json.dumps({"out": str(OUT), "eax_ids": len(state["eax_ids"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
