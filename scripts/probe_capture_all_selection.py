#!/usr/bin/env python3
"""Capture ALL PPService.axd requests/responses during Selection panel 'All' click.

Goal: find the exact API call that selects all elements in a dimension,
which is the missing piece to fix dataRange=MultiPart(1x1).
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
OUT = Path("/tmp/probe_all_selection")
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


def main() -> None:
    all_requests: list[dict] = []
    state: dict = {"eax_ids": [], "adhoc_id": None}

    csv_path = OUT / "probe.csv"
    csv_path.write_text(CSV_DATA, encoding="utf-8")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1600, "height": 900})
        page = ctx.new_page()

        # Capture ALL PP requests and responses
        def on_request(req):
            if "PPService.axd" in req.url:
                post = req.post_data or ""
                entry = {"url": req.url, "body": post[:8000]}
                all_requests.append(entry)
                eax_m = re.search(r'"tEax":\{"id":"([^"]+!DSO![^"]+)"\}', post)
                if eax_m:
                    eax_id = eax_m.group(1)
                    if eax_id not in state["eax_ids"]:
                        state["eax_ids"].append(eax_id)
                if state["adhoc_id"] is None and '"tAdHocId":{"id":"' in post:
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

        # NAVIGATE TO DASHBOARDS
        try:
            page.get_by_text("Dashboards", exact=True).click(timeout=5000)
        except Exception:
            page.mouse.click(512, 420)
        page.wait_for_timeout(3000)

        # CREATE NEW DASHBOARD
        try:
            page.get_by_text("New", exact=True).last.click(timeout=3000, force=True)
        except Exception:
            page.mouse.click(515, 580)
        page.wait_for_timeout(3000)

        # INSERT CHART WIDGET
        print("Inserting chart widget...")
        try:
            page.locator("#InsertCategory").click(timeout=5000)
        except Exception:
            page.mouse.click(220, 42)
        page.wait_for_timeout(500)
        page.mouse.click(286, 98)
        page.wait_for_timeout(700)
        page.mouse.click(290, 154)
        page.wait_for_timeout(4000)

        # Wait until we have an EAX widget open
        for _ in range(20):
            if state["eax_ids"]:
                break
            page.wait_for_timeout(500)
        print(f"EAX ids captured: {state['eax_ids']}")

        # DATA IMPORT
        print("Starting data import...")
        try:
            page.get_by_text("Data import", exact=False).last.click(timeout=5000, force=True)
        except Exception:
            print("  'Data import' not found, trying coordinate click")
            page.mouse.click(300, 300)
        page.wait_for_timeout(2000)

        try:
            page.get_by_text("File with data", exact=False).click(timeout=5000, force=True)
        except Exception:
            page.mouse.click(585, 450)
        page.wait_for_timeout(1000)

        # Upload CSV
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
                print(f"  File upload fallback also failed: {e}")

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
        print(f"EAX id after import: {eax_id}")

        # Check current state
        if eax_id:
            try:
                result = pp(ctx.request, {"GetEaxMd": {"tEax": {"id": eax_id}, "tArg": {"pattern": {"chart": True, "dims": True, "dimSettings": True}}}})
                meta = result.get("GetEaxMdResult", {}).get("meta", {})
                chart = meta.get("chart", {})
                dims = meta.get("dims", {})
                print(f"BEFORE Selection interaction:")
                print(f"  dataRange: {chart.get('dataRange')}")
                print(f"  dims keys: {list(dims.keys()) if isinstance(dims, dict) else dims}")
            except Exception as e:
                print(f"GetEaxMd failed: {e}")

        # MARK: snapshot request count before selection interaction
        req_count_before = len(all_requests)
        print(f"\nRequest count before Selection interaction: {req_count_before}")

        # OPEN SELECTION PANEL
        print("\nOpening Selection panel...")
        page.screenshot(path=str(OUT / "before_selection_tab.png"))

        # Try clicking Selection tab
        clicked_selection = False
        for sel_text in ["Selection", "Выборка", "Фильтр", "Filter"]:
            try:
                page.get_by_text(sel_text, exact=True).first.click(timeout=3000, force=True)
                clicked_selection = True
                print(f"  Clicked '{sel_text}' tab")
                break
            except Exception:
                pass

        if not clicked_selection:
            print("  Could not find Selection tab by text")
        page.wait_for_timeout(2000)
        page.screenshot(path=str(OUT / "after_selection_tab.png"))

        # Find category dim element and click it
        print("\nLooking for 'category' dimension...")
        clicked_dim = False
        for dim_text in ["category", "Category", "Категория"]:
            try:
                page.get_by_text(dim_text, exact=False).first.click(timeout=3000, force=True)
                clicked_dim = True
                print(f"  Clicked '{dim_text}' dim")
                break
            except Exception:
                pass

        if not clicked_dim:
            print("  category dim not found by text, trying coordinates")
            page.mouse.click(100, 400)
        page.wait_for_timeout(3000)
        page.screenshot(path=str(OUT / "after_category_click.png"))

        req_count_after_dim = len(all_requests)
        new_after_dim = all_requests[req_count_before:]
        print(f"\nNew requests after clicking category dim: {len(new_after_dim)}")
        for r in new_after_dim:
            body = r["body"]
            try:
                parsed = json.loads(body)
                print(f"  Method: {list(parsed.keys())}")
            except Exception:
                print(f"  Body preview: {body[:200]}")

        # NOW CLICK "All" button
        print("\nLooking for 'All' button...")
        req_count_before_all = len(all_requests)
        clicked_all = False
        for all_text in ["All", "Все", "Выбрать все", "Select All", "Select all"]:
            try:
                page.get_by_text(all_text, exact=True).last.click(timeout=3000, force=True)
                clicked_all = True
                print(f"  Clicked '{all_text}'")
                break
            except Exception:
                pass

        if not clicked_all:
            # Try looking for checkboxes and clicking header checkbox
            print("  'All' button not found by text, trying header checkbox")
            try:
                page.locator("input[type='checkbox']").first.click(timeout=3000, force=True)
                clicked_all = True
                print("  Clicked first checkbox")
            except Exception:
                print("  Checkbox click failed")

        page.wait_for_timeout(4000)
        page.screenshot(path=str(OUT / "after_all_click.png"))

        # Capture ALL new requests after "All" click
        new_after_all = all_requests[req_count_before_all:]
        print(f"\nNew requests after 'All' click: {len(new_after_all)}")
        for i, r in enumerate(new_after_all):
            body = r["body"]
            try:
                parsed = json.loads(body)
                method = list(parsed.keys())[0]
                print(f"\n  [{i}] Method: {method}")
                # Print full body for analysis
                print(f"      Body: {body[:2000]}")
            except Exception:
                print(f"  [{i}] Raw body: {body[:500]}")

        # Save all new requests
        (OUT / "requests_during_all_click.json").write_text(
            json.dumps(new_after_all, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # Also check dataRange after
        if eax_id:
            try:
                result = pp(ctx.request, {"GetEaxMd": {"tEax": {"id": eax_id}, "tArg": {"pattern": {"chart": True}}}})
                chart = result.get("GetEaxMdResult", {}).get("meta", {}).get("chart", {})
                dr = chart.get("dataRange", {})
                print(f"\nAFTER clicking 'All':")
                print(f"  dataRange: {dr}")
            except Exception as e:
                print(f"GetEaxMd after All failed: {e}")

        # Save all captured requests to file for analysis
        (OUT / "all_requests.json").write_text(
            json.dumps(all_requests, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\nTotal requests captured: {len(all_requests)}")
        print(f"Saved to: {OUT}")

        page.screenshot(path=str(OUT / "final.png"), full_page=True)
        browser.close()


if __name__ == "__main__":
    main()
