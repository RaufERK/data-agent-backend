"""Capture Navigator screenshots for dashboards imported by smoke_navigator_import.py."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from playwright.sync_api import sync_playwright


def _safe_name(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9а-яА-ЯёЁ_-]+", "_", value).strip("_")
    return text[:80] or "dashboard"


def _login_if_needed(page, username: str, password: str) -> None:
    page.wait_for_timeout(1200)
    text = page.locator("body").inner_text(timeout=5000)
    if "Логин" not in text or "Пароль" not in text:
        return
    page.mouse.click(960, 490)
    page.keyboard.press("Control+A")
    page.keyboard.type(username)
    page.mouse.click(960, 560)
    page.keyboard.press("Control+A")
    page.keyboard.type(password)
    page.mouse.click(960, 670)
    page.wait_for_timeout(5000)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="artifacts/navigator_smoke/navigator_import_smoke_full.json")
    parser.add_argument("--out-dir", default="artifacts/navigator_smoke/screenshots")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default="admin")
    args = parser.parse_args()

    rows = json.loads(Path(args.input).read_text(encoding="utf-8"))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    captured: list[dict[str, str]] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            ignore_https_errors=True,
        )
        page = context.new_page()
        for index, row in enumerate(rows, start=1):
            url = row.get("dashboard_url")
            if not url:
                continue
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            _login_if_needed(page, args.username, args.password)
            page.wait_for_timeout(6000)
            text = page.locator("body").inner_text(timeout=5000)
            screenshot = out_dir / f"{index:02d}_{_safe_name(str(row.get('title') or 'dashboard'))}.png"
            page.screenshot(path=str(screenshot), full_page=True)
            captured.append(
                {
                    "title": str(row.get("title") or ""),
                    "url": str(url),
                    "screenshot": str(screenshot),
                    "text_excerpt": text[:1200],
                }
            )
        browser.close()

    report = out_dir / "screenshots.json"
    report.write_text(json.dumps(captured, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"screenshots={len(captured)} report={report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
