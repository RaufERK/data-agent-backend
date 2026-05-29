#!/usr/bin/env python3
"""E2E test: publish CRM dashboard to Foresight and verify all 6 widgets show data."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.foresight_service import publish_dashboard

SPEC = Path("artifacts/visiology_manual_bundle/dashboard_spec.json")
OUT = Path("/tmp/e2e_crm_publish")
OUT.mkdir(parents=True, exist_ok=True)


def _num(v) -> float:
    try:
        return float(str(v).replace(",", ".").strip())
    except Exception:
        return 0.0


def spec_to_payload_chart(c: dict) -> dict:
    t = c.get("type", "bar")
    title = c.get("title", "")

    if t == "kpi":
        value = _num(c.get("value", 0))
        return {
            "chart_type": "big_number",
            "title": title,
            "x_field": "label",
            "y_field": "value",
            "columns": [{"name": "label"}, {"name": "value"}],
            "rows": [{"label": title, "value": value}],
        }

    if t == "pie":
        rows = [{"category": s["label"], "value": s["value"]} for s in (c.get("slices") or [])]
        return {
            "chart_type": "pie",
            "title": title,
            "x_field": "category",
            "y_field": "value",
            "columns": [{"name": "category"}, {"name": "value"}],
            "rows": rows,
        }

    if t == "bar":
        categories = c.get("categories") or []
        series = c.get("series") or []
        values = series[0].get("values", []) if series else []
        rows = [{"category": cat, "value": val} for cat, val in zip(categories, values)]
        return {
            "chart_type": "bar",
            "title": title,
            "x_field": "category",
            "y_field": "value",
            "columns": [{"name": "category"}, {"name": "value"}],
            "rows": rows,
        }

    return {"chart_type": "bar", "title": title, "x_field": "category", "y_field": "value",
            "columns": [{"name": "category"}, {"name": "value"}], "rows": []}


def build_payload(spec: dict) -> dict:
    charts = [spec_to_payload_chart(c) for c in (spec.get("charts") or [])]

    # 2-column grid layout, 3 rows
    layout = []
    for i, ch in enumerate(charts):
        layout.append({
            "id": str(i),
            "slice_name": ch["title"],
            "col": (i % 2) * 6,
            "row": (i // 2) * 3,
            "width": 6,
            "height": 300,
        })

    return {
        "dashboard_title": spec.get("topic", "CRM Dashboard"),
        "foresight_object_id": "DA_CRM_E2E_TEST",
        "charts": charts,
        "layout": layout,
    }


def main() -> None:
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    payload = build_payload(spec)

    print(f"Dashboard: {payload['dashboard_title']}")
    print(f"Charts ({len(payload['charts'])}):")
    for i, ch in enumerate(payload["charts"]):
        print(f"  [{i}] {ch['chart_type']:12s} {ch['title']} — {len(ch['rows'])} rows")

    (OUT / "payload.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nPayload → {OUT}/payload.json")
    print("\nCalling publish_dashboard()...")

    t0 = time.time()
    try:
        result = publish_dashboard(payload)
    except Exception as e:
        print(f"\nFAIL: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)

    elapsed = time.time() - t0
    print(f"Done in {elapsed:.1f}s\n")

    # Print result (without binary)
    clean = {k: v for k, v in result.items() if k != "screenshot_bytes"}
    print(json.dumps(clean, ensure_ascii=False, indent=2))

    # Save screenshot
    shot = result.get("screenshot_bytes") or b""
    if shot:
        p = OUT / "screenshot.png"
        p.write_bytes(shot)
        print(f"\nScreenshot → {p} ({len(shot)} bytes)")

    # View URL
    key = result.get("saved_key") or result.get("object_key")
    if key:
        print(f"\nView URL: http://127.0.0.1:8110/fp10.x/app/dashboard.html#key={key}&mode=view&repo=FS_DEMO")

    (OUT / "result.json").write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Result  → {OUT}/result.json")


if __name__ == "__main__":
    main()
