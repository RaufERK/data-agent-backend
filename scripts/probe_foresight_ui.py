#!/usr/bin/env python3
"""Probe Foresight web UI actions and record requests/screenshots.

This is intentionally a diagnostic script: we use the browser exactly as a user
does, then inspect the web requests needed to create/open real dashboard objects.
"""

from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


BASE = "http://127.0.0.1:8110/fp10.x"
OUT = Path("/tmp/foresight_ui_probe")
OUT.mkdir(parents=True, exist_ok=True)


def save(page, name: str) -> None:
    page.screenshot(path=str(OUT / f"{name}.png"), full_page=True)
    (OUT / f"{name}.html").write_text(page.content(), encoding="utf-8")


def main() -> None:
    requests: list[dict[str, object]] = []
    responses: list[dict[str, object]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 900})

        def on_request(req):
            post = req.post_data or ""
            if "PPService.axd" in req.url or "Create" in post or "Dashboard" in post:
                requests.append(
                    {
                        "method": req.method,
                        "url": req.url,
                        "post": post[:4000],
                    }
                )

        def on_response(resp):
            if "PPService.axd" in resp.url:
                try:
                    body = resp.text()[:4000]
                except Exception as exc:  # noqa: BLE001 - diagnostic only
                    body = f"<body unavailable: {exc}>"
                responses.append({"url": resp.url, "status": resp.status, "body": body})

        page.on("request", on_request)
        page.on("response", on_response)

        page.goto(f"{BASE}/app/login.html#repo=FS_DEMO", wait_until="networkidle", timeout=60000)
        page.fill('input[name="username"]', "FP_ADMIN")
        page.fill('input[type="password"]', "FP_ADMIN")
        page.keyboard.press("Enter")
        page.wait_for_timeout(4000)
        save(page, "01_after_login")

        page.get_by_text("Dashboards", exact=True).click(timeout=10000)
        page.wait_for_timeout(2500)
        save(page, "02_dashboards_module")

        # Prefer the visible "New" tile in the dashboard module. If it is not a
        # semantic button, fall back to coordinates near the left tile.
        clicked = False
        (OUT / "02_text.txt").write_text(page.locator("body").inner_text(), encoding="utf-8")
        for label in ("New", "Создать", "Новый"):
            try:
                page.get_by_text(label, exact=True).last.click(timeout=2500)
                clicked = True
                break
            except PlaywrightTimeoutError:
                pass
        if not clicked:
            # On 1600x900 the New tile is below Open in the left action column.
            page.mouse.click(515, 580)

        page.wait_for_timeout(6000)
        save(page, "03_after_new_dashboard")

        (OUT / "requests.json").write_text(
            json.dumps(requests, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (OUT / "responses.json").write_text(
            json.dumps(responses, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps({"url": page.url, "out": str(OUT), "requests": len(requests)}, indent=2))
        browser.close()


if __name__ == "__main__":
    main()
