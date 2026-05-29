#!/usr/bin/env python3
"""Probe creation of a reusable multi-block Foresight 8448 dashboard template."""

from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8110/fp10.x"
OUT = Path("/tmp/foresight_template_probe")
OUT.mkdir(parents=True, exist_ok=True)


def save(page, name: str) -> None:
    page.screenshot(path=str(OUT / f"{name}.png"), full_page=True)
    (OUT / f"{name}.txt").write_text(page.locator("body").inner_text(), encoding="utf-8")
    (OUT / f"{name}.html").write_text(page.content(), encoding="utf-8")


def click_insert(page) -> None:
    try:
        page.get_by_text("INSERT", exact=True).click(timeout=1500, force=True)
    except Exception:
        page.mouse.click(220, 42)
    page.wait_for_timeout(700)


def click_visualizer(page, label: str, fallback: tuple[int, int]) -> bool:
    click_insert(page)
    try:
        page.get_by_text(label, exact=False).first.click(timeout=1800, force=True)
        page.wait_for_timeout(2000)
        return True
    except Exception:
        page.mouse.click(*fallback)
        page.wait_for_timeout(2000)
        return label in page.locator("body").inner_text()


def add_chart(page) -> None:
    click_insert(page)
    page.mouse.click(286, 98)
    page.wait_for_timeout(500)
    page.mouse.click(292, 154)
    page.wait_for_timeout(2500)


def add_table(page) -> None:
    click_insert(page)
    page.mouse.click(238, 98)
    page.wait_for_timeout(2500)


def add_indicator(page) -> None:
    click_insert(page)
    page.mouse.click(548, 98)
    page.wait_for_timeout(2500)


def main() -> None:
    requests: list[dict[str, object]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 900})

        def on_request(req):
            post = req.post_data or ""
            if "PPService.axd" in req.url:
                requests.append({"method": req.method, "url": req.url, "post": post[:30000]})

        page.on("request", on_request)
        page.goto(f"{BASE}/app/login.html#repo=FS_DEMO", wait_until="networkidle", timeout=60000)
        page.fill('input[name="username"]', "FP_ADMIN")
        page.fill('input[type="password"]', "FP_ADMIN")
        page.keyboard.press("Enter")
        page.wait_for_timeout(5000)
        try:
            page.get_by_text("Dashboards", exact=True).click(timeout=5000, force=True)
        except Exception:
            page.mouse.click(512, 420)
        page.wait_for_timeout(1500)
        try:
            page.get_by_text("New", exact=True).last.click(timeout=3000, force=True)
        except Exception:
            page.mouse.click(515, 580)
        page.wait_for_timeout(5000)
        save(page, "01_new")

        actions = [
            ("chart", add_chart),
            ("table", add_table),
            ("indicator", add_indicator),
            ("chart", add_chart),
        ]
        results = []
        for idx, (label, action) in enumerate(actions, start=1):
            action(page)
            body = page.locator("body").inner_text()
            results.append({"label": label, "block_count": body.count("Block ")})
            save(page, f"02_after_{idx}_{label}")

        try:
            page.get_by_text("HOME", exact=True).click(timeout=3000, force=True)
        except Exception:
            page.mouse.click(95, 42)
        page.wait_for_timeout(700)
        page.keyboard.press("Control+S")
        page.wait_for_timeout(5000)
        save(page, "03_after_ctrl_s")

        text = page.locator("body").inner_text()
        if "Name:" in text and "Identifier:" in text:
            inputs = page.locator("input")
            count = inputs.count()
            if count >= 2:
                inputs.nth(count - 2).fill("Data Agent Foresight Template")
                inputs.nth(count - 1).fill("DA_FORESIGHT_TEMPLATE")
            page.mouse.click(1055, 693)
            page.wait_for_timeout(8000)
            save(page, "04_after_save_as")

        (OUT / "requests.json").write_text(json.dumps(requests, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"url": page.url, "out": str(OUT), "requests": len(requests), "results": results}, ensure_ascii=False, indent=2))
        browser.close()


if __name__ == "__main__":
    main()
