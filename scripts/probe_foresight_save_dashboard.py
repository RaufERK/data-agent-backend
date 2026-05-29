#!/usr/bin/env python3
"""Create one real Foresight dashboard with imported CSV data and probe saving."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8110/fp10.x"
OUT = Path("/tmp/foresight_save_probe")
OUT.mkdir(parents=True, exist_ok=True)
CSV_PATH = OUT / "data_agent_demo.csv"


def write_csv() -> None:
    rows = [
        ("period", "category", "revenue", "profit", "orders"),
        ("2025-01", "Electronics", 4200000, 1100000, 1250),
        ("2025-01", "Fashion", 1800000, 600000, 950),
        ("2025-02", "Electronics", 4500000, 1200000, 1340),
        ("2025-02", "Fashion", 2100000, 700000, 1050),
    ]
    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)


def save(page, name: str) -> None:
    page.screenshot(path=str(OUT / f"{name}.png"), full_page=True)
    (OUT / f"{name}.txt").write_text(page.locator("body").inner_text(), encoding="utf-8")


def main() -> None:
    write_csv()
    requests: list[dict[str, object]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 900})
        page.on(
            "request",
            lambda req: requests.append(
                {"method": req.method, "url": req.url, "post": (req.post_data or "")[:10000]}
            )
            if "PPService.axd" in req.url
            else None,
        )

        page.goto(f"{BASE}/app/login.html#repo=FS_DEMO", wait_until="networkidle", timeout=60000)
        page.fill('input[name="username"]', "FP_ADMIN")
        page.fill('input[type="password"]', "FP_ADMIN")
        page.keyboard.press("Enter")
        page.wait_for_timeout(5000)
        try:
            page.get_by_text("Dashboards", exact=True).click(timeout=3000)
        except Exception:
            page.mouse.click(512, 420)
        page.wait_for_timeout(1000)
        try:
            page.get_by_text("New", exact=True).last.click(timeout=3000, force=True)
        except Exception:
            page.mouse.click(515, 580)
        page.wait_for_timeout(5000)

        # Insert first chart.
        page.mouse.click(220, 42)
        page.wait_for_timeout(500)
        page.mouse.click(286, 98)
        page.wait_for_timeout(500)
        page.mouse.click(290, 154)
        page.wait_for_timeout(3000)

        # Import CSV into chart.
        page.mouse.click(78, 850)
        page.wait_for_timeout(2000)
        page.mouse.click(585, 450)
        page.wait_for_timeout(800)
        page.mouse.click(970, 727)
        page.wait_for_timeout(2000)
        save(page, "00_before_browse")
        with page.expect_file_chooser(timeout=5000) as fc_info:
            page.mouse.click(1150, 256)
        fc_info.value.set_files(str(CSV_PATH))
        page.wait_for_timeout(5000)
        page.mouse.click(970, 727)
        page.wait_for_timeout(5000)
        page.mouse.click(1055, 727)
        page.wait_for_timeout(10000)
        save(page, "01_import_result")

        # Finish import wizard.
        page.get_by_text("Finish", exact=True).click(timeout=5000, force=True)
        page.wait_for_timeout(5000)
        save(page, "02_after_finish")

        # Try Save changes in ribbon. If it opens Save As, fill final metadata.
        page.get_by_text("HOME", exact=True).click(timeout=5000, force=True)
        page.wait_for_timeout(1000)
        page.mouse.click(390, 78)
        page.wait_for_timeout(5000)
        save(page, "03_after_save_click")

        text = page.locator("body").inner_text()
        if "Name:" in text and "Identifier:" in text:
            # Best-effort fill: use visible textboxes in save dialog, usually
            # name and identifier are the last two inputs.
            inputs = page.locator("input")
            count = inputs.count()
            if count >= 2:
                inputs.nth(count - 2).fill("Data Agent: реальный CSV dashboard")
                inputs.nth(count - 1).fill("DA_REAL_CSV_DASHBOARD")
            page.mouse.click(1055, 693)
            page.wait_for_timeout(8000)
            save(page, "04_after_save_as_ok")

        (OUT / "requests.json").write_text(
            json.dumps(requests, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps({"url": page.url, "out": str(OUT), "requests": len(requests)}, indent=2))
        browser.close()


if __name__ == "__main__":
    main()
