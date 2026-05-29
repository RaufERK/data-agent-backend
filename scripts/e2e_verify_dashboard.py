#!/usr/bin/env python3
"""Open the published CRM dashboard and verify all widgets show data.

Checks:
1. Screenshot with longer wait (15s for all charts to render)
2. GetEaxMd on each widget — dataRange should NOT be MultiPart(1x1)
3. Reports pass/fail per widget
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright
from backend.config import Settings

OUT = Path("/tmp/e2e_verify_dashboard")
OUT.mkdir(parents=True, exist_ok=True)

PP_HEADERS = {
    "Content-Type": "application/json; charset=UTF-8",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}


def pp(req_ctx, base_url: str, body: dict) -> dict:
    r = req_ctx.post(
        f"{base_url.rstrip('/')}/app/PPService.axd",
        data=json.dumps(body, ensure_ascii=False),
        headers=PP_HEADERS,
    )
    return json.loads(r.text())


def main() -> None:
    result_file = Path("/tmp/e2e_crm_publish/result.json")
    if not result_file.exists():
        print("ERROR: result.json not found — run e2e_crm_publish.py first")
        sys.exit(1)

    result = json.loads(result_file.read_text())
    key = result.get("object_key") or result.get("saved_key")
    if not key:
        print("ERROR: no object_key in result")
        sys.exit(1)

    cfg = Settings()
    base = cfg.foresight_base_url.rstrip("/")
    view_url = f"{base}/app/dashboard.html#key={key}&mode=view&name=Dashboard&repo={cfg.foresight_repo_id}"
    print(f"Opening: {view_url}")

    eax_ids: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = ctx.new_page()

        def on_req(req):
            post = req.post_data or ""
            m = re.search(r'"tEax":\{"id":"([^"]+!DSO![^"]+)"\}', post)
            if m and m.group(1) not in eax_ids:
                eax_ids.append(m.group(1))

        page.on("request", on_req)

        # Login
        page.goto(f"{base}/app/login.html#repo={cfg.foresight_repo_id}", wait_until="networkidle", timeout=60000)
        page.fill('input[name="username"]', cfg.foresight_repo_login)
        page.fill('input[type="password"]', cfg.foresight_repo_password)
        page.keyboard.press("Enter")
        page.wait_for_timeout(8000)

        if "login.html" in page.url:
            print("Login failed")
            browser.close()
            sys.exit(1)
        print("Logged in")

        # Open view URL
        page.goto(view_url, wait_until="networkidle", timeout=60000)
        print("Waiting 15s for all charts to render...")
        page.wait_for_timeout(15000)

        page.screenshot(path=str(OUT / "dashboard_full.png"), full_page=True)
        print(f"Screenshot → {OUT}/dashboard_full.png")

        print(f"\nEAX ids captured during view: {len(eax_ids)}")
        for eid in eax_ids:
            print(f"  {eid[:60]}...")

        # Check dataRange for each EAX
        print("\n--- Widget dataRange check ---")
        passed = 0
        failed = 0
        results = []
        for eid in eax_ids:
            try:
                r = pp(ctx.request, base, {
                    "GetEaxMd": {"tEax": {"id": eid}, "tArg": {"pattern": {"chart": True, "dims": True}}}
                })
                meta = r.get("GetEaxMdResult", {}).get("meta", {})
                chart = meta.get("chart", {})
                dr = chart.get("dataRange", {})
                dr_type = dr.get("type", "?")
                dr_w = dr.get("width", "?")
                dr_h = dr.get("height", "?")
                is_broken = (dr_type == "MultiPart" and dr_w == 1 and dr_h == 1)
                status = "FAIL dataRange=1x1" if is_broken else f"OK  dataRange={dr_type}"
                symbol = "❌" if is_broken else "✅"
                print(f"  {symbol} {status}  (eax ...{eid[-20:]})")
                results.append({"eax_id": eid, "dr_type": dr_type, "dr_w": dr_w, "dr_h": dr_h, "ok": not is_broken})
                if is_broken:
                    failed += 1
                else:
                    passed += 1
            except Exception as e:
                print(f"  ? ERROR checking {eid[-20:]}: {e}")
                results.append({"eax_id": eid, "error": str(e)})

        print(f"\nResult: {passed} passed, {failed} failed out of {len(eax_ids)} widgets")

        browser.close()

    (OUT / "verify_results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Details → {OUT}/verify_results.json")

    if failed:
        print("\n⚠ Some widgets still broken")
        sys.exit(1)
    elif passed == 0:
        print("\n⚠ No widgets found to check")
        sys.exit(1)
    else:
        print("\n✅ All widgets OK")


if __name__ == "__main__":
    main()
