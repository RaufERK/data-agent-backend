#!/usr/bin/env python3
"""Debug: open published dashboard, check each widget's pivot data and dims."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright
from backend.config import Settings

OUT = Path("/tmp/debug_widget_data")
OUT.mkdir(parents=True, exist_ok=True)

PP_HEADERS = {
    "Content-Type": "application/json; charset=UTF-8",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}


def pp(req_ctx, base: str, body: dict) -> dict:
    r = req_ctx.post(f"{base}/app/PPService.axd", data=json.dumps(body, ensure_ascii=False), headers=PP_HEADERS)
    return json.loads(r.text())


def main() -> None:
    result = json.loads(Path("/tmp/e2e_crm_publish/result.json").read_text())
    key = result["object_key"]
    cfg = Settings()
    base = cfg.foresight_base_url.rstrip("/")
    view_url = f"{base}/app/dashboard.html#key={key}&mode=view&name=Dashboard&repo={cfg.foresight_repo_id}"

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

        page.goto(f"{base}/app/login.html#repo={cfg.foresight_repo_id}", wait_until="networkidle", timeout=60000)
        page.fill('input[name="username"]', cfg.foresight_repo_login)
        page.fill('input[type="password"]', cfg.foresight_repo_password)
        page.keyboard.press("Enter")
        page.wait_for_timeout(8000)

        if "login.html" in page.url:
            print("Login failed"); browser.close(); sys.exit(1)

        page.goto(view_url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(10000)

        print(f"EAX ids: {len(eax_ids)}")

        report = []
        for i, eid in enumerate(eax_ids):
            print(f"\n=== Widget {i}: ...{eid[-20:]} ===")

            # Get chart meta
            r = pp(ctx.request, base, {"GetEaxMd": {"tEax": {"id": eid}, "tArg": {"pattern": {
                "chart": True,
                "dims": True,
                "dimArg": {
                    "elsArg": {"totalCount": True, "selectionInfo": True, "filter": {"levels": 1, "elementsGroup": True}, "pattern": {"attributes": "*"}},
                    "pattern": {"getDescr": True, "getIsAllSelected": True, "getSelection": True},
                },
                "dataSources": "Get",
                "dataSource": {"getCubeArg": {"pars": True}, "displaySourceName": True},
            }}}})
            meta = r.get("GetEaxMdResult", {}).get("meta", {})
            chart = meta.get("chart", {})
            dr = chart.get("dataRange", {})
            print(f"  dataRange: type={dr.get('type')} w={dr.get('width')} h={dr.get('height')}")

            # Dims
            dims_raw = meta.get("dims", {})
            dim_items = dims_raw.get("its", {}).get("it", []) if isinstance(dims_raw, dict) else []
            print(f"  Dims ({len(dim_items)}):")
            for d in dim_items:
                name = (d.get("descr") or {}).get("name", "?")
                sel_info = d.get("selectionInfo", {})
                total = sel_info.get("totalCount") if isinstance(sel_info, dict) else "?"
                selected = sel_info.get("selectedCount") if isinstance(sel_info, dict) else "?"
                is_all = d.get("isAllSelected")
                print(f"    {name}: total={total}, selected={selected}, allSelected={is_all}")

                # Get elements
                try:
                    el_r = pp(ctx.request, base, {"BatchExec": {"tArg": {"its": {"it": [{
                        "GetDimElements": {
                            "tDim": {"id": d.get("id") or f"{eid}!{d.get('key')}"},
                            "tArg": {
                                "filter": {"levels": 1, "includeRoot": False, "elementsGroup": True},
                                "pattern": {"attributes": "*", "getSelectState": True},
                                "range": {"start": 0, "count": 20},
                            }
                        }
                    }]}}}})
                    els = el_r.get("BatchExecResult", {}).get("its", {}).get("it", [{}])[0]
                    el_data = els.get("GetDimElementsResult", {}).get("its", {}).get("it", [])
                    print(f"    Elements ({len(el_data)}): {[e.get('n') or e.get('name') or str(e.get('k','?')) for e in el_data[:5]]}")
                except Exception as e:
                    print(f"    GetDimElements error: {e}")

            # Data source info
            ds = meta.get("dataSources", {})
            if isinstance(ds, dict):
                ds_its = ds.get("its", {}).get("it", [])
                for ds_item in ds_its[:1]:
                    cube = ds_item.get("cube", {}).get("obDesc", {})
                    print(f"  DataSource cube: k={cube.get('k')}, n={cube.get('n')}")

            report.append({"eax_id": eid, "dataRange": dr, "dims": len(dim_items)})

        (OUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        page.screenshot(path=str(OUT / "view.png"), full_page=True)
        browser.close()

    print(f"\nReport → {OUT}/report.json")
    print(f"Screenshot → {OUT}/view.png")


if __name__ == "__main__":
    main()
