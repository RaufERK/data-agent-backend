#!/usr/bin/env python3
"""Capture the API call when clicking the '≡' (select all) icon on a dim in Selection panel.

From the screenshot we can see:
- Left panel has Selection tab open
- 'category (1 of 5)' is expanded, A is selected (blue), B-E not selected
- There's a '≡ ▼' icon pair on the left of each dim row
- We need to click that '≡' icon (which likely selects all elements)

Strategy:
1. After import, click Selection tab
2. Click category dim row to expand it
3. Click the small icon (≡) at left of category row to trigger select-all
4. Also try clicking individual checkboxes for B, C, D, E
5. Capture ALL PPService requests and find the selection SET call
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
OUT = Path("/tmp/probe_select_all_icon")
OUT.mkdir(parents=True, exist_ok=True)

PP_HEADERS = {
    "Content-Type": "application/json; charset=UTF-8",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}

CSV_DATA = """period,category,value
2024-01,A,100
2024-01,B,200
2024-01,C,300
2024-01,D,150
2024-01,E,250
"""


def pp(req_ctx, body: dict) -> dict:
    response = req_ctx.post(
        f"{BASE}/app/PPService.axd",
        data=json.dumps(body, ensure_ascii=False),
        headers=PP_HEADERS,
    )
    text = response.text()
    if response.status != 200:
        raise RuntimeError(f"PPService {response.status}: {text[:1200]}")
    return json.loads(text)


def get_data_range(req_ctx, eax_id: str) -> dict:
    result = pp(req_ctx, {"GetEaxMd": {"tEax": {"id": eax_id}, "tArg": {"pattern": {"chart": True}}}})
    return result.get("GetEaxMdResult", {}).get("meta", {}).get("chart", {}).get("dataRange", {})


def main() -> None:
    all_pp_requests: list[dict] = []
    state: dict = {"eax_ids": [], "adhoc_id": None}

    csv_path = OUT / "probe.csv"
    csv_path.write_text(CSV_DATA, encoding="utf-8")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1600, "height": 900})
        page = ctx.new_page()

        def on_request(req):
            if "PPService.axd" in req.url:
                post = req.post_data or ""
                all_pp_requests.append({"url": req.url, "body": post[:10000]})
                eax_m = re.search(r'"tEax":\{"id":"([^"]+!DSO![^"]+)"\}', post)
                if eax_m:
                    eid = eax_m.group(1)
                    if eid not in state["eax_ids"]:
                        state["eax_ids"].append(eid)
                if state["adhoc_id"] is None:
                    m = re.search(r'"tAdHocId":\{"id":"([^"]+)"\}', post)
                    if m:
                        state["adhoc_id"] = m.group(1)

        page.on("request", on_request)

        # LOGIN
        page.goto(f"{BASE}/app/login.html#repo={REPO}", wait_until="networkidle", timeout=60000)
        page.fill('input[name="username"]', USER)
        page.fill('input[type="password"]', PASS)
        page.keyboard.press("Enter")
        page.wait_for_timeout(5000)

        # DASHBOARDS → NEW
        try:
            page.get_by_text("Dashboards", exact=True).click(timeout=5000)
        except Exception:
            page.mouse.click(512, 420)
        page.wait_for_timeout(3000)
        try:
            page.get_by_text("New", exact=True).last.click(timeout=3000, force=True)
        except Exception:
            page.mouse.click(515, 580)
        page.wait_for_timeout(3000)

        # INSERT CHART
        try:
            page.locator("#InsertCategory").click(timeout=5000)
        except Exception:
            page.mouse.click(220, 42)
        page.wait_for_timeout(500)
        page.mouse.click(286, 98)
        page.wait_for_timeout(700)
        page.mouse.click(290, 154)
        page.wait_for_timeout(4000)

        for _ in range(20):
            if state["eax_ids"]:
                break
            page.wait_for_timeout(500)

        # DATA IMPORT
        try:
            page.get_by_text("Data import", exact=False).last.click(timeout=5000, force=True)
        except Exception:
            pass
        page.wait_for_timeout(2000)
        try:
            page.get_by_text("File with data", exact=False).click(timeout=5000, force=True)
        except Exception:
            page.mouse.click(585, 450)
        page.wait_for_timeout(1000)
        try:
            with page.expect_file_chooser(timeout=5000) as chooser:
                page.get_by_text("Next >", exact=True).last.click(timeout=5000, force=True)
            chooser.value.set_files(str(csv_path))
        except Exception:
            try:
                page.get_by_text("Next >", exact=True).last.click(timeout=5000, force=True)
                page.wait_for_timeout(3000)
                with page.expect_file_chooser(timeout=5000) as chooser:
                    page.get_by_text("Browse", exact=False).click(timeout=5000, force=True)
                chooser.value.set_files(str(csv_path))
            except Exception as e:
                print(f"Upload failed: {e}")
        page.wait_for_timeout(5000)
        try:
            page.get_by_text("Next >", exact=True).last.click(timeout=5000, force=True)
        except Exception:
            pass
        page.wait_for_timeout(5000)
        try:
            page.get_by_text("Import", exact=True).last.click(timeout=5000, force=True)
        except Exception:
            pass
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

        eax_id = state["eax_ids"][0] if state["eax_ids"] else None
        print(f"EAX id: {eax_id}")
        dr = get_data_range(ctx.request, eax_id) if eax_id else {}
        print(f"dataRange BEFORE: type={dr.get('type')}, w={dr.get('width')}, h={dr.get('height')}")

        # OPEN SELECTION TAB
        page.screenshot(path=str(OUT / "00_before_selection.png"))
        # Try multiple ways to click Selection tab
        clicked_sel = False
        for sel_txt in ["Selection", "Выборка", "SELECTION"]:
            try:
                page.get_by_text(sel_txt, exact=True).first.click(timeout=3000, force=True)
                clicked_sel = True
                print(f"Clicked Selection tab: {sel_txt}")
                break
            except Exception:
                pass
        if not clicked_sel:
            # Click at the known position of the Selection tab from previous runs (~y=172, x=114)
            page.mouse.click(114, 172)
            print("Clicked Selection tab by coordinate")
        page.wait_for_timeout(2000)
        page.screenshot(path=str(OUT / "01_selection_tab.png"))

        # Click category dim to expand it
        clicked_cat = False
        for cat_txt in ["category", "Category"]:
            try:
                page.get_by_text(cat_txt, exact=False).first.click(timeout=3000, force=True)
                clicked_cat = True
                print(f"Clicked category dim: {cat_txt}")
                break
            except Exception:
                pass
        if not clicked_cat:
            page.mouse.click(160, 266)
            print("Clicked category dim by coordinate")
        page.wait_for_timeout(2000)
        page.screenshot(path=str(OUT / "02_category_expanded.png"))

        # ===== STRATEGY 1: Click the ≡ icon (left icon of dim row) =====
        print("\nStrategy 1: clicking '≡' icon on category row...")
        req_mark1 = len(all_pp_requests)

        # The ≡ icon is the leftmost element before the dim name text
        # From screenshot: it's at approximately x=22, y=266 (inside left panel)
        # The panel starts at x=0, the icon is very small
        # Let's try clicking at coords of the category row's left icon
        # From screenshot: category row is at ~y=266, left icon is at x=22
        page.mouse.click(22, 266)
        page.wait_for_timeout(2000)
        page.screenshot(path=str(OUT / "03_after_icon_click.png"))

        new1 = all_pp_requests[req_mark1:]
        print(f"  New PP requests: {len(new1)}")
        for r in new1:
            try:
                b = json.loads(r["body"])
                print(f"    {list(b.keys())}: {r['body'][:400]}")
            except Exception:
                print(f"    raw: {r['body'][:200]}")

        dr = get_data_range(ctx.request, eax_id) if eax_id else {}
        print(f"  dataRange after icon click: type={dr.get('type')}, w={dr.get('width')}, h={dr.get('height')}")

        # ===== STRATEGY 2: Right-click on category dim row for context menu =====
        print("\nStrategy 2: right-click on 'category (1 of 5)' text...")
        req_mark2 = len(all_pp_requests)
        try:
            page.get_by_text("category", exact=False).first.click(button="right", timeout=3000, force=True)
        except Exception:
            page.mouse.click(160, 266, button="right")
        page.wait_for_timeout(2000)
        page.screenshot(path=str(OUT / "04_context_menu.png"))

        # Look for "Select All" or "All" in context menu
        for txt in ["Select all", "Select All", "All", "Все", "Выбрать все"]:
            try:
                page.get_by_text(txt, exact=False).first.click(timeout=2000, force=True)
                print(f"  Clicked context menu item: {txt}")
                break
            except Exception:
                pass
        page.wait_for_timeout(2000)
        page.screenshot(path=str(OUT / "05_after_context_select.png"))

        new2 = all_pp_requests[req_mark2:]
        print(f"  New PP requests: {len(new2)}")
        for r in new2:
            try:
                b = json.loads(r["body"])
                print(f"    {list(b.keys())}: {r['body'][:600]}")
            except Exception:
                print(f"    raw: {r['body'][:200]}")

        dr = get_data_range(ctx.request, eax_id) if eax_id else {}
        print(f"  dataRange after context menu: type={dr.get('type')}, w={dr.get('width')}, h={dr.get('height')}")

        # ===== STRATEGY 3: Click individual checkboxes B, C, D, E =====
        print("\nStrategy 3: clicking individual element checkboxes B, C, D, E...")
        req_mark3 = len(all_pp_requests)

        # From screenshot: A is at ~y=352, B=372, C=390, D=408, E=426
        # Checkboxes are at x~22 in the list
        for y in [372, 390, 408, 426]:
            page.mouse.click(22, y)
            page.wait_for_timeout(500)

        page.wait_for_timeout(2000)
        page.screenshot(path=str(OUT / "06_after_checkboxes.png"))

        new3 = all_pp_requests[req_mark3:]
        print(f"  New PP requests: {len(new3)}")
        for r in new3:
            try:
                b = json.loads(r["body"])
                method = list(b.keys())[0]
                print(f"    [{method}]: {r['body'][:800]}")
            except Exception:
                print(f"    raw: {r['body'][:200]}")

        dr = get_data_range(ctx.request, eax_id) if eax_id else {}
        print(f"  dataRange after checkbox clicks: type={dr.get('type')}, w={dr.get('width')}, h={dr.get('height')}")

        # ===== STRATEGY 4: Use keyboard shortcut Ctrl+A in the list =====
        print("\nStrategy 4: Ctrl+A in the element list...")
        req_mark4 = len(all_pp_requests)
        page.mouse.click(150, 390)  # click in the list area
        page.wait_for_timeout(500)
        page.keyboard.press("Control+a")
        page.wait_for_timeout(2000)
        page.screenshot(path=str(OUT / "07_after_ctrl_a.png"))

        new4 = all_pp_requests[req_mark4:]
        print(f"  New PP requests: {len(new4)}")
        for r in new4:
            try:
                b = json.loads(r["body"])
                print(f"    {list(b.keys())}: {r['body'][:600]}")
            except Exception:
                print(f"    raw: {r['body'][:200]}")

        dr = get_data_range(ctx.request, eax_id) if eax_id else {}
        print(f"  dataRange after Ctrl+A: type={dr.get('type')}, w={dr.get('width')}, h={dr.get('height')}")

        # Save everything
        (OUT / "all_requests.json").write_text(
            json.dumps(all_pp_requests, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\nTotal PP requests: {len(all_pp_requests)}")
        page.screenshot(path=str(OUT / "final.png"), full_page=True)
        browser.close()


if __name__ == "__main__":
    main()
