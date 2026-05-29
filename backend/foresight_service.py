"""Foresight BI integration service for data_agent.

Provides:
  - SSH-based Metabase Python API access
  - .pefx file upload and apply
  - Dashboard export from data_agent → Foresight
"""
from __future__ import annotations

import csv
import json
import os
import random
import re
import string
import tempfile
from pathlib import Path
from typing import Any

import pexpect
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from backend.config import Settings

_cfg = Settings()

_SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "PreferredAuthentications=password",
    "-o", "PubkeyAuthentication=no",
]

_LD_PATH = (
    "export LANG=ru_RU.utf8;"
    " export LC_ALL=ru_RU.utf8;"
    " export LD_LIBRARY_PATH=/opt/foresight/fp10.x-biserver/bin:/opt/python/lib;"
    " unset PYTHONPATH;"
)
_PYTHON_RUN = "DISPLAY=:987 python3"

_METABASE_CONNECT = """
import sys
sys.path.append('/opt/foresight/fp10.x-biserver/bin/interops')
sys.path.append('/opt/foresight/fp10.x-biserver/bin/interops/python')
import metabase

m = metabase.CMetabaseManagerFactory_Create().Active
mbdef = m.Definitions.Add()
mbdef.Id = 'FS_DEMO'
mbdef.Name = 'Demo'
mbdef.SecurityPackage = 'STANDARDSECURITYPACKAGE'
mbdef.DriverId = 'POSTGRES'
mbdef.Authentication = 1
logon = mbdef.LogonData
for key, value in [
    ('SERVER', '127.0.0.1:5432'),
    ('DATABASE', 'FS_TRAINING_FORE'),
    ('DATABASE_ONLY', 'FS_TRAINING_FORE'),
    ('SCHEMA_ONLY', 'public'),
    ('CASESENSITIVE', True),
    ('Unicode', True),
]:
    logon.put_ParamValue(key, value)
spack = m.Packs.get_Item(0).Package
creds = metabase.IPasswordCredentials(spack.CreateCredentials(1).this)
creds.UserName = {repo_login!r}
creds.Password = {repo_password!r}
creds.UserOS = 'linux'
creds.UserStation = 'rocky-fp10'
mb = mbdef.OpenDefault(creds)
"""


def _ssh_run(command: str, timeout: int = 180) -> tuple[str, int | None]:
    """Run command on VM via SSH, auto-answer password prompt."""
    child = pexpect.spawn(
        "ssh",
        _SSH_OPTS + [
            "-p", str(_cfg.foresight_ssh_port),
            f"{_cfg.foresight_ssh_user}@{_cfg.foresight_ssh_host}",
            command,
        ],
        encoding="utf-8",
        timeout=timeout,
    )
    out: list[str] = []
    while True:
        idx = child.expect(
            [r"(?i)password:", pexpect.EOF, pexpect.TIMEOUT],
            timeout=timeout,
        )
        out.append(child.before or "")
        if idx == 0:
            child.sendline(_cfg.foresight_ssh_password)
        else:
            break
    child.close(force=True)
    return "".join(out), child.exitstatus


def _scp_upload(local_path: Path, remote_path: str, timeout: int = 60) -> int | None:
    """Upload file to VM via SCP."""
    child = pexpect.spawn(
        "scp",
        _SSH_OPTS + [
            "-P", str(_cfg.foresight_ssh_port),
            str(local_path),
            f"{_cfg.foresight_ssh_user}@{_cfg.foresight_ssh_host}:{remote_path}",
        ],
        encoding="utf-8",
        timeout=timeout,
    )
    out: list[str] = []
    while True:
        idx = child.expect(
            [r"(?i)password:", pexpect.EOF, pexpect.TIMEOUT],
            timeout=timeout,
        )
        out.append(child.before or "")
        if idx == 0:
            child.sendline(_cfg.foresight_ssh_password)
        else:
            break
    child.close(force=True)
    return child.exitstatus


def _scp_download(remote_path: str, local_path: Path, timeout: int = 60) -> int | None:
    """Download file from VM via SCP."""
    child = pexpect.spawn(
        "scp",
        _SSH_OPTS + [
            "-P", str(_cfg.foresight_ssh_port),
            f"{_cfg.foresight_ssh_user}@{_cfg.foresight_ssh_host}:{remote_path}",
            str(local_path),
        ],
        encoding="utf-8",
        timeout=timeout,
    )
    out: list[str] = []
    while True:
        idx = child.expect(
            [r"(?i)password:", pexpect.EOF, pexpect.TIMEOUT],
            timeout=timeout,
        )
        out.append(child.before or "")
        if idx == 0:
            child.sendline(_cfg.foresight_ssh_password)
        else:
            break
    child.close(force=True)
    return child.exitstatus


def _run_python_on_vm(script_body: str, timeout: int = 180) -> str:
    """Upload Python script to VM and run it with Foresight env, return stdout."""
    connect_block = _METABASE_CONNECT.format(
        repo_login=_cfg.foresight_repo_login,
        repo_password=_cfg.foresight_repo_password,
    )
    full_script = connect_block + "\n" + script_body

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(full_script)
        local_path = Path(f.name)

    remote_path = f"/tmp/da_{os.getpid()}.py"
    try:
        code = _scp_upload(local_path, remote_path, timeout=30)
        if code not in (0, None):
            raise RuntimeError(f"SCP upload failed: {code}")

        run_cmd = (
            f"{_LD_PATH} {_PYTHON_RUN} {remote_path} 2>&1;"
            f" rm -f {remote_path}"
        )
        output, _ = _ssh_run(run_cmd, timeout=timeout)
        return output
    finally:
        local_path.unlink(missing_ok=True)


def _extract_block(text: str, begin: str, end: str) -> str:
    m = re.search(
        rf"{re.escape(begin)}\s*(.*?)\s*{re.escape(end)}", text, re.S
    )
    if not m:
        raise RuntimeError(f"Block {begin!r}..{end!r} not found.\n{text[-2000:]}")
    return m.group(1).strip()


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def check_connection() -> dict:
    """Verify Foresight connectivity and return repo root info."""
    script = """
import json

root = mb.Root
desc = metabase.IMetabaseObjectDescriptor(root.this)
children = []
for i in range(desc.Children.Count):
    c = metabase.IMetabaseObjectDescriptor(desc.Children.get_Item(i).this)
    children.append({'key': int(c.Key), 'id': c.Id, 'name': c.Name, 'class': int(c.ClassId)})

print('RESULT_BEGIN')
print(json.dumps({'status': 'ok', 'root_id': desc.Id, 'children': children[:10]}, ensure_ascii=False))
print('RESULT_END')
"""
    raw = _run_python_on_vm(script, timeout=60)
    block = _extract_block(raw, "RESULT_BEGIN", "RESULT_END")
    return json.loads(block)


def apply_pefx(pefx_path: Path, object_id: str, parent_id: str) -> dict:
    """Upload a .pefx file and apply it to the Foresight repository."""
    remote_pkg = f"/tmp/{pefx_path.name}"
    remote_script = f"/tmp/apply_{os.getpid()}.py"

    script_body = f"""
import json, sys
sys.path.append('/opt/foresight/fp10.x-biserver/bin/interops')
sys.path.append('/opt/foresight/fp10.x-biserver/bin/interops/python')
import metabase

m = metabase.CMetabaseManagerFactory_Create().Active
mbdef = m.Definitions.Add()
mbdef.Id = 'FS_DEMO'
mbdef.Name = 'Demo'
mbdef.SecurityPackage = 'STANDARDSECURITYPACKAGE'
mbdef.DriverId = 'POSTGRES'
mbdef.Authentication = 1
logon = mbdef.LogonData
for key, value in [('SERVER','127.0.0.1:5432'),('DATABASE','FS_TRAINING_FORE'),('DATABASE_ONLY','FS_TRAINING_FORE'),('SCHEMA_ONLY','public'),('CASESENSITIVE',True),('Unicode',True)]:
    logon.put_ParamValue(key, value)
spack = m.Packs.get_Item(0).Package
creds = metabase.IPasswordCredentials(spack.CreateCredentials(1).this)
creds.UserName = {_cfg.foresight_repo_login!r}
creds.Password = {_cfg.foresight_repo_password!r}
creds.UserOS = 'linux'
creds.UserStation = 'rocky-fp10'
mb = mbdef.OpenDefault(creds)

result = {{'existing_key_before': int(mb.GetObjectKeyById({object_id!r}))}}
upd = mb.CreateUpdate()
upd.LoadFromFileNF({remote_pkg!r}, 0)
ctx = upd.CreateUpdateContext()
upd.ApplyEx(None, ctx)
try:
    result['log_count'] = int(upd.Log.Count)
except Exception:
    result['log_count'] = 0

key = int(mb.GetObjectKeyById({object_id!r}))
result['object_key_after'] = key
if key != 4294967295:
    obj = metabase.IMetabaseObjectDescriptor(mb.get_ItemById({object_id!r}).this)
    result['object'] = {{'id': obj.Id, 'name': obj.Name, 'key': int(obj.Key), 'class_id': int(obj.ClassId)}}

print('APPLY_BEGIN')
print(json.dumps(result, ensure_ascii=False, indent=2))
print('APPLY_END')
"""

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(script_body)
        local_script = Path(f.name)

    try:
        code = _scp_upload(pefx_path, remote_pkg, timeout=60)
        if code not in (0, None):
            raise RuntimeError(f"SCP pefx upload failed: {code}")

        code = _scp_upload(local_script, remote_script, timeout=30)
        if code not in (0, None):
            raise RuntimeError(f"SCP script upload failed: {code}")

        run_cmd = (
            f"{_LD_PATH} {_PYTHON_RUN} {remote_script} 2>&1;"
            f" rm -f {remote_script} {remote_pkg}"
        )
        output, _ = _ssh_run(run_cmd, timeout=300)
        block = _extract_block(output, "APPLY_BEGIN", "APPLY_END")
        return json.loads(block)
    finally:
        local_script.unlink(missing_ok=True)


def export_dashboard_pefx(object_id: str, description: str = "") -> bytes:
    """Export a Foresight object to .pefx bytes via Metabase API."""
    remote_out = f"/tmp/da_export_{os.getpid()}.pefx"
    script = f"""
import json

obj = mb.get_ItemById({object_id!r})
upd = mb.CreateUpdate()
upd.Description = {description!r}
upd.BoundType = 2
root = upd.RootFolder
node = metabase.IMetabaseUpdateObjectNode(root.Add(1).this)
node.Object = obj
node.Label = obj.Id
node.UpdatePart = 1
node.BoundType = 2
node.IncludeChildrenDependencies = 1
upd.Prepare()
upd.SaveToFileNF({remote_out!r})
print('EXPORT_OK')
"""
    raw = _run_python_on_vm(script, timeout=120)
    if "EXPORT_OK" not in raw:
        raise RuntimeError(f"Export failed: {raw[-1000:]}")

    local_tmp = Path(tempfile.mktemp(suffix=".pefx"))
    try:
        code = _scp_download(remote_out, local_tmp, timeout=60)
        if code not in (0, None):
            raise RuntimeError(f"SCP download failed: {code}")
        data = local_tmp.read_bytes()
        _ssh_run(f"rm -f {remote_out}", timeout=15)
        return data
    finally:
        local_tmp.unlink(missing_ok=True)


def clone_dashboard(
    source_id: str = "IP_COVID19_RUS",
    new_id: str = "DA_DASHBOARD",
    new_name: str = "Data Agent: Аналитический дашборд",
    parent_id: str = "F_EXAMPLES",
    export_copy: bool = True,
) -> dict:
    """Clone an existing Foresight dashboard using native Metabase CopyObject."""
    remote_out = f"/tmp/{new_id.lower()}_{os.getpid()}_clone.pefx"
    script = f"""
import json

result = {{
    'source_id': {source_id!r},
    'new_id': {new_id!r},
    'parent_id': {parent_id!r},
    'existing_key_before': int(mb.GetObjectKeyById({new_id!r})),
}}

if result['existing_key_before'] == 4294967295:
    src = mb.get_ItemById({source_id!r})
    parent = metabase.IMetabaseObjectDescriptor(mb.get_ItemById({parent_id!r}).this)
    ci = mb.CreateCopyInfo()
    ci.Source = src
    ci.Destination = parent
    ci.Id = {new_id!r}
    ci.Name = {new_name!r}
    mb.CopyObject(ci)
    result['copy_ok'] = True
else:
    result['copy_skipped'] = 'already exists'

key = int(mb.GetObjectKeyById({new_id!r}))
result['object_key_after'] = key
if key != 4294967295:
    obj = metabase.IMetabaseObjectDescriptor(mb.get_ItemById({new_id!r}).this)
    result['object'] = {{
        'id': obj.Id,
        'name': obj.Name,
        'key': int(obj.Key),
        'class_id': int(obj.ClassId),
        'parent_id': obj.Parent.Id if obj.Parent else None,
        'parent_key': int(obj.Parent.Key) if obj.Parent else None,
    }}

    if {export_copy!r}:
        upd = mb.CreateUpdate()
        upd.Description = {f'{new_name} export'!r}
        upd.BoundType = 2
        root = upd.RootFolder
        node = metabase.IMetabaseUpdateObjectNode(root.Add(1).this)
        node.Object = obj
        node.Label = obj.Id
        node.UpdatePart = 1
        node.BoundType = 2
        node.IncludeChildrenDependencies = 1
        upd.Prepare()
        upd.SaveToFileNF({remote_out!r})
        import os
        result['export_path'] = {remote_out!r}
        result['export_size'] = os.path.getsize({remote_out!r})

print('CLONE_BEGIN')
print(json.dumps(result, ensure_ascii=False, indent=2))
print('CLONE_END')
"""
    raw = _run_python_on_vm(script, timeout=360)
    block = _extract_block(raw, "CLONE_BEGIN", "CLONE_END")
    result = json.loads(block)

    if export_copy and result.get("export_path"):
        local_tmp = Path(tempfile.mktemp(suffix=".pefx"))
        try:
            code = _scp_download(result["export_path"], local_tmp, timeout=120)
            if code in (0, None):
                result["export_bytes"] = len(local_tmp.read_bytes())
            else:
                result["export_download_error"] = f"SCP download failed: {code}"
        finally:
            local_tmp.unlink(missing_ok=True)
            _ssh_run(f"rm -f {result['export_path']}", timeout=15)

    return result


def clone_template_dashboard(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Clone the confirmed working 8448 Foresight dashboard template.

    This is the lowest-risk integration path for "copy dashboard" scenarios:
    the source dashboard already has materialized blocks and persisted DSO
    settings, including `dataRange=None` for the primary widgets.
    """
    payload = payload or {}
    source_id = str(payload.get("source_id") or "DA_PERM_TEST_02")
    suffix = "".join(random.choice(string.digits) for _ in range(6))
    new_id = str(payload.get("new_id") or payload.get("foresight_object_id") or f"DA_TEMPLATE_CLONE_{suffix}")
    new_name = str(payload.get("new_name") or payload.get("dashboard_title") or payload.get("title") or "Data Agent: Foresight template clone")
    parent_id = str(payload.get("parent_id") or "F_EXAMPLES")

    result = clone_dashboard(
        source_id=source_id,
        new_id=new_id,
        new_name=new_name,
        parent_id=parent_id,
        export_copy=bool(payload.get("export_copy", False)),
    )
    key = result.get("object_key_after")
    base = _cfg.foresight_base_url.rstrip("/")
    if key and key != 4294967295:
        result["view_url"] = f"{base}/app/dashboard.html#key={key}&mode=view&name=Dashboard&repo={_cfg.foresight_repo_id}"
        result["edit_url"] = f"{base}/app/dashboard.html#key={key}&mode=edit&name=Dashboard&repo={_cfg.foresight_repo_id}"
    result["mode"] = "template_clone"
    result["template_source_id"] = source_id
    return result


_PP_HEADERS = {
    "content-type": "application/json;charset=UTF-8",
    "get-ppbi-time": "1",
}


def _random_id(length: int = 16) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "S" + "".join(random.choice(alphabet) for _ in range(length - 1))


def _mvp_rows(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    payload = payload or {}
    tables = payload.get("tables") if isinstance(payload.get("tables"), list) else []
    if tables:
        first = tables[0] if isinstance(tables[0], dict) else {}
        rows = first.get("rows") if isinstance(first.get("rows"), list) else []
        row_limit_raw = payload.get("foresight_row_limit") or payload.get("inline_row_limit")
        try:
            row_limit = int(row_limit_raw) if row_limit_raw not in (None, "") else 0
        except (TypeError, ValueError):
            row_limit = 0
        effective_row_limit = row_limit if row_limit > 0 else None
        normalized: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            normalized.append(
                {
                    "period": row.get("period") or row.get("date") or row.get("month") or "",
                    "category": row.get("category") or row.get("segment") or row.get("name") or "",
                    "revenue": row.get("revenue") or row.get("sales") or row.get("amount") or 0,
                    "profit": row.get("profit") or row.get("margin_value") or 0,
                    "orders": row.get("orders") or row.get("count") or 0,
                }
            )
        normalized = [row for row in normalized if row["period"] and row["category"]]
        if normalized:
            return normalized[:effective_row_limit] if effective_row_limit is not None else normalized

    return [
        {"period": "2025-01", "category": "Electronics", "revenue": 4200000, "profit": 1100000, "orders": 1250},
        {"period": "2025-01", "category": "Fashion", "revenue": 1800000, "profit": 600000, "orders": 950},
        {"period": "2025-02", "category": "Electronics", "revenue": 4500000, "profit": 1200000, "orders": 1340},
        {"period": "2025-02", "category": "Fashion", "revenue": 2100000, "profit": 700000, "orders": 1050},
        {"period": "2025-03", "category": "Electronics", "revenue": 5100000, "profit": 1400000, "orders": 1520},
        {"period": "2025-03", "category": "Fashion", "revenue": 2400000, "profit": 800000, "orders": 1180},
    ]


def _write_csv(path: Path, rows: list[dict[str, Any]], metric_fields: list[str]) -> None:
    fieldnames = ["period", "category", *metric_fields]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def _pp_post(request_context: Any, base_url: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = request_context.post(
        f"{base_url.rstrip('/')}/app/PPService.axd",
        data=json.dumps(payload, ensure_ascii=False),
        headers=_PP_HEADERS,
    )
    text = response.text()
    if response.status != 200:
        raise RuntimeError(f"PPService error {response.status}: {text[:1200]}")
    return json.loads(text)


def _set_visualizer_mode(
    request_context: Any,
    base_url: str,
    eax_id: str,
    widget_type: str,
) -> None:
    if widget_type in {"bar", "line"}:
        chart_type = "column" if widget_type == "bar" else "line"
        _pp_post(
            request_context,
            base_url,
            {
                "SetEaxMd": {
                    "tEax": {"id": eax_id},
                    "tArg": {
                        "pattern": {
                            "setChart": {
                                "meta": {
                                    "hiChart": json.dumps(
                                        {
                                            "chart": {"defaultSeriesType": chart_type},
                                            "plotOptions": {"series": {}},
                                            "template": None,
                                        },
                                        ensure_ascii=False,
                                    )
                                }
                            }
                        },
                        "meta": {},
                    },
                }
            },
        )
        mode_meta = {
            "chart": {"enabled": True, "visible": True, "active": True, "viewOrder": 0},
            "bubbleChart": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
            "bubbleTree": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
            "treeMap": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
            "grid": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
            "mapChart": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
            "speedometer": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
        }
    elif widget_type == "table":
        mode_meta = {
            "chart": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
            "bubbleChart": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
            "bubbleTree": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
            "treeMap": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
            "grid": {"enabled": True, "visible": True, "active": True, "viewOrder": 0},
            "mapChart": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
            "speedometer": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
        }
    else:
        mode_meta = {
            "chart": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
            "bubbleChart": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
            "bubbleTree": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
            "treeMap": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
            "grid": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
            "mapChart": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
            "speedometer": {"enabled": True, "visible": True, "active": True, "viewOrder": 0},
        }

    _pp_post(
        request_context,
        base_url,
        {
            "SetEaxMd": {
                "tEax": {"id": eax_id},
                "tArg": {
                    "pattern": {
                        "grid": True,
                        "chart": True,
                        "bubbleChart": True,
                        "bubbleTree": True,
                        "treeMap": True,
                        "mapChart": True,
                        "speedometer": True,
                    },
                    "meta": mode_meta,
                    "metaGet": {"chart": True, "grid": True, "speedometer": True},
                },
            }
        },
    )


def _add_widget(
    request_context: Any,
    base_url: str,
    adhoc_id: str,
    widget_type: str,
    cube_key: int,
) -> dict[str, Any]:
    dso_id = _random_id()
    _pp_post(
        request_context,
        base_url,
        {
            "SetAdHoc": {
                "tAdHocId": {"id": adhoc_id},
                "tArg": {
                    "meta": {"dataSourceObjects": {"its": {"it": [{"createNew": 2561, "id": dso_id, "slideKey": "1"}]}}},
                    "pattern": {"dataSourceObjects": "Add"},
                    "metaGet": {"dataSourceObjects": "Get"},
                },
            }
        },
    )
    eax_id = f"{adhoc_id}!DSO!{dso_id}"
    _set_visualizer_mode(request_context, base_url, eax_id, widget_type)
    _pp_post(
        request_context,
        base_url,
        {
            "SetEaxMd": {
                "tEax": {"id": eax_id},
                "tArg": {
                    "pattern": {"dataSources": "Set"},
                    "meta": {
                        "dataSources": {
                            "its": {"it": [{"k": 0, "vis": True, "cube": {"obDesc": {"n": "", "i": "", "k": cube_key, "c": 0}}}]},
                            "OpenOptions": "DataAndSelection",
                        }
                    },
                    "refresh": {
                        "fetchData": True,
                        "map": True,
                        "grid": True,
                        "bubbleTree": True,
                        "treeMap": True,
                        "chart": True,
                        "speedometer": True,
                        "saveData": False,
                    },
                    "metaGet": {"chart": True, "grid": True, "speedometer": True, "dataSources": "Get"},
                },
            }
        },
    )
    return {"dso_id": dso_id, "eax_id": eax_id, "type": widget_type, "cube_key": cube_key}


def _layout_prop(left: float, top: float, width: float, height: float) -> dict[str, Any]:
    right = max(0.0, 100.0 - left - width)
    bottom = max(0.0, 100.0 - top - height)
    return {
        "@tag": "layout",
        "prop": [
            {"@tag": "left", "@val": f"{left:.2f}"},
            {"@tag": "right", "@val": f"{right:.2f}"},
            {"@tag": "top", "@val": f"{top:.2f}"},
            {"@tag": "bottom", "@val": f"{bottom:.2f}"},
            {"@tag": "leftUnit", "@val": "%"},
            {"@tag": "rightUnit", "@val": "%"},
            {"@tag": "topUnit", "@val": "%"},
            {"@tag": "bottomUnit", "@val": "%"},
            {"@tag": "anchorLeft", "@val": "1"},
            {"@tag": "anchorTop", "@val": "1"},
            {"@tag": "anchorRight", "@val": "1"},
            {"@tag": "anchorBottom", "@val": "1"},
        ],
    }


def _dashboard_block(dso_id: str, name: str, widget_type: str, rect: tuple[float, float, float, float]) -> dict[str, Any]:
    if widget_type == "kpi":
        block_type = "Gauge"
    elif widget_type == "table":
        block_type = "Table"
    else:
        block_type = "Chart"
    left, top, width, height = rect
    return {
        "@key": dso_id,
        "block": {
            "@type": block_type,
            "@key": dso_id,
            "prop": [
                {"@tag": "name", "@val": name},
                {
                    "@tag": "background",
                    "prop": [
                        {"@tag": "useBackground", "@val": "1"},
                        {"@tag": "backgroundColor", "@val": "#ffffff"},
                        {"@tag": "useGradient", "@val": "0"},
                        {"@tag": "gradientColor", "@val": "#c9c9c9"},
                        {"@tag": "gradientAngle", "@val": "270"},
                    ],
                },
                _layout_prop(left, top, width, height),
                {"@tag": "margins", "prop": {"@tag": "useMargins", "@val": "1"}},
                {"@tag": "interactivity", "@val": "1"},
                {
                    "@tag": "decor",
                    "prop": [
                        {"@tag": "cornerRadius", "@val": "5"},
                        {"@tag": "useBorderRadius", "@val": "1"},
                        {"@tag": "useBorder", "@val": "0"},
                        {"@tag": "borderColor", "@val": "#c9c9c9"},
                        {"@tag": "borderWidth", "@val": "1"},
                        {"@tag": "useShadow", "@val": "0"},
                        {"@tag": "shadowColor", "@val": "#000000"},
                        {"@tag": "shadowWidth", "@val": "8"},
                        {"@tag": "shadowOpacity", "@val": "10"},
                        {
                            "@tag": "paddings",
                            "prop": [
                                {"@tag": "usePaddings", "@val": "1"},
                                {"@tag": "left", "@val": "10"},
                                {"@tag": "right", "@val": "10"},
                                {"@tag": "top", "@val": "10"},
                                {"@tag": "bottom", "@val": "10"},
                            ],
                        },
                    ],
                },
                {
                    "@tag": "title",
                    "prop": [
                        {"@tag": "show", "@val": "1"},
                        {
                            "@tag": "font",
                            "prop": [
                                {"@tag": "color", "@val": "#48494c"},
                                {"@tag": "family", "@val": "Arial"},
                                {"@tag": "isBold", "@val": "1"},
                                {"@tag": "size", "@val": "14"},
                            ],
                        },
                        {"@tag": "align", "@val": "Left"},
                    ],
                },
                {
                    "@tag": "divider",
                    "prop": [
                        {"@tag": "show", "@val": "0"},
                        {"@tag": "color", "@val": "#c9c9c9"},
                        {"@tag": "height", "@val": "1"},
                        {"@tag": "blurSize", "@val": "25"},
                        {"@tag": "useBlur", "@val": "1"},
                    ],
                },
                {
                    "@tag": "tableAdaptability",
                    "prop": [
                        {"@tag": "checkTableAdaptability", "@val": "0"},
                        {"@tag": "autoAdjust", "@val": "1"},
                        {"@tag": "zoom", "@val": "0"},
                        {"@tag": "autoAdjustInWidth", "@val": "1"},
                        {"@tag": "autoAdjustInHeight", "@val": "1"},
                    ],
                },
            ],
        },
    }


def _set_widget_layout(
    request_context: Any,
    base_url: str,
    adhoc_id: str,
    widgets: list[dict[str, Any]],
) -> None:
    rects = [
        (0.0, 0.0, 32.5, 20.0),
        (33.75, 0.0, 32.5, 20.0),
        (67.5, 0.0, 32.5, 20.0),
        (0.0, 22.5, 49.0, 35.0),
        (51.0, 22.5, 49.0, 35.0),
        (0.0, 60.0, 100.0, 40.0),
    ]
    names = ["KPI 1", "KPI 2", "KPI 3", "Table", "Bar chart", "Line chart"]
    areas = [
        _dashboard_block(str(widget["dso_id"]), names[index], str(widget["type"]), rects[index])
        for index, widget in enumerate(widgets[: len(rects)])
        if widget.get("dso_id")
    ]
    slide_key = _random_id()
    _pp_post(
        request_context,
        base_url,
        {
            "SetAdHoc": {
                "tAdHocId": {"id": adhoc_id},
                "tArg": {
                    "meta": {
                        "Md8": {
                            "activeSlideKey": 1,
                            "slides": {
                                "its": {
                                    "it": [
                                        {
                                            "key": 1,
                                            "mainPanel": {
                                                "block": {
                                                    "@type": "Slide",
                                                    "@key": slide_key,
                                                    "prop": [
                                                        {"@tag": "name", "@val": "Slide 1"},
                                                        {
                                                            "@tag": "background",
                                                            "prop": [
                                                                {"@tag": "useBackground", "@val": "1"},
                                                                {"@tag": "backgroundColor", "@val": "#f4f4f4"},
                                                                {"@tag": "useGradient", "@val": "0"},
                                                                {"@tag": "gradientColor", "@val": "#c9c9c9"},
                                                                {"@tag": "gradientAngle", "@val": "270"},
                                                            ],
                                                        },
                                                        {"@tag": "margins", "prop": {"@tag": "useMargins", "@val": "0"}},
                                                        {"@tag": "interactivity", "@val": "1"},
                                                        {
                                                            "@tag": "decor",
                                                            "prop": {
                                                                "@tag": "paddings",
                                                                "prop": [
                                                                    {"@tag": "usePaddings", "@val": "0"},
                                                                    {"@tag": "left", "@val": "10"},
                                                                    {"@tag": "right", "@val": "10"},
                                                                    {"@tag": "top", "@val": "10"},
                                                                    {"@tag": "bottom", "@val": "10"},
                                                                ],
                                                            },
                                                        },
                                                        {"@tag": "layouts", "area": areas},
                                                    ],
                                                }
                                            },
                                        }
                                    ]
                                }
                            },
                        }
                    },
                    "pattern": {"layout": {"activeSlideKey": True, "slides": "Change"}},
                },
            }
        },
    )
    _pp_post(
        request_context,
        base_url,
        {
            "SetAdHoc": {
                "tAdHocId": {"id": adhoc_id},
                "tArg": {
                    "meta": {
                        "Md": {
                            "kap": {
                                "@version": "10.8",
                                "block": {
                                    "@type": "Dashboard",
                                    "@key": _random_id(),
                                    "prop": [
                                        {"@tag": "name", "@val": "Dashboard"},
                                        {
                                            "@tag": "background",
                                            "prop": [
                                                {"@tag": "useBackground", "@val": "0"},
                                                {"@tag": "backgroundColor", "@val": "#ffffff"},
                                                {"@tag": "useGradient", "@val": "0"},
                                                {"@tag": "gradientColor", "@val": "#c9c9c9"},
                                                {"@tag": "gradientAngle", "@val": "270"},
                                            ],
                                        },
                                        {"@tag": "margins", "prop": {"@tag": "useMargins", "@val": "0"}},
                                        {"@tag": "interactivity", "@val": "1"},
                                        {
                                            "@tag": "decor",
                                            "prop": {
                                                "@tag": "paddings",
                                                "prop": [
                                                    {"@tag": "usePaddings", "@val": "1"},
                                                    {"@tag": "left", "@val": "10"},
                                                    {"@tag": "right", "@val": "10"},
                                                    {"@tag": "top", "@val": "10"},
                                                    {"@tag": "bottom", "@val": "10"},
                                                ],
                                            },
                                        },
                                        {"@tag": "autoLayout", "@val": "1"},
                                        {"@tag": "pageLayout", "prop": {"@tag": "sizeMode", "@val": "stretch"}},
                                        {"@tag": "counter", "@val": str(len(areas))},
                                    ],
                                },
                            }
                        }
                    },
                    "pattern": {"md": True},
                },
            }
        },
    )


def _run_initial_import(page: Any, csv_path: Path) -> None:
    def dump_debug(name: str) -> None:
        debug_dir = Path("/tmp/foresight_mvp_debug")
        debug_dir.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(debug_dir / f"{name}.png"), full_page=True)
        (debug_dir / f"{name}.txt").write_text(page.locator("body").inner_text(), encoding="utf-8")
        (debug_dir / f"{name}.html").write_text(page.content(), encoding="utf-8")

    def has_block_data_panel() -> bool:
        body_text = page.locator("body").inner_text()
        return "Block 1" in body_text and "Data sources" in body_text

    try:
        page.locator("#InsertCategory").click(timeout=5000)
    except Exception:
        page.mouse.click(220, 42)
    page.wait_for_timeout(500)
    for attempt in range(2):
        page.mouse.click(286, 98)
        page.wait_for_timeout(700)
        page.mouse.click(290, 154)
        page.wait_for_timeout(3000)
        if has_block_data_panel():
            break
    else:
        dump_debug("chart_insert_failure")
        raise RuntimeError("Could not create the initial chart block for import")

    try:
        page.get_by_text("Data import", exact=False).last.click(timeout=5000, force=True)
    except Exception as exc:
        dump_debug("import_open_failure")
        raise RuntimeError("Import wizard did not open") from exc

    page.wait_for_timeout(2000)

    try:
        page.get_by_text("File with data", exact=False).click(timeout=5000, force=True)
    except Exception:
        page.mouse.click(585, 450)
    page.wait_for_timeout(1000)

    try:
        with page.expect_file_chooser(timeout=5000) as fc_info:
            page.get_by_text("Next >", exact=True).last.click(timeout=5000, force=True)
        fc_info.value.set_files(str(csv_path))
    except Exception:
        try:
            page.get_by_text("Next >", exact=True).last.click(timeout=5000, force=True)
        except Exception:
            page.mouse.click(970, 727)
        page.wait_for_timeout(3000)
        try:
            with page.expect_file_chooser(timeout=5000) as fc_info:
                page.get_by_text("Browse", exact=False).click(timeout=5000, force=True)
            fc_info.value.set_files(str(csv_path))
        except Exception as exc:
            dump_debug("import_upload_failure")
            raise RuntimeError("Import wizard did not expose a file chooser") from exc

    page.wait_for_timeout(5000)

    try:
        page.get_by_text("Next >", exact=True).last.click(timeout=5000, force=True)
    except Exception:
        page.mouse.click(970, 727)
    page.wait_for_timeout(5000)

    try:
        page.get_by_text("Import", exact=True).last.click(timeout=5000, force=True)
    except Exception:
        page.mouse.click(1055, 727)
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


def publish_mvp_dashboard(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    rows = _mvp_rows(payload)
    title = str(payload.get("title") or payload.get("dashboard_title") or "Data Agent MVP")
    widget_plan = ["kpi", "kpi", "kpi", "table", "bar", "line"]

    with tempfile.TemporaryDirectory(prefix="foresight_mvp_") as tmpdir:
        tmp = Path(tmpdir)
        full_csv = tmp / "mvp_full.csv"
        revenue_csv = tmp / "mvp_revenue.csv"
        profit_csv = tmp / "mvp_profit.csv"
        orders_csv = tmp / "mvp_orders.csv"
        _write_csv(full_csv, rows, ["revenue", "profit", "orders"])
        _write_csv(revenue_csv, rows, ["revenue"])
        _write_csv(profit_csv, rows, ["profit"])
        _write_csv(orders_csv, rows, ["orders"])

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1600, "height": 900})
            page = context.new_page()
            state: dict[str, Any] = {"root_id": None, "metabase_id": None, "adhoc_id": None, "cube_keys": []}

            def on_request(req: Any) -> None:
                post = req.post_data or ""
                if state["root_id"] is None and "!M!Root" in post:
                    match = re.search(r'([A-Z0-9]+!M!Root)', post)
                    if match:
                        state["root_id"] = match.group(1)
                        state["metabase_id"] = state["root_id"][:-5]
                if state["adhoc_id"] is None and '"tAdHocId":{"id":"' in post:
                    match = re.search(r'"tAdHocId":\{"id":"([^"]+)"\}', post)
                    if match:
                        state["adhoc_id"] = match.group(1)
                cube_match = re.search(r'"cube":\{"obDesc":\{.*?"k":(\d+)', post)
                if cube_match:
                    cube_key = int(cube_match.group(1))
                    if cube_key not in state["cube_keys"]:
                        state["cube_keys"].append(cube_key)

            page.on("request", on_request)

            page.goto(f"{_cfg.foresight_base_url.rstrip('/')}/app/login.html#repo={_cfg.foresight_repo_id}", wait_until="networkidle", timeout=60000)
            page.fill('input[name="username"]', _cfg.foresight_repo_login)
            page.fill('input[type="password"]', _cfg.foresight_repo_password)
            page.keyboard.press("Enter")
            page.wait_for_timeout(5000)
            try:
                page.get_by_text("Dashboards", exact=True).click(timeout=5000)
            except Exception:
                page.mouse.click(512, 420)
            for _ in range(20):
                if state["root_id"]:
                    break
                page.wait_for_timeout(500)
            if not state["root_id"]:
                raise RuntimeError("Could not capture Foresight Root session id")

            try:
                page.get_by_text("New", exact=True).last.click(timeout=3000, force=True)
            except Exception:
                page.mouse.click(515, 580)
            for _ in range(20):
                if state["adhoc_id"]:
                    break
                page.wait_for_timeout(500)
            if not state["adhoc_id"]:
                raise RuntimeError("Could not capture Foresight AdHoc id after creating the dashboard")

            # Use the UI-import flow for the first bar chart because it is the only
            # traced path that reliably creates the actual cube in the current demo VM.
            _run_initial_import(page, full_csv)

            body_text = page.locator("body").inner_text()
            if not state["cube_keys"]:
                raise RuntimeError("Foresight import finished without a captured cube key")
            latest_hint = max(state["cube_keys"])

            # The traced platform stores the actual cube object next to the imported folder; in
            # the current demo repository it has been the first created object immediately after import.
            base_cube_key = latest_hint
            revenue_cube_key = latest_hint
            profit_cube_key = latest_hint
            orders_cube_key = latest_hint

            widgets = []
            widgets.append(_add_widget(context.request, _cfg.foresight_base_url, state["adhoc_id"], "kpi", revenue_cube_key))
            widgets.append(_add_widget(context.request, _cfg.foresight_base_url, state["adhoc_id"], "kpi", profit_cube_key))
            widgets.append(_add_widget(context.request, _cfg.foresight_base_url, state["adhoc_id"], "kpi", orders_cube_key))
            widgets.append(_add_widget(context.request, _cfg.foresight_base_url, state["adhoc_id"], "table", base_cube_key))
            widgets.append(_add_widget(context.request, _cfg.foresight_base_url, state["adhoc_id"], "bar", base_cube_key))
            widgets.append(_add_widget(context.request, _cfg.foresight_base_url, state["adhoc_id"], "line", base_cube_key))
            _set_widget_layout(context.request, _cfg.foresight_base_url, state["adhoc_id"], widgets)
            page.wait_for_timeout(5000)
            page.reload(wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(5000)

            screenshot = tmp / "foresight_mvp.png"
            page.screenshot(path=str(screenshot), full_page=True)
            body_text = page.locator("body").inner_text()
            result = {
                "status": "ok",
                "mode": "mvp_web_session",
                "temporary": True,
                "dashboard": {"n": title, "c": 8448, "k": None, "i": None},
                "adhoc_id": state["adhoc_id"],
                "root_id": state["root_id"],
                "metabase_id": state["metabase_id"],
                "rows": len(rows),
                "widget_types": widget_plan,
                "widgets": widgets,
                "cube_key_hint": latest_hint,
                "screenshot_bytes": screenshot.read_bytes(),
                "body_excerpt": body_text[:4000],
                "layout_blocks": len(widgets),
            }
            browser.close()
            return result


# ─────────────────────────────────────────────────────────────────────────────
# Dynamic dashboard publish — reads real payload from Data Agent
# ─────────────────────────────────────────────────────────────────────────────

_GRID_COLS = 12


def _csv_from_payload(path: Path, payload: dict[str, Any]) -> None:
    """Write a single flat CSV from payload tables[0].rows."""
    tables = payload.get("tables") or []
    if not tables:
        raise RuntimeError("payload has no tables")
    table = tables[0]
    columns: list[str] = [c if isinstance(c, str) else c.get("name", "") for c in (table.get("columns") or [])]
    rows: list[dict[str, Any]] = table.get("rows") or []
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in columns})


def _csv_for_chart_inline(path: Path, chart: dict[str, Any]) -> bool:
    """Write CSV from chart.rows / chart.columns if present. Returns True on success."""
    rows = chart.get("rows")
    cols = chart.get("columns")
    if not rows or not cols:
        return False
    fieldnames = [c if isinstance(c, str) else c.get("name", "") for c in cols]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in fieldnames})
    return True


def _csv_for_metric(
    path: Path, payload: dict[str, Any], y_field: str, x_field: str | None,
) -> None:
    """Write a CSV with only the dimension column(s) + one metric column for per-widget cube isolation."""
    tables = payload.get("tables") or []
    if not tables:
        raise RuntimeError("payload has no tables")
    table = tables[0]
    all_columns: list[str] = table.get("columns") or []
    rows: list[dict[str, Any]] = table.get("rows") or []

    # Always include dimension columns (non-numeric or explicitly specified)
    keep = []
    if x_field and x_field in all_columns:
        keep.append(x_field)
    # Add the metric column
    if y_field in all_columns and y_field not in keep:
        keep.append(y_field)
    # If nothing matched, write full CSV
    if not keep:
        keep = all_columns

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=keep)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in keep})


def _has_data_panel(page: Any) -> bool:
    return "Data sources" in page.locator("body").inner_text()


def _expand_details_panel(page: Any) -> bool:
    try:
        button = page.locator("#ExpandDetailsButton")
        if button.count():
            button.click(timeout=3000, force=True)
            page.wait_for_timeout(800)
    except Exception:
        pass
    return _has_data_panel(page)


def _save_current_dashboard(page: Any) -> None:
    page.keyboard.press("Control+S")
    page.wait_for_timeout(5000)


def _reopen_saved_dashboard(page: Any, edit_url: str) -> None:
    page.goto(edit_url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(8000)


def _run_subsequent_import(page: Any, csv_path: Path) -> None:
    """Run a second (or later) CSV import within the same Foresight dashboard session.
    Inserts a new chart block via INSERT menu, then goes through the import wizard."""
    debug_dir = Path("/tmp/foresight_sub_debug")
    debug_dir.mkdir(parents=True, exist_ok=True)
    _sub_idx = len(list(debug_dir.glob("step_*.png")))

    def dump_debug(name: str) -> None:
        page.screenshot(path=str(debug_dir / f"{name}.png"), full_page=True)

    def has_data_panel() -> bool:
        return _has_data_panel(page)

    # Insert > Chart > Column chart (same as initial import)
    try:
        page.locator("#InsertCategory").click(timeout=5000)
    except Exception:
        page.mouse.click(220, 42)
    page.wait_for_timeout(500)
    for attempt in range(2):
        page.mouse.click(286, 98)
        page.wait_for_timeout(700)
        page.mouse.click(290, 154)
        page.wait_for_timeout(3000)
        if has_data_panel():
            break

    dump_debug(f"step_{_sub_idx:02d}_after_insert_{csv_path.stem}")

    if not has_data_panel():
        _expand_details_panel(page)

    if not has_data_panel():
        # Escape dismisses any active capture mask left by the insert operation
        page.keyboard.press("Escape")
        page.wait_for_timeout(800)
        _expand_details_panel(page)
        dump_debug(f"step_{_sub_idx:02d}_after_escape_{csv_path.stem}")

    if not has_data_panel():
        _expand_details_panel(page)

    # Click "Data import..." link in the Data sources panel
    dump_debug(f"step_{_sub_idx:02d}_before_import_click_{csv_path.stem}")
    (debug_dir / f"step_{_sub_idx:02d}_before_import_click_{csv_path.stem}.txt").write_text(
        page.locator("body").inner_text()[:3000], encoding="utf-8"
    )
    try:
        page.get_by_text("Data import", exact=False).last.click(timeout=8000, force=True)
    except Exception:
        dump_debug(f"step_{_sub_idx:02d}_import_open_failure")
        raise RuntimeError(f"Subsequent import: 'Data import' not found for {csv_path.name}")

    page.wait_for_timeout(2000)

    try:
        page.get_by_text("File with data", exact=False).click(timeout=5000, force=True)
    except Exception:
        page.mouse.click(585, 450)
    page.wait_for_timeout(1000)

    # Next > triggers file chooser
    try:
        with page.expect_file_chooser(timeout=5000) as fc_info:
            page.get_by_text("Next >", exact=True).last.click(timeout=5000, force=True)
        fc_info.value.set_files(str(csv_path))
    except Exception:
        try:
            page.get_by_text("Next >", exact=True).last.click(timeout=5000, force=True)
        except Exception:
            page.mouse.click(970, 727)
        page.wait_for_timeout(3000)
        try:
            with page.expect_file_chooser(timeout=5000) as fc_info:
                page.get_by_text("Browse", exact=False).click(timeout=5000, force=True)
            fc_info.value.set_files(str(csv_path))
        except Exception:
            dump_debug(f"step_{_sub_idx:02d}_upload_failure")
            raise RuntimeError(f"Subsequent import: file chooser not available for {csv_path.name}")

    page.wait_for_timeout(5000)
    dump_debug(f"step_{_sub_idx:02d}_after_upload_{csv_path.stem}")

    try:
        page.get_by_text("Next >", exact=True).last.click(timeout=5000, force=True)
    except Exception:
        page.mouse.click(970, 727)
    page.wait_for_timeout(5000)

    try:
        page.get_by_text("Import", exact=True).last.click(timeout=5000, force=True)
    except Exception:
        page.mouse.click(1055, 727)
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
    dump_debug(f"step_{_sub_idx:02d}_done_{csv_path.stem}")


def _hiChart_type(chart_type: str) -> str:
    return {
        "bar": "column", "hbar": "bar", "line": "line",
        "area": "area", "scatter": "scatter",
        "pie": "pie", "donut": "pie",
    }.get(chart_type, "column")


def _set_visualizer_mode_typed(
    request_context: Any, base_url: str, eax_id: str, chart_type: str,
) -> None:
    if chart_type == "table":
        mode_meta = {
            "chart": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
            "grid": {"enabled": True, "visible": True, "active": True, "viewOrder": 0},
            "speedometer": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
            "bubbleChart": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
            "bubbleTree": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
            "treeMap": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
            "mapChart": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
        }
    elif chart_type == "big_number":
        mode_meta = {
            "chart": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
            "grid": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
            "speedometer": {"enabled": True, "visible": True, "active": True, "viewOrder": 0},
            "bubbleChart": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
            "bubbleTree": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
            "treeMap": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
            "mapChart": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
        }
    else:
        hi_type = _hiChart_type(chart_type)
        _pp_post(request_context, base_url, {"SetEaxMd": {"tEax": {"id": eax_id}, "tArg": {
            "pattern": {"setChart": {"meta": {"hiChart": json.dumps({
                "chart": {"defaultSeriesType": hi_type},
                "plotOptions": {
                    hi_type: {"dataLabels": {"enabled": True}} if hi_type == "pie" else {"series": {}},
                },
                "template": None,
            }, ensure_ascii=False)}}},
            "meta": {},
        }}})
        mode_meta = {
            "chart": {"enabled": True, "visible": True, "active": True, "viewOrder": 0},
            "grid": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
            "speedometer": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
            "bubbleChart": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
            "bubbleTree": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
            "treeMap": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
            "mapChart": {"enabled": False, "visible": False, "active": False, "viewOrder": 1},
        }

    _pp_post(request_context, base_url, {"SetEaxMd": {"tEax": {"id": eax_id}, "tArg": {
        "pattern": {
            "grid": True, "chart": True, "bubbleChart": True,
            "bubbleTree": True, "treeMap": True, "mapChart": True, "speedometer": True,
        },
        "meta": mode_meta,
        "metaGet": {"chart": True, "grid": True, "speedometer": True},
    }}})



def _add_widget_from_chart(
    request_context: Any, base_url: str, adhoc_id: str,
    chart: dict[str, Any], cube_key: int,
) -> dict[str, Any]:
    chart_type = chart.get("chart_type", "bar")
    y_field = chart.get("y_field") or chart.get("metric") or ""
    dso_id = _random_id()

    _pp_post(request_context, base_url, {"SetAdHoc": {"tAdHocId": {"id": adhoc_id}, "tArg": {
        "meta": {"dataSourceObjects": {"its": {"it": [
            {"createNew": 2561, "id": dso_id, "slideKey": "1"}
        ]}}},
        "pattern": {"dataSourceObjects": "Add"},
        "metaGet": {"dataSourceObjects": "Get"},
    }}})

    eax_id = f"{adhoc_id}!DSO!{dso_id}"
    _set_visualizer_mode_typed(request_context, base_url, eax_id, chart_type)

    _pp_post(request_context, base_url, {"SetEaxMd": {"tEax": {"id": eax_id}, "tArg": {
        "pattern": {"dataSources": "Set"},
        "meta": {"dataSources": {
            "its": {"it": [{"k": 0, "vis": True, "cube": {
                "obDesc": {"n": "", "i": "", "k": cube_key, "c": 0}
            }}]},
            "OpenOptions": "DataAndSelection",
        }},
        "refresh": {
            "fetchData": True, "map": True, "grid": True,
            "chart": True, "speedometer": True, "saveData": False,
        },
        "metaGet": {"chart": True, "grid": True, "speedometer": True, "dataSources": "Get"},
    }}})

    _configure_chart_axes(request_context, base_url, eax_id, chart_type)
    _select_all_dims(request_context, base_url, eax_id)

    return {"dso_id": dso_id, "eax_id": eax_id, "chart_type": chart_type, "cube_key": cube_key, "y_field": y_field}


def _select_all_dims(request_context: Any, base_url: str, eax_id: str) -> None:
    """Select all elements in every dimension of an EAX widget.

    After dataSources Set with OpenOptions=DataAndSelection, Foresight resets
    each dimension to its first element only (dataRange = MultiPart 1x1).
    This call fixes that by selecting All in every dim, then refreshing.
    """
    try:
        dims_result = _pp_post(request_context, base_url, {"GetEaxMd": {"tEax": {"id": eax_id}, "tArg": {"pattern": {
            "dims": True,
            "dimArg": {
                "elsArg": {"totalCount": True, "selectionInfo": True, "filter": {"levels": 0, "elementsGroup": True}, "pattern": {"attributes": "*"}},
                "pattern": {"getDescr": True, "getIsAllSelected": True},
            },
        }}}})
        dims_raw = dims_result.get("GetEaxMdResult", {}).get("meta", {}).get("dims", {})
        dim_items = dims_raw.get("its", {}).get("it", []) if isinstance(dims_raw, dict) else []
        for d in dim_items:
            dim_key = d.get("k") or d.get("key")
            dim_id = f"{eax_id}!{dim_key}" if dim_key is not None else d.get("id")
            _pp_post(request_context, base_url, {"BatchExec": {"tArg": {"its": {"it": [
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
        _pp_post(request_context, base_url, {"SetEaxMd": {"tEax": {"id": eax_id}, "tArg": {
            "pattern": {"grid": True},
            "meta": {"grid": {"dataDisplayMode": "Interactive"}},
            "refresh": {"chart": True, "fetchData": True, "saveData": False},
            "metaGet": {"chart": True},
        }}})
    except Exception:
        pass


def _configure_chart_axes(request_context: Any, base_url: str, eax_id: str, chart_type: str) -> None:
    if chart_type in ("table", "big_number"):
        return
    try:
        result = _pp_post(request_context, base_url, {"GetEaxMd": {"tEax": {"id": eax_id}, "tArg": {
            "pattern": {"chart": True},
        }}})
        chart = result.get("GetEaxMdResult", {}).get("meta", {}).get("chart", {})
        timeline = chart.get("timeLineDimension") or {}
        objectives = chart.get("objectivesDimension") or {}
        timeline_is_facts = str(timeline.get("id", "")).startswith("FACTS") or timeline.get("n") == "Values"
        objectives_is_facts = str(objectives.get("id", "")).startswith("FACTS") or objectives.get("n") == "Values"
        chart_meta: dict[str, Any] = {
            "seriesInRows": False if chart_type in ("pie", "donut") else True,
        }
        if timeline_is_facts and not objectives_is_facts and timeline.get("k") and objectives.get("k"):
            chart_meta["timeLineDimension"] = {"k": objectives["k"]}
            chart_meta["objectivesDimension"] = {"k": timeline["k"]}
        _pp_post(request_context, base_url, {"SetEaxMd": {"tEax": {"id": eax_id}, "tArg": {
            "pattern": {"chart": True},
            "meta": {"chart": chart_meta},
            "refresh": {"fetchData": True, "chart": True, "saveData": False},
            "metaGet": {"chart": True},
        }}})
    except Exception:
        pass


def _block_type_for(chart_type: str) -> str:
    if chart_type == "big_number":
        return "Gauge"
    if chart_type == "table":
        return "Table"
    return "Chart"


def _layout_from_payload(
    request_context: Any, base_url: str, adhoc_id: str,
    widgets: list[dict[str, Any]],
    layout_items: list[dict[str, Any]],
    charts: list[dict[str, Any]],
) -> None:
    row_heights: dict[int, int] = {}
    for item in layout_items:
        r = item.get("row", 0)
        h = item.get("height", 224)
        row_heights[r] = max(row_heights.get(r, 0), h)

    total_h = sum(row_heights.values()) or 224
    row_top_pct: dict[int, float] = {}
    cumulative = 0.0
    for r in sorted(row_heights.keys()):
        row_top_pct[r] = cumulative / total_h * 100
        cumulative += row_heights[r]

    chart_by_id = {str(c.get("id")): c for c in charts}
    areas = []

    for idx, item in enumerate(layout_items):
        if idx >= len(widgets):
            break
        widget = widgets[idx]
        chart_id = str(item.get("id", ""))
        chart_spec = chart_by_id.get(chart_id, {})
        chart_type = chart_spec.get("chart_type") or widget.get("chart_type", "bar")
        title = item.get("slice_name") or chart_spec.get("slice_name") or f"Block {idx + 1}"

        col = item.get("col", 0)
        width_cols = item.get("width", 4)
        row = item.get("row", 0)
        height_px = item.get("height", 224)

        left_pct = col / _GRID_COLS * 100
        width_pct = width_cols / _GRID_COLS * 100
        top_pct = row_top_pct.get(row, 0.0)
        height_pct = height_px / total_h * 100
        right_pct = max(0.0, 100.0 - left_pct - width_pct)
        bottom_pct = max(0.0, 100.0 - top_pct - height_pct)

        areas.append({
            "@key": widget["dso_id"],
            "block": {
                "@type": _block_type_for(chart_type),
                "@key": widget["dso_id"],
                "prop": [
                    {"@tag": "name", "@val": title},
                    {"@tag": "background", "prop": [
                        {"@tag": "useBackground", "@val": "1"},
                        {"@tag": "backgroundColor", "@val": "#ffffff"},
                        {"@tag": "useGradient", "@val": "0"},
                        {"@tag": "gradientColor", "@val": "#c9c9c9"},
                        {"@tag": "gradientAngle", "@val": "270"},
                    ]},
                    {"@tag": "layout", "prop": [
                        {"@tag": "left", "@val": f"{left_pct:.2f}"},
                        {"@tag": "right", "@val": f"{right_pct:.2f}"},
                        {"@tag": "top", "@val": f"{top_pct:.2f}"},
                        {"@tag": "bottom", "@val": f"{bottom_pct:.2f}"},
                        {"@tag": "leftUnit", "@val": "%"},
                        {"@tag": "rightUnit", "@val": "%"},
                        {"@tag": "topUnit", "@val": "%"},
                        {"@tag": "bottomUnit", "@val": "%"},
                        {"@tag": "anchorLeft", "@val": "1"},
                        {"@tag": "anchorTop", "@val": "1"},
                        {"@tag": "anchorRight", "@val": "1"},
                        {"@tag": "anchorBottom", "@val": "1"},
                    ]},
                    {"@tag": "margins", "prop": {"@tag": "useMargins", "@val": "1"}},
                    {"@tag": "interactivity", "@val": "1"},
                    {"@tag": "decor", "prop": [
                        {"@tag": "cornerRadius", "@val": "5"},
                        {"@tag": "useBorderRadius", "@val": "1"},
                        {"@tag": "useBorder", "@val": "0"},
                        {"@tag": "useShadow", "@val": "0"},
                        {"@tag": "paddings", "prop": [
                            {"@tag": "usePaddings", "@val": "1"},
                            {"@tag": "left", "@val": "10"},
                            {"@tag": "right", "@val": "10"},
                            {"@tag": "top", "@val": "10"},
                            {"@tag": "bottom", "@val": "10"},
                        ]},
                    ]},
                    {"@tag": "title", "prop": [
                        {"@tag": "show", "@val": "1"},
                        {"@tag": "font", "prop": [
                            {"@tag": "color", "@val": "#48494c"},
                            {"@tag": "family", "@val": "Arial"},
                            {"@tag": "isBold", "@val": "1"},
                            {"@tag": "size", "@val": "13"},
                        ]},
                        {"@tag": "align", "@val": "Left"},
                    ]},
                ],
            },
        })

    slide_key = _random_id()
    _pp_post(request_context, base_url, {"SetAdHoc": {"tAdHocId": {"id": adhoc_id}, "tArg": {
        "meta": {"Md8": {
            "activeSlideKey": 1,
            "slides": {"its": {"it": [{
                "key": 1,
                "mainPanel": {"block": {
                    "@type": "Slide", "@key": slide_key,
                    "prop": [
                        {"@tag": "name", "@val": "Slide 1"},
                        {"@tag": "background", "prop": [
                            {"@tag": "useBackground", "@val": "1"},
                            {"@tag": "backgroundColor", "@val": "#f4f4f4"},
                            {"@tag": "useGradient", "@val": "0"},
                            {"@tag": "gradientColor", "@val": "#c9c9c9"},
                            {"@tag": "gradientAngle", "@val": "270"},
                        ]},
                        {"@tag": "margins", "prop": {"@tag": "useMargins", "@val": "0"}},
                        {"@tag": "interactivity", "@val": "1"},
                        {"@tag": "decor", "prop": {"@tag": "paddings", "prop": [
                            {"@tag": "usePaddings", "@val": "0"},
                            {"@tag": "left", "@val": "10"},
                            {"@tag": "right", "@val": "10"},
                            {"@tag": "top", "@val": "10"},
                            {"@tag": "bottom", "@val": "10"},
                        ]}},
                        {"@tag": "layouts", "area": areas},
                    ],
                }},
            }]}},
        }},
        "pattern": {"layout": {"activeSlideKey": True, "slides": "Change"}},
    }}})

    _pp_post(request_context, base_url, {"SetAdHoc": {"tAdHocId": {"id": adhoc_id}, "tArg": {
        "meta": {"Md": {"kap": {
            "@version": "10.8",
            "block": {
                "@type": "Dashboard", "@key": _random_id(),
                "prop": [
                    {"@tag": "name", "@val": "Dashboard"},
                    {"@tag": "autoLayout", "@val": "1"},
                    {"@tag": "pageLayout", "prop": {"@tag": "sizeMode", "@val": "stretch"}},
                    {"@tag": "counter", "@val": str(len(areas))},
                ],
            },
        }}},
        "pattern": {"md": True},
    }}})


def publish_dashboard(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Publish a Foresight dashboard from a real Data Agent export payload.

    Reads charts[], layout[], tables[] and creates a permanent 8448 object
    with correct widget types (bar/line/pie/table/big_number) and layout.

    Each non-table widget gets its own CSV import so that each cube has
    exactly the columns needed. We reuse the UI-created DSO from each import
    rather than creating bare API DSOs that don't render correctly.
    """
    payload = payload or {}
    charts: list[dict[str, Any]] = payload.get("charts") or []
    layout_items: list[dict[str, Any]] = payload.get("layout") or []
    title = str(payload.get("dashboard_title") or payload.get("title") or "Data Agent Dashboard")
    object_id = str(payload.get("foresight_object_id") or "DA_DASHBOARD_PUBLISHED")

    if not charts:
        raise RuntimeError("payload.charts is empty — nothing to publish")

    with tempfile.TemporaryDirectory(prefix="foresight_pub_") as tmpdir:
        tmp = Path(tmpdir)

        # Write per-chart CSVs: table charts get full CSV, others get dimension + metric only.
        full_csv_path = tmp / "full.csv"
        if payload.get("tables"):
            _csv_from_payload(full_csv_path, payload)
        elif charts and charts[0].get("rows"):
            _csv_for_chart_inline(full_csv_path, charts[0])

        chart_csvs: list[Path] = []
        for idx, chart in enumerate(charts):
            chart_type = chart.get("chart_type", "bar")
            y_field = chart.get("y_field") or chart.get("metric") or ""
            x_field = chart.get("x_field") or ""
            metric_csv = tmp / f"chart_{idx}.csv"
            if _csv_for_chart_inline(metric_csv, chart):
                pass  # used per-chart inline rows
            elif chart_type == "table" or not y_field:
                _csv_from_payload(metric_csv, payload)
            else:
                _csv_for_metric(metric_csv, payload, y_field, x_field or None)
            chart_csvs.append(metric_csv)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1600, "height": 900})
            page = context.new_page()
            state: dict[str, Any] = {
                "root_id": None, "adhoc_id": None,
                "cube_keys": [], "eax_ids": [],
            }

            def on_request(req: Any) -> None:
                post = req.post_data or ""
                if state["root_id"] is None and "!M!Root" in post:
                    m = re.search(r"([A-Z0-9]+!M!Root)", post)
                    if m:
                        state["root_id"] = m.group(1)
                if '"tAdHocId":{"id":"' in post:
                    m = re.search(r'"tAdHocId":\{"id":"([^"]+)"\}', post)
                    if m:
                        state["adhoc_id"] = m.group(1)
                cube_m = re.search(r'"cube":\{"obDesc":\{.*?"k":(\d+)', post)
                if cube_m:
                    k = int(cube_m.group(1))
                    if k not in state["cube_keys"]:
                        state["cube_keys"].append(k)
                eax_m = re.search(r'"tEax":\{"id":"([^"]+!DSO![^"]+)"\}', post)
                if eax_m:
                    eid = eax_m.group(1)
                    if eid not in state["eax_ids"]:
                        state["eax_ids"].append(eid)

            page.on("request", on_request)
            base = _cfg.foresight_base_url.rstrip("/")

            page.goto(f"{base}/app/login.html#repo={_cfg.foresight_repo_id}", wait_until="networkidle", timeout=60000)
            page.fill('input[name="username"]', _cfg.foresight_repo_login)
            page.fill('input[type="password"]', _cfg.foresight_repo_password)
            page.keyboard.press("Enter")
            page.wait_for_timeout(5000)

            try:
                page.get_by_text("Dashboards", exact=True).click(timeout=5000)
            except Exception:
                page.mouse.click(512, 420)
            for _ in range(20):
                if state["root_id"]:
                    break
                page.wait_for_timeout(500)
            if not state["root_id"]:
                raise RuntimeError("Could not capture Foresight Root session id")

            try:
                page.get_by_text("New", exact=True).last.click(timeout=3000, force=True)
            except Exception:
                page.mouse.click(515, 580)
            for _ in range(20):
                if state["adhoc_id"]:
                    break
                page.wait_for_timeout(500)
            if not state["adhoc_id"]:
                raise RuntimeError("Could not capture Foresight AdHoc id")

            # First import — creates the initial chart block + DSO via UI
            eax_ids_before_first = list(state["eax_ids"])
            cube_keys_before_first = list(state["cube_keys"])
            _run_initial_import(page, chart_csvs[0])
            first_eax_id = next(
                (e for e in state["eax_ids"] if e not in eax_ids_before_first), None
            )
            new_keys = [k for k in state["cube_keys"] if k not in cube_keys_before_first]
            first_cube_key = max(new_keys) if new_keys else (max(state["cube_keys"]) if state["cube_keys"] else None)
            if first_cube_key is None:
                raise RuntimeError("No cube key captured after first CSV import")
            first_dso_id = first_eax_id.split("!DSO!")[-1] if first_eax_id else None

            save_resp = _save_object_as(context.request, base, state["adhoc_id"], title, object_id)

            saved_key = None
            saved_id = None
            try:
                ob = (
                    save_resp.get("SaveObjectAsResult", {}).get("object")
                    or save_resp.get("tResult", {}).get("ob")
                    or {}
                )
                saved_key = ob.get("k") or ob.get("key")
                saved_id = ob.get("i") or ob.get("id")
            except Exception:
                pass
            if not saved_key:
                raise RuntimeError(f"SaveObjectAs did not return a dashboard key: {save_resp}")

            view_url = f"{base}/app/dashboard.html#key={saved_key}&mode=view&name=Dashboard&repo={_cfg.foresight_repo_id}"
            edit_url = f"{base}/app/dashboard.html#key={saved_key}&mode=edit&name=Dashboard&repo={_cfg.foresight_repo_id}"

            # Subsequent imports: each creates another chart block + DSO via UI
            per_chart_dso_ids: list[str | None] = [first_dso_id]
            per_chart_cube_keys: list[int] = [first_cube_key]
            for idx in range(1, len(charts)):
                _reopen_saved_dashboard(page, edit_url)
                eax_ids_before = list(state["eax_ids"])
                cube_keys_before = list(state["cube_keys"])
                sub_err = None
                try:
                    _run_subsequent_import(page, chart_csvs[idx])
                except Exception as e:
                    sub_err = e
                new_eax = next(
                    (e for e in state["eax_ids"] if e not in eax_ids_before), None
                )
                new_keys = [k for k in state["cube_keys"] if k not in cube_keys_before]
                ck = max(new_keys) if new_keys else first_cube_key
                if sub_err and not new_keys:
                    # Import failed and no new cube — re-raise so we can diagnose
                    raise RuntimeError(f"Subsequent import #{idx} failed: {sub_err}") from sub_err
                per_chart_dso_ids.append(new_eax.split("!DSO!")[-1] if new_eax else None)
                per_chart_cube_keys.append(ck)
                _save_current_dashboard(page)

            _reopen_saved_dashboard(page, edit_url)
            current_adhoc_id = state["adhoc_id"]
            if not current_adhoc_id:
                raise RuntimeError("Could not capture current Foresight AdHoc id after reopening the saved dashboard")

            # Build widget list: prefer UI-created DSO eax_id; fall back to API-created DSO
            widgets: list[dict[str, Any]] = []
            for idx, chart in enumerate(charts):
                cube_key = per_chart_cube_keys[idx]
                dso_id = per_chart_dso_ids[idx]
                ui_eax_id = f"{current_adhoc_id}!DSO!{dso_id}" if dso_id else None
                chart_type = chart.get("chart_type", "bar")
                y_field = chart.get("y_field") or chart.get("metric") or ""
                if ui_eax_id:
                    # Reuse the UI-created DSO; reconfigure its visualizer mode
                    _set_visualizer_mode_typed(context.request, base, ui_eax_id, chart_type)
                    _configure_chart_axes(context.request, base, ui_eax_id, chart_type)
                    _select_all_dims(context.request, base, ui_eax_id)
                    widgets.append({
                        "dso_id": dso_id,
                        "eax_id": ui_eax_id,
                        "chart_type": chart_type,
                        "cube_key": cube_key,
                        "y_field": y_field,
                    })
                else:
                    # Fallback: create a new DSO via API
                    w = _add_widget_from_chart(context.request, base, state["adhoc_id"], chart, cube_key)
                    widgets.append(w)

            _layout_from_payload(
                context.request, base, current_adhoc_id,
                widgets, layout_items, charts,
            )
            page.wait_for_timeout(2000)
            _save_current_dashboard(page)

            try:
                page.goto(view_url, wait_until="networkidle", timeout=60000)
                page.wait_for_timeout(5000)
            except Exception:
                pass

            screenshot = tmp / "published.png"
            page.screenshot(path=str(screenshot), full_page=True)
            body_text = page.locator("body").inner_text()
            screenshot_bytes = screenshot.read_bytes()
            browser.close()

            return {
                "status": "ok",
                "mode": "permanent",
                "temporary": False,
                "dashboard_title": title,
                "object_id": saved_id or object_id,
                "object_key": saved_key,
                "view_url": view_url,
                "edit_url": edit_url,
                "cube_key": first_cube_key,
                "rows": len((payload.get("tables") or [{}])[0].get("rows") or []),
                "widget_count": len(widgets),
                "widget_types": [c.get("chart_type") for c in charts],
                "save_response": save_resp,
                "screenshot_bytes": screenshot_bytes,
                "body_excerpt": body_text[:3000],
            }

def _save_object_as(
    request_context: Any,
    base_url: str,
    adhoc_id: str,
    name: str,
    object_id: str = "",
) -> dict[str, Any]:
    """Save AdHoc dashboard as a permanent 8448 object via PPService, without triggering UI Ctrl+S cleanup."""
    return _pp_post(
        request_context,
        base_url,
        {
            "SaveObjectAs": {
                "tObject": {"id": adhoc_id},
                "tArg": {
                    "destination": {
                        "operation": "CreateNew",
                        "create": {
                            "name": name,
                            "id": object_id,
                            "parent": {"i": "", "n": "", "k": 0, "c": 0, "p": 0, "h": False},
                            "permanent": True,
                        },
                        "keepMoniker": True,
                    }
                },
            }
        },
    )


def publish_permanent_dashboard(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Publish a real multi-widget Foresight dashboard saved as a permanent object.

    Flow:
      1. Login, open new dashboard (captures adhoc_id).
      2. Import CSV -> cube (captures cube_key).
      3. Add 6 DSOs (kpi x3, table, bar, line).
      4. Set layout via SetAdHoc slides:Change.
      5. Save permanently via SaveObjectAs API (no Ctrl+S = no slides-reset bug).
      6. Return view URL + screenshot.
    """
    payload = payload or {}
    rows = _mvp_rows(payload)
    title = str(payload.get("title") or payload.get("dashboard_title") or "Data Agent Dashboard")
    object_id = str(payload.get("foresight_object_id") or "DA_DASHBOARD_PERMANENT")
    widget_plan = ["kpi", "kpi", "kpi", "table", "bar", "line"]

    with tempfile.TemporaryDirectory(prefix="foresight_perm_") as tmpdir:
        tmp = Path(tmpdir)
        full_csv = tmp / "data.csv"
        _write_csv(full_csv, rows, ["revenue", "profit", "orders"])

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1600, "height": 900})
            page = context.new_page()
            state: dict[str, Any] = {"root_id": None, "adhoc_id": None, "cube_keys": []}

            def on_request(req: Any) -> None:
                post = req.post_data or ""
                if state["root_id"] is None and "!M!Root" in post:
                    m = re.search(r"([A-Z0-9]+!M!Root)", post)
                    if m:
                        state["root_id"] = m.group(1)
                if state["adhoc_id"] is None and '"tAdHocId":{"id":"' in post:
                    m = re.search(r'"tAdHocId":\{"id":"([^"]+)"\}', post)
                    if m:
                        state["adhoc_id"] = m.group(1)
                cube_m = re.search(r'"cube":\{"obDesc":\{.*?"k":(\d+)', post)
                if cube_m:
                    k = int(cube_m.group(1))
                    if k not in state["cube_keys"]:
                        state["cube_keys"].append(k)

            page.on("request", on_request)

            base = _cfg.foresight_base_url.rstrip("/")
            page.goto(f"{base}/app/login.html#repo={_cfg.foresight_repo_id}", wait_until="networkidle", timeout=60000)
            page.fill('input[name="username"]', _cfg.foresight_repo_login)
            page.fill('input[type="password"]', _cfg.foresight_repo_password)
            page.keyboard.press("Enter")
            page.wait_for_timeout(5000)

            try:
                page.get_by_text("Dashboards", exact=True).click(timeout=5000)
            except Exception:
                page.mouse.click(512, 420)
            for _ in range(20):
                if state["root_id"]:
                    break
                page.wait_for_timeout(500)
            if not state["root_id"]:
                raise RuntimeError("Could not capture Foresight Root session id")

            try:
                page.get_by_text("New", exact=True).last.click(timeout=3000, force=True)
            except Exception:
                page.mouse.click(515, 580)
            for _ in range(20):
                if state["adhoc_id"]:
                    break
                page.wait_for_timeout(500)
            if not state["adhoc_id"]:
                raise RuntimeError("Could not capture Foresight AdHoc id")

            _run_initial_import(page, full_csv)

            if not state["cube_keys"]:
                raise RuntimeError("Foresight import finished without a captured cube key")
            cube_key = max(state["cube_keys"])

            widgets = []
            for wtype in widget_plan:
                widgets.append(_add_widget(context.request, base, state["adhoc_id"], wtype, cube_key))

            _set_widget_layout(context.request, base, state["adhoc_id"], widgets)
            page.wait_for_timeout(2000)

            save_resp = _save_object_as(context.request, base, state["adhoc_id"], title, object_id)

            saved_key = None
            saved_id = None
            try:
                ob = (
                    save_resp.get("SaveObjectAsResult", {}).get("object")
                    or save_resp.get("tResult", {}).get("ob")
                    or {}
                )
                saved_key = ob.get("k") or ob.get("key")
                saved_id = ob.get("i") or ob.get("id")
            except Exception:
                pass

            screenshot = tmp / "foresight_permanent.png"
            page.wait_for_timeout(3000)
            if saved_key:
                try:
                    page.goto(
                        f"{base}/app/dashboard.html#key={saved_key}&mode=view&name=Dashboard&repo={_cfg.foresight_repo_id}",
                        wait_until="networkidle",
                        timeout=60000,
                    )
                    page.wait_for_timeout(5000)
                except Exception:
                    pass
            page.screenshot(path=str(screenshot), full_page=True)
            body_text = page.locator("body").inner_text()

            view_url = None
            edit_url = None
            if saved_key:
                view_url = f"{base}/app/dashboard.html#key={saved_key}&mode=view&name=Dashboard&repo={_cfg.foresight_repo_id}"
                edit_url = f"{base}/app/dashboard.html#key={saved_key}&mode=edit&name=Dashboard&repo={_cfg.foresight_repo_id}"

            result = {
                "status": "ok",
                "mode": "permanent",
                "temporary": False,
                "dashboard_title": title,
                "object_id": saved_id or object_id,
                "object_key": saved_key,
                "view_url": view_url,
                "edit_url": edit_url,
                "adhoc_id": state["adhoc_id"],
                "cube_key": cube_key,
                "rows": len(rows),
                "widget_types": widget_plan,
                "widgets": widgets,
                "save_response": save_resp,
                "screenshot_bytes": screenshot.read_bytes(),
                "body_excerpt": body_text[:3000],
            }
            browser.close()
            return result
