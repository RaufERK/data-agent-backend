#!/usr/bin/env python3
"""Probe Foresight dashboard editor controls after creating a new dashboard."""

from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8110/fp10.x"
OUT = Path("/tmp/foresight_editor_probe")
OUT.mkdir(parents=True, exist_ok=True)


def save(page, name: str) -> None:
    page.screenshot(path=str(OUT / f"{name}.png"), full_page=True)
    (OUT / f"{name}.html").write_text(page.content(), encoding="utf-8")
    (OUT / f"{name}.txt").write_text(page.locator("body").inner_text(), encoding="utf-8")


def main() -> None:
    requests: list[dict[str, object]] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 900})

        def on_request(req):
            post = req.post_data or ""
            if "PPService.axd" in req.url:
                requests.append({"method": req.method, "url": req.url, "post": post[:6000]})

        page.on("request", on_request)
        page.goto(f"{BASE}/app/login.html#repo=FS_DEMO", wait_until="networkidle", timeout=60000)
        page.fill('input[name="username"]', "FP_ADMIN")
        page.fill('input[type="password"]', "FP_ADMIN")
        page.keyboard.press("Enter")
        page.wait_for_timeout(3000)
        page.get_by_text("Dashboards", exact=True).click(timeout=10000)
        page.wait_for_timeout(1000)
        try:
            page.get_by_text("New", exact=True).last.click(timeout=3000, force=True)
        except Exception:
            page.mouse.click(515, 580)
        page.wait_for_timeout(5000)
        save(page, "01_new_dashboard")

        # Open INSERT tab and visualizer menu.
        # Ribbon tabs are rendered by a custom widget; coordinate clicks are
        # more stable than text locators.
        page.mouse.click(220, 42)
        page.wait_for_timeout(1000)
        save(page, "02_insert_tab")
        page.mouse.click(570, 98)
        page.wait_for_timeout(1000)
        save(page, "03_visualizers_menu")

        # Click common chart options if they are visible; this reveals whether
        # a widget gets created without a source.
        for label in ("Chart", "Line chart", "Bar chart", "Table", "Indicator"):
            try:
                page.get_by_text(label, exact=False).first.click(timeout=1500)
                page.wait_for_timeout(1000)
                save(page, f"04_after_{label.replace(' ', '_').lower()}")
                break
            except Exception:
                pass

        (OUT / "requests.json").write_text(
            json.dumps(requests, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps({"url": page.url, "out": str(OUT), "requests": len(requests)}, indent=2))
        browser.close()


if __name__ == "__main__":
    main()
