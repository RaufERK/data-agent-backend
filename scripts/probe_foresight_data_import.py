#!/usr/bin/env python3
"""Probe Foresight dashboard Data import flow with a small CSV file."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8110/fp10.x"
OUT = Path("/tmp/foresight_import_probe")
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
        writer = csv.writer(f, delimiter=",")
        writer.writerows(rows)


def save(page, name: str) -> None:
    page.screenshot(path=str(OUT / f"{name}.png"), full_page=True)
    (OUT / f"{name}.html").write_text(page.content(), encoding="utf-8")
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
                {
                    "method": req.method,
                    "url": req.url,
                    "post": (req.post_data or "")[:8000],
                }
            )
            if "PPService.axd" in req.url or "import" in req.url.lower()
            else None,
        )

        page.goto(f"{BASE}/app/login.html#repo=FS_DEMO", wait_until="networkidle", timeout=60000)
        page.fill('input[name="username"]', "FP_ADMIN")
        page.fill('input[type="password"]', "FP_ADMIN")
        page.get_by_text("Log in", exact=True).click(timeout=5000, force=True)
        page.wait_for_timeout(6000)
        if "login.html" in page.url:
            page.screenshot(path=str(OUT / "00_login_failed.png"), full_page=True)
            raise RuntimeError("Foresight web login failed")
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
        save(page, "01_new_dashboard")

        # Add a visualizer block first; the Data panel with "Data import..."
        # appears only when a block is selected.
        page.mouse.click(220, 42)
        page.wait_for_timeout(500)
        page.mouse.click(286, 98)  # Chart visualizer dropdown
        page.wait_for_timeout(500)
        page.mouse.click(290, 154)  # First chart type
        page.wait_for_timeout(3000)
        save(page, "015_after_chart")

        # Click "Data import..." in the lower-left side panel. In this UI the
        # first click opens an import wizard; the native file chooser may appear
        # on a later step, so record the intermediate state too.
        page.mouse.click(78, 850)
        page.wait_for_timeout(4000)
        save(page, "02_after_data_import_click")
        page.mouse.click(585, 450)  # "File with data" tile
        page.wait_for_timeout(1000)
        save(page, "025_after_file_with_data")
        try:
            with page.expect_file_chooser(timeout=5000) as fc_info:
                page.mouse.click(970, 727)  # Next >
            chooser = fc_info.value
            chooser.set_files(str(CSV_PATH))
            page.wait_for_timeout(8000)
            save(page, "03_after_csv_selected")
        except Exception:
            page.mouse.click(970, 727)
            page.wait_for_timeout(3000)
            save(page, "03_after_next_no_filechooser")
            try:
                with page.expect_file_chooser(timeout=5000) as fc_info:
                    page.mouse.click(1150, 256)  # Browse...
                chooser = fc_info.value
                chooser.set_files(str(CSV_PATH))
                page.wait_for_timeout(5000)
                save(page, "04_after_csv_selected_late")
                page.mouse.click(970, 727)  # Next >
                page.wait_for_timeout(5000)
                save(page, "05_after_next_fields")
                page.mouse.click(1055, 727)  # Import
                page.wait_for_timeout(10000)
                save(page, "06_after_import")
                page.mouse.click(1055, 693)  # Save imported source OK
                page.wait_for_timeout(10000)
                save(page, "07_after_import_ok")
            except Exception:
                pass
            pass

        (OUT / "requests.json").write_text(
            json.dumps(requests, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps({"url": page.url, "out": str(OUT), "requests": len(requests)}, indent=2))
        browser.close()


if __name__ == "__main__":
    main()
