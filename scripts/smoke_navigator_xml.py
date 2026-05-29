"""Smoke-test Navigator XML export against saved eval payloads.

Checks the two regressions that break Navigator imports most often:
  1. every payload chart/KPI becomes a t19 screen widget;
  2. KPI units are preserved in card xparams.
"""
from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.builders.triplex_export import _TriplexExportBuilder  # noqa: E402


UNIT_ATTR_RE = re.compile(r's(?:Unit|Postfix|Suffix)="([^"]+)"')


def _expected_widgets(payload: dict) -> int:
    return len(payload.get("charts") or []) + len(payload.get("kpi_rows") or [])


def _kpi_units(payload: dict) -> dict[str, str]:
    result: dict[str, str] = {}
    for row in payload.get("kpi_rows") or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("metric_name") or row.get("title") or row.get("metric_code") or "").strip()
        unit = str(row.get("unit") or row.get("value_unit") or "").strip()
        if name and unit:
            result[name] = unit
    return result


def inspect_payload(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    xml_bytes = _TriplexExportBuilder(payload).build_xml()
    root = ET.fromstring(xml_bytes)
    widget_rows = root.findall("./data/t19/r")
    widget_names = [str(row.get("sname_ru") or "") for row in widget_rows]
    expected = _expected_widgets(payload)
    expected_units = _kpi_units(payload)
    missing_units: list[str] = []
    for name, unit in expected_units.items():
        matching = [row for row in widget_rows if str(row.get("sname_ru") or "") == name]
        if not matching:
            missing_units.append(f"{name}:missing_widget")
            continue
        xparams = str(matching[0].get("xparams") or "")
        units = set(UNIT_ATTR_RE.findall(xparams))
        if unit not in units:
            missing_units.append(f"{name}:{unit}")

    return {
        "payload": str(path),
        "expected_widgets": expected,
        "actual_widgets": len(widget_rows),
        "missing_widgets": max(0, expected - len(widget_rows)),
        "expected_unit_count": len(expected_units),
        "missing_units": "; ".join(missing_units),
        "ok": len(widget_rows) >= expected and not missing_units,
        "widget_names": " | ".join(widget_names[:20]),
    }


def main() -> int:
    payloads = sorted(Path("eval_results").glob("**/export_payload.json"))
    out_dir = Path("artifacts/navigator_smoke")
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = [inspect_payload(path) for path in payloads]
    out_csv = out_dir / "navigator_xml_smoke.csv"
    with out_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "payload",
                "expected_widgets",
                "actual_widgets",
                "missing_widgets",
                "expected_unit_count",
                "missing_units",
                "ok",
                "widget_names",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    failed = [row for row in rows if not row["ok"]]
    print(f"payloads={len(rows)} failed={len(failed)} report={out_csv}")
    for row in failed[:20]:
        print(
            f"FAIL {row['payload']} expected={row['expected_widgets']} "
            f"actual={row['actual_widgets']} missing_units={row['missing_units']}"
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
