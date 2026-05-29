#!/usr/bin/env python3
"""Test ChangeDimSelection fix using the existing permanent test dashboard.

Uses SSH+Metabase to get an EAX id from the known DA_PERM_TEST_02 dashboard,
then tests ChangeDimSelection purely via HTTP API (no browser session needed).

The HTTP API for PPService.axd requires a valid browser session, but we can
get the session token from the existing foresight_service.py login mechanism.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.foresight_service import (
    _pp_login_and_get_context,
    _pp_post,
)
from backend.config import Settings

OUT = Path("/tmp/probe_fix_via_metabase")
OUT.mkdir(parents=True, exist_ok=True)


def main() -> None:
    cfg = Settings()
    base_url = cfg.foresight_base_url

    print(f"Base URL: {base_url}")
    print("Getting PP session context...")

    with _pp_login_and_get_context(base_url) as (request_context, eax_registry):
        print(f"Login OK, EAX registry: {eax_registry}")

        # Find first EAX ID in the registry (should be from the loaded dashboard)
        eax_ids = list(eax_registry.keys()) if eax_registry else []
        print(f"Known EAX ids: {eax_ids[:3]}")

        if not eax_ids:
            print("No EAX ids found - need to open a dashboard first")
            # Try to navigate to a known permanent dashboard and get its EAX
            # The DA_PERM_TEST_02 dashboard should have published EAX widgets
            print("This probe needs a browser session with an open EAX widget.")
            print("Use a probe that creates a widget via UI instead.")
            return

        eax_id = eax_ids[0]
        print(f"\nTesting with EAX id: {eax_id}")

        # Get current dataRange
        result = _pp_post(request_context, base_url, {
            "GetEaxMd": {"tEax": {"id": eax_id}, "tArg": {"pattern": {"chart": True}}}
        })
        chart = result.get("GetEaxMdResult", {}).get("meta", {}).get("chart", {})
        dr = chart.get("dataRange", {})
        print(f"dataRange BEFORE: type={dr.get('type')}, w={dr.get('width')}, h={dr.get('height')}")

        # Get dims
        result = _pp_post(request_context, base_url, {"GetEaxMd": {"tEax": {"id": eax_id}, "tArg": {"pattern": {
            "dims": True,
            "dimArg": {
                "elsArg": {"totalCount": True, "selectionInfo": True, "filter": {"levels": 0, "elementsGroup": True}, "pattern": {"attributes": "*"}},
                "pattern": {"getDescr": True, "getIsAllSelected": True},
            },
        }}}})
        dims_raw = result.get("GetEaxMdResult", {}).get("meta", {}).get("dims", {})
        its = dims_raw.get("its", {}).get("it", []) if isinstance(dims_raw, dict) else []
        print(f"\nDims ({len(its)}):")
        for d in its:
            name = (d.get("descr") or {}).get("name", "?")
            print(f"  name={name}, key={d.get('key')}, isAllSelected={d.get('isAllSelected')}")
            print(f"  id={d.get('id')}")

        # Apply ChangeDimSelection(All) for each dim
        for d in its:
            dim_id = d.get("id") or f"{eax_id}!{d['key']}"
            name = (d.get("descr") or {}).get("name", "?")
            try:
                r = _pp_post(request_context, base_url, {"BatchExec": {"tArg": {"its": {"it": [
                    {"ChangeDimSelection": {
                        "tDim": {"id": dim_id},
                        "tArg": {
                            "elSelectOp": "Select",
                            "elRelative": "All",
                            "elKeys": {"it": []},
                            "ignoreMissingKeys": False,
                            "pattern": {"attributes": "*"},
                            "schemaNoApply": True,
                        },
                    }},
                ]}}}})
                print(f"  ChangeDimSelection(All) for {name}: OK")
            except Exception as e:
                print(f"  ChangeDimSelection(All) for {name}: ERROR {e}")

        # Refresh
        try:
            ref = _pp_post(request_context, base_url, {"SetEaxMd": {
                "tEax": {"id": eax_id},
                "tArg": {
                    "pattern": {"grid": True},
                    "meta": {"grid": {"dataDisplayMode": "Interactive"}},
                    "refresh": {"chart": True, "fetchData": True, "saveData": False},
                    "metaGet": {"chart": True},
                },
            }})
            ref_dr = ref.get("SetEaxMdResult", {}).get("meta", {}).get("chart", {}).get("dataRange", {})
            print(f"\ndataRange in refresh: type={ref_dr.get('type')}, w={ref_dr.get('width')}, h={ref_dr.get('height')}")
        except Exception as e:
            print(f"Refresh error: {e}")

        # Final check
        result = _pp_post(request_context, base_url, {
            "GetEaxMd": {"tEax": {"id": eax_id}, "tArg": {"pattern": {"chart": True}}}
        })
        chart = result.get("GetEaxMdResult", {}).get("meta", {}).get("chart", {})
        dr = chart.get("dataRange", {})
        print(f"\ndataRange AFTER: type={dr.get('type')}, w={dr.get('width')}, h={dr.get('height')}")


if __name__ == "__main__":
    main()
