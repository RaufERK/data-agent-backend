"""Import saved dashboard payloads into Navigator and validate persisted widgets.

This smoke test checks the integration boundary after XML import, not just the
generated XML. It compares expected XML widgets with ui.tscreenwidget_v30 rows
and verifies KPI units in the imported xparams.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
import xml.etree.ElementTree as ET
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.builders.triplex_export import _TriplexExportBuilder  # noqa: E402
from backend.config import get_settings  # noqa: E402
from backend.services.navigator_import import (  # noqa: E402
    NavigatorImportConfig,
    build_dashboard_url,
    ensure_subject_area_access,
    grant_subject_area_source_access,
    import_xml_to_navigator,
    query_dashboard_screen,
    query_import_state,
    resolve_imported_dashboard,
    resolve_imported_subject_area,
    _run_json_sql,
)


UNIT_ATTR_RE = re.compile(r's(?:Unit|Postfix|Suffix)="([^"]+)"')


def _navigator_import_config() -> NavigatorImportConfig:
    settings = get_settings()
    return NavigatorImportConfig(
        base_url=settings.navigator_base_url,
        db_host=settings.navigator_db_host,
        db_port=settings.navigator_db_port,
        db_name=settings.navigator_db_name,
        db_user=settings.navigator_db_user,
        db_password=settings.navigator_db_password,
        access_login=settings.navigator_access_login,
    )


def _payload_title(path: Path, index: int) -> str:
    title = path.parent.name.strip() or path.stem
    return f"SMOKE_NAV_{index:02d}_{title}"


def _kpi_units(payload: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for row in payload.get("kpi_rows") or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("metric_name") or row.get("title") or row.get("metric_code") or "").strip()
        unit = str(row.get("unit") or row.get("value_unit") or "").strip()
        if name and unit:
            result[name] = unit
    return result


def _xml_widgets(xml_bytes: bytes) -> list[dict[str, str]]:
    root = ET.fromstring(xml_bytes)
    result: list[dict[str, str]] = []
    for row in root.findall("./data/t19/r"):
        result.append(
            {
                "name": str(row.get("sname_ru") or ""),
                "type_id": str(row.get("nwidgettypeid") or ""),
                "xparams": str(row.get("xparams") or ""),
            }
        )
    return result


def _query_screen_widgets(screen_id: int, config: NavigatorImportConfig) -> list[dict[str, Any]]:
    sql = f"""
SELECT COALESCE(json_agg(row_to_json(w) ORDER BY norder, nid), '[]'::json)::text
FROM (
    SELECT
        nid,
        norder,
        sname_ru,
        nwidgettypeid,
        xparams::text AS xparams
    FROM ui.tscreenwidget_v30
    WHERE nscreenid = {int(screen_id)}
) w;
"""
    result = _run_json_sql(sql, config, "Navigator widget lookup failed")
    if isinstance(result, list):
        return result
    return []


def _coverage(expected: set[str], actual: set[str]) -> float:
    if not expected:
        return 1.0
    return round(len(expected & actual) / len(expected), 4)


def _unit_misses(expected_units: dict[str, str], db_widgets: list[dict[str, Any]]) -> list[str]:
    by_name = {str(row.get("sname_ru") or ""): str(row.get("xparams") or "") for row in db_widgets}
    misses: list[str] = []
    for name, unit in expected_units.items():
        xparams = by_name.get(name)
        if xparams is None:
            misses.append(f"{name}:missing_widget")
            continue
        if unit not in set(UNIT_ATTR_RE.findall(xparams)):
            misses.append(f"{name}:{unit}")
    return misses


def inspect_payload(path: Path, index: int, config: NavigatorImportConfig, do_import: bool) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["dashboard_title"] = _payload_title(path, index)
    payload["title"] = payload["dashboard_title"]

    builder = _TriplexExportBuilder(payload)
    xml_bytes = builder.build_xml()
    expected_widgets = _xml_widgets(xml_bytes)
    expected_by_name = {row["name"]: row for row in expected_widgets}
    expected_names = set(expected_by_name)
    expected_units = _kpi_units(payload)

    row: dict[str, Any] = {
        "payload": str(path),
        "title": payload["dashboard_title"],
        "expected_widgets": len(expected_widgets),
        "expected_unit_count": len(expected_units),
        "xml_bytes": len(xml_bytes),
        "imported": False,
        "subject_area_id": "",
        "dashboard_id": "",
        "screen_id": "",
        "dashboard_url": "",
        "actual_widgets": 0,
        "widget_coverage": 0.0,
        "type_match_coverage": 0.0,
        "unit_coverage": 0.0 if expected_units else 1.0,
        "missing_widgets": "",
        "type_mismatches": "",
        "missing_units": "",
        "subject_area_user_source_count": "",
        "subject_area_connection_count": "",
        "subject_area_data_model_count": "",
        "ok": False,
        "error": "",
    }

    if not do_import:
        row["ok"] = True
        return row

    try:
        import_xml_to_navigator(xml_bytes, config)
        actual_subject_area = resolve_imported_subject_area(
            builder.subject_area_id,
            builder.subject_area_name,
            config,
        )
        subject_area_id = int(actual_subject_area["nid"])
        ensure_subject_area_access(subject_area_id, config)
        grant_subject_area_source_access(subject_area_id, config)
        dashboard_info = resolve_imported_dashboard(subject_area_id, config)
        actual_dashboard = dashboard_info.get("dashboard") or {}
        dashboard_id = int(actual_dashboard.get("nid") or dashboard_info.get("linked_dashboard_id"))
        validation = query_import_state(subject_area_id, dashboard_id, config)
        screen = query_dashboard_screen(dashboard_id, config)
        screen_id = int(screen.get("screen_id"))
        db_widgets = _query_screen_widgets(screen_id, config)
    except Exception as exc:  # noqa: BLE001 - smoke report should capture all failures.
        row["error"] = str(exc)
        return row

    actual_by_name = {str(item.get("sname_ru") or ""): item for item in db_widgets}
    actual_names = set(actual_by_name)
    missing_widgets = sorted(expected_names - actual_names)
    type_mismatches: list[str] = []
    for name, expected in expected_by_name.items():
        actual = actual_by_name.get(name)
        if not actual:
            continue
        expected_type = str(expected.get("type_id") or "")
        actual_type = str(actual.get("nwidgettypeid") or "")
        if expected_type != actual_type:
            type_mismatches.append(f"{name}:{expected_type}->{actual_type}")
    unit_misses = _unit_misses(expected_units, db_widgets)
    type_match_count = max(0, len(expected_names & actual_names) - len(type_mismatches))
    validation_counts = validation if isinstance(validation, dict) else {}

    row.update(
        {
            "imported": True,
            "subject_area_id": subject_area_id,
            "dashboard_id": dashboard_id,
            "screen_id": screen_id,
            "dashboard_url": build_dashboard_url(dashboard_id, screen_id, config),
            "actual_widgets": len(db_widgets),
            "widget_coverage": _coverage(expected_names, actual_names),
            "type_match_coverage": round(type_match_count / len(expected_names), 4) if expected_names else 1.0,
            "unit_coverage": round((len(expected_units) - len(unit_misses)) / len(expected_units), 4)
            if expected_units
            else 1.0,
            "missing_widgets": "; ".join(missing_widgets),
            "type_mismatches": "; ".join(type_mismatches),
            "missing_units": "; ".join(unit_misses),
            "subject_area_user_source_count": validation_counts.get("subject_area_user_source_count", ""),
            "subject_area_connection_count": validation_counts.get("subject_area_connection_count", ""),
            "subject_area_data_model_count": validation_counts.get("subject_area_data_model_count", ""),
        }
    )
    row["ok"] = (
        row["widget_coverage"] == 1.0
        and row["type_match_coverage"] == 1.0
        and row["unit_coverage"] == 1.0
        and not row["error"]
    )
    return row


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pattern", default="eval_results/**/export_payload.json")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--xml-only", action="store_true")
    args = parser.parse_args()

    payloads = sorted(Path(".").glob(args.pattern))
    if args.limit > 0:
        payloads = payloads[: args.limit]

    out_dir = Path("artifacts/navigator_smoke")
    out_dir.mkdir(parents=True, exist_ok=True)
    config = _navigator_import_config()
    rows = [
        inspect_payload(path, index + 1, config, do_import=not args.xml_only)
        for index, path in enumerate(payloads)
    ]

    fieldnames = [
        "payload",
        "title",
        "expected_widgets",
        "actual_widgets",
        "widget_coverage",
        "type_match_coverage",
        "expected_unit_count",
        "unit_coverage",
        "missing_widgets",
        "type_mismatches",
        "missing_units",
        "subject_area_user_source_count",
        "subject_area_connection_count",
        "subject_area_data_model_count",
        "subject_area_id",
        "dashboard_id",
        "screen_id",
        "dashboard_url",
        "xml_bytes",
        "imported",
        "ok",
        "error",
    ]
    out_csv = out_dir / "navigator_import_smoke.csv"
    out_json = out_dir / "navigator_import_smoke_full.json"
    with out_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    out_json.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    failed = [row for row in rows if not row["ok"]]
    print(f"payloads={len(rows)} failed={len(failed)} csv={out_csv} json={out_json}")
    for row in failed[:20]:
        print(
            f"FAIL {row['payload']} widgets={row['actual_widgets']}/{row['expected_widgets']} "
            f"widget_cov={row['widget_coverage']} type_cov={row['type_match_coverage']} "
            f"unit_cov={row['unit_coverage']} error={row['error']}"
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
