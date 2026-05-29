"""
Backend integration test suite for data_agent.
Tests all major API endpoints with real Excel files.
Reports quality metrics: latency, success rate, data correctness.
"""
from __future__ import annotations

import json
import time
import statistics
import traceback
from pathlib import Path
from typing import Any

import requests

# ── Config ────────────────────────────────────────────────────────────────────
BASE = "http://localhost:8000/api"
DATA_DIR = Path("/home/user-tot/Desktop/pannels/дата")
FILES = list(DATA_DIR.glob("*.xlsx"))

# ── Helpers ───────────────────────────────────────────────────────────────────

class Result:
    def __init__(self, name: str):
        self.name = name
        self.passed = 0
        self.failed = 0
        self.errors: list[str] = []
        self.latencies: list[float] = []
        self.details: list[dict] = []

    def ok(self, label: str, latency: float, detail: str = ""):
        self.passed += 1
        self.latencies.append(latency)
        self.details.append({"status": "✅", "label": label, "latency_ms": round(latency * 1000), "detail": detail})

    def fail(self, label: str, latency: float, reason: str):
        self.failed += 1
        self.latencies.append(latency)
        self.errors.append(f"{label}: {reason}")
        self.details.append({"status": "❌", "label": label, "latency_ms": round(latency * 1000), "detail": reason})

    @property
    def total(self): return self.passed + self.failed
    @property
    def success_rate(self): return self.passed / self.total * 100 if self.total else 0
    @property
    def avg_latency_ms(self): return round(statistics.mean(self.latencies) * 1000) if self.latencies else 0
    @property
    def p95_latency_ms(self):
        if not self.latencies: return 0
        s = sorted(self.latencies)
        return round(s[min(int(len(s) * 0.95), len(s) - 1)] * 1000)


def timed_post(url, **kwargs):
    t0 = time.perf_counter()
    r = requests.post(url, **kwargs, timeout=60)
    return r, time.perf_counter() - t0

def timed_get(url, **kwargs):
    t0 = time.perf_counter()
    r = requests.get(url, **kwargs, timeout=60)
    return r, time.perf_counter() - t0


# ── Test suites ───────────────────────────────────────────────────────────────

def test_session_lifecycle(results: list[Result]) -> str | None:
    """Create session → verify it exists → delete it."""
    r = Result("Session lifecycle")

    # Create
    resp, lat = timed_post(f"{BASE}/sessions")
    if resp.status_code != 200:
        r.fail("create session", lat, f"HTTP {resp.status_code}")
        results.append(r)
        return None
    sid = resp.json().get("session_id")
    if not sid:
        r.fail("create session", lat, "no session_id in response")
        results.append(r)
        return None
    r.ok("create session", lat, f"id={sid[:8]}…")

    # List tables (should be empty)
    resp2, lat2 = timed_get(f"{BASE}/sessions/{sid}/tables")
    if resp2.status_code == 200 and resp2.json().get("tables") == []:
        r.ok("empty tables list", lat2)
    else:
        r.fail("empty tables list", lat2, f"HTTP {resp2.status_code} body={resp2.text[:80]}")

    # Delete
    resp3, lat3 = timed_post.__wrapped__ if hasattr(timed_post, "__wrapped__") else (None, None)
    t0 = time.perf_counter()
    resp3 = requests.delete(f"{BASE}/sessions/{sid}", timeout=10)
    lat3 = time.perf_counter() - t0
    if resp3.status_code == 200:
        r.ok("delete session", lat3)
    else:
        r.fail("delete session", lat3, f"HTTP {resp3.status_code}")

    results.append(r)
    return sid  # might be deleted but we'll create fresh below


def test_file_upload(sid: str, file: Path, results: list[Result]) -> dict[str, Any] | None:
    """Upload a single file, verify row count, columns returned."""
    r = Result(f"Upload: {file.name}")

    with open(file, "rb") as f:
        resp, lat = timed_post(
            f"{BASE}/sessions/{sid}/upload",
            files={"file": (file.name, f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )

    if resp.status_code != 200:
        r.fail("upload file", lat, f"HTTP {resp.status_code}: {resp.text[:200]}")
        results.append(r)
        return None

    data = resp.json()
    row_count = data.get("row_count", 0)
    columns = data.get("columns", [])
    table_name = data.get("table_name", "")

    if row_count > 0:
        r.ok("upload file", lat, f"{row_count} строк, {len(columns)} колонок, table={table_name}")
    else:
        r.fail("upload file", lat, f"row_count=0, вероятно файл пустой или ошибка парсинга")

    results.append(r)
    return data if resp.status_code == 200 else None


def test_table_preview(sid: str, table_name: str, expected_rows: int, results: list[Result]):
    """Preview endpoint: check row_count matches upload, columns non-empty."""
    r = Result(f"Preview: {table_name}")

    resp, lat = timed_get(f"{BASE}/sessions/{sid}/tables/{table_name}/preview", params={"limit": 100})
    if resp.status_code != 200:
        r.fail("preview", lat, f"HTTP {resp.status_code}: {resp.text[:200]}")
        results.append(r)
        return

    data = resp.json()
    actual_rows = data.get("row_count", -1)
    columns = data.get("columns", [])
    preview_data = data.get("data", [])

    # row_count should match what upload returned
    if actual_rows == expected_rows:
        r.ok("row_count matches upload", lat, f"{actual_rows} строк")
    else:
        r.fail("row_count matches upload", lat, f"preview={actual_rows} vs upload={expected_rows} — MISMATCH")

    # Columns non-empty
    if len(columns) > 0:
        r.ok("columns non-empty", 0, f"{len(columns)} колонок: {', '.join(columns[:5])}{'…' if len(columns) > 5 else ''}")
    else:
        r.fail("columns non-empty", 0, "columns list is empty")

    # Preview data ≤ limit
    if len(preview_data) <= 100:
        r.ok("preview data respects limit", 0, f"{len(preview_data)} записей")
    else:
        r.fail("preview data respects limit", 0, f"{len(preview_data)} > 100")

    results.append(r)


def test_quality_check(sid: str, table_name: str, results: list[Result]):
    """Quality check: returns summary, columns, issues list."""
    r = Result(f"Quality: {table_name}")

    resp, lat = timed_get(f"{BASE}/sessions/{sid}/quality/{table_name}")
    if resp.status_code != 200:
        r.fail("quality check", lat, f"HTTP {resp.status_code}: {resp.text[:200]}")
        results.append(r)
        return

    data = resp.json()
    summary = data.get("summary", {})
    columns = data.get("columns", {})

    if summary:
        total = summary.get("total_cells", 0)
        issues = summary.get("total_issues", 0)
        r.ok("quality check returns summary", lat, f"total_cells={total}, issues={issues}")
    else:
        r.fail("quality check returns summary", lat, "summary is empty")

    # Check severity distribution
    severities = {"error": 0, "warning": 0, "info": 0}
    for col_issues in columns.values():
        for issue in col_issues:
            sev = issue.get("severity", "")
            if sev in severities:
                severities[sev] += 1

    r.ok("severity distribution", 0,
         f"errors={severities['error']}, warnings={severities['warning']}, info={severities['info']}")

    # Case mismatch should be error (not warning)
    for col_issues in columns.values():
        for issue in col_issues:
            if issue.get("type") == "case_mismatch" and issue.get("severity") != "error":
                r.fail("case_mismatch is error", 0, f"got severity={issue.get('severity')}")
                break
            if issue.get("type") == "null" and issue.get("severity") not in ("warning", "info"):
                r.fail("null is warning", 0, f"got severity={issue.get('severity')}")

    results.append(r)


def test_schema_analysis(sid: str, results: list[Result]):
    """Schema analysis: tables detected, PK/FK suggestions."""
    r = Result("Schema analysis")

    resp, lat = timed_get(f"{BASE}/sessions/{sid}/schema")
    if resp.status_code != 200:
        r.fail("schema analysis", lat, f"HTTP {resp.status_code}: {resp.text[:200]}")
        results.append(r)
        return

    data = resp.json()
    tables = data.get("tables", [])

    if len(tables) > 0:
        r.ok("tables detected", lat, f"{len(tables)} таблиц")
    else:
        r.fail("tables detected", lat, "no tables in schema result")

    for table in tables[:3]:
        name = table.get("name", "?")
        cols = table.get("columns", [])
        has_pk = any(c.get("is_pk") for c in cols)
        r.ok(f"table {name}", 0, f"{len(cols)} колонок, PK={has_pk}")

    results.append(r)


def test_semantic_manifest(sid: str, results: list[Result]):
    """Semantic manifest: returns tables with column metadata."""
    r = Result("Semantic manifest")

    resp, lat = timed_get(f"{BASE}/sessions/{sid}/semantic-manifest")
    if resp.status_code != 200:
        r.fail("semantic manifest", lat, f"HTTP {resp.status_code}: {resp.text[:200]}")
        results.append(r)
        return

    data = resp.json()
    tables = data.get("tables", [])
    if tables:
        r.ok("manifest generated", lat, f"{len(tables)} таблиц")
        for t in tables[:2]:
            r.ok(f"  {t.get('name','?')}", 0, f"{len(t.get('columns',[]))} колонок")
    else:
        r.fail("manifest generated", lat, "no tables in manifest")

    results.append(r)


def test_data_version(sid: str, table_name: str, results: list[Result]) -> str | None:
    """Create data version via instruction, then preview it."""
    r = Result(f"Data versions: {table_name}")

    instruction = f"Возьми первые 50 строк из {table_name}"
    resp, lat = timed_post(
        f"{BASE}/sessions/{sid}/versions",
        json={"instruction": instruction, "name": "Тест: первые 50"},
    )

    if resp.status_code not in (200, 201):
        r.fail("create version", lat, f"HTTP {resp.status_code}: {resp.text[:300]}")
        results.append(r)
        return None

    data = resp.json()
    version = data.get("version", {})
    vid = version.get("version_id")
    row_count = version.get("row_count", 0)

    if vid:
        r.ok("create version", lat, f"id={vid[:8]}…, rows={row_count}")
    else:
        r.fail("create version", lat, "no version_id returned")
        results.append(r)
        return None

    # Preview version
    resp2, lat2 = timed_get(f"{BASE}/sessions/{sid}/versions/{vid}/preview", params={"limit": 20})
    if resp2.status_code == 200:
        d2 = resp2.json()
        preview_rows = d2.get("row_count", 0)
        r.ok("preview version", lat2, f"row_count={preview_rows}")
        # row_count should equal version row_count
        if preview_rows == row_count:
            r.ok("version row_count consistent", 0)
        else:
            r.fail("version row_count consistent", 0, f"preview={preview_rows} vs version={row_count}")
    else:
        r.fail("preview version", lat2, f"HTTP {resp2.status_code}")

    # List versions
    resp3, lat3 = timed_get(f"{BASE}/sessions/{sid}/versions")
    if resp3.status_code == 200:
        versions = resp3.json().get("versions", [])
        if any(v.get("version_id") == vid for v in versions):
            r.ok("version in list", lat3, f"total versions: {len(versions)}")
        else:
            r.fail("version in list", lat3, "created version not found in list")
    else:
        r.fail("list versions", lat3, f"HTTP {resp3.status_code}")

    results.append(r)
    return vid


def test_memory(sid: str, results: list[Result]):
    """Add business instruction to memory, list it, delete it."""
    r = Result("Memory (business instructions)")

    # Add
    resp, lat = timed_post(
        f"{BASE}/sessions/{sid}/memory",
        json={"instruction": "Выручка считается по полю Сумма договора", "scope": "project"},
    )
    if resp.status_code != 200:
        r.fail("add instruction", lat, f"HTTP {resp.status_code}: {resp.text[:200]}")
        results.append(r)
        return

    item = resp.json().get("item", {})
    mem_id = item.get("id")
    r.ok("add instruction", lat, f"id={mem_id}")

    # List
    resp2, lat2 = timed_get(f"{BASE}/sessions/{sid}/memory")
    if resp2.status_code == 200:
        items = resp2.json().get("items", [])
        if any(i.get("id") == mem_id for i in items):
            r.ok("instruction in list", lat2, f"total: {len(items)}")
        else:
            r.fail("instruction in list", lat2, "not found after creation")
    else:
        r.fail("list memory", lat2, f"HTTP {resp2.status_code}")

    # Delete
    t0 = time.perf_counter()
    resp3 = requests.delete(f"{BASE}/sessions/{sid}/memory/{mem_id}", timeout=10)
    lat3 = time.perf_counter() - t0
    if resp3.status_code == 200:
        r.ok("delete instruction", lat3)
    else:
        r.fail("delete instruction", lat3, f"HTTP {resp3.status_code}")

    results.append(r)


def test_clean(sid: str, results: list[Result]):
    """Run server-side cleaning."""
    r = Result("Data cleaning")

    resp, lat = timed_post(f"{BASE}/sessions/{sid}/clean")
    if resp.status_code == 200:
        cleaned = resp.json().get("cleaned_tables", [])
        r.ok("clean endpoint", lat, f"cleaned tables: {cleaned}")
    else:
        r.fail("clean endpoint", lat, f"HTTP {resp.status_code}: {resp.text[:200]}")

    results.append(r)


def test_multi_file_session(results: list[Result]):
    """Upload 3 files in one session, check all tables present."""
    r = Result("Multi-file session (3 files)")

    # Create session
    resp, lat = timed_post(f"{BASE}/sessions")
    sid = resp.json().get("session_id")
    r.ok("create session", lat)

    uploaded = []
    for file in FILES[:3]:
        with open(file, "rb") as f:
            resp2, lat2 = timed_post(
                f"{BASE}/sessions/{sid}/upload",
                files={"file": (file.name, f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            )
        if resp2.status_code == 200:
            tn = resp2.json().get("table_name")
            uploaded.append(tn)
            r.ok(f"upload {file.name[:30]}", lat2, f"table={tn}, rows={resp2.json().get('row_count')}")
        else:
            r.fail(f"upload {file.name[:30]}", lat2, f"HTTP {resp2.status_code}")

    # List tables
    resp3, lat3 = timed_get(f"{BASE}/sessions/{sid}/tables")
    if resp3.status_code == 200:
        tables = resp3.json().get("tables", [])
        if set(uploaded).issubset(set(tables)):
            r.ok("all tables listed", lat3, f"{tables}")
        else:
            r.fail("all tables listed", lat3, f"expected {uploaded}, got {tables}")
    else:
        r.fail("list tables", lat3, f"HTTP {resp3.status_code}")

    # Schema across all tables
    resp4, lat4 = timed_get(f"{BASE}/sessions/{sid}/schema")
    if resp4.status_code == 200:
        n = len(resp4.json().get("tables", []))
        r.ok("schema multi-table", lat4, f"{n} tables in schema")
    else:
        r.fail("schema multi-table", lat4, f"HTTP {resp4.status_code}")

    results.append(r)


def test_error_handling(results: list[Result]):
    """Check that bad requests return proper 4xx errors."""
    r = Result("Error handling / edge cases")

    # Non-existent session
    resp, lat = timed_get(f"{BASE}/sessions/nonexistent-id-000/tables")
    if resp.status_code == 404:
        r.ok("404 on bad session", lat)
    else:
        r.fail("404 on bad session", lat, f"got {resp.status_code}")

    # Quality on nonexistent table
    sid = requests.post(f"{BASE}/sessions", timeout=10).json()["session_id"]
    resp2, lat2 = timed_get(f"{BASE}/sessions/{sid}/quality/no_such_table")
    if resp2.status_code == 404:
        r.ok("404 quality nonexistent table", lat2)
    else:
        r.fail("404 quality nonexistent table", lat2, f"got {resp2.status_code}")

    # Version with empty instruction
    resp3, lat3 = timed_post(f"{BASE}/sessions/{sid}/versions", json={"instruction": ""})
    if resp3.status_code == 400:
        r.ok("400 on empty instruction", lat3)
    else:
        r.fail("400 on empty instruction", lat3, f"got {resp3.status_code}")

    # Semantic manifest without files
    resp4, lat4 = timed_get(f"{BASE}/sessions/{sid}/semantic-manifest")
    if resp4.status_code == 400:
        r.ok("400 semantic manifest without files", lat4)
    else:
        r.fail("400 semantic manifest without files", lat4, f"got {resp4.status_code}")

    results.append(r)


# ── Report ────────────────────────────────────────────────────────────────────

def print_report(all_results: list[Result], total_time: float):
    print("\n" + "═" * 70)
    print("  DATA AGENT — BACKEND TEST REPORT")
    print("═" * 70)

    total_passed = sum(r.passed for r in all_results)
    total_failed = sum(r.failed for r in all_results)
    total_tests = total_passed + total_failed
    overall_rate = total_passed / total_tests * 100 if total_tests else 0

    for r in all_results:
        status = "✅" if r.failed == 0 else "⚠️ " if r.success_rate >= 50 else "❌"
        print(f"\n{status} {r.name}")
        print(f"   Результат: {r.passed}/{r.total} прошло ({r.success_rate:.0f}%)")
        if r.latencies:
            print(f"   Латентность: avg={r.avg_latency_ms}ms, p95={r.p95_latency_ms}ms")
        for d in r.details:
            prefix = d["status"]
            ms = f"[{d['latency_ms']}ms]" if d["latency_ms"] > 0 else ""
            detail = f" — {d['detail']}" if d["detail"] else ""
            print(f"     {prefix} {d['label']} {ms}{detail}")

    print("\n" + "─" * 70)
    print("  СВОДКА")
    print("─" * 70)
    print(f"  Всего тестов:      {total_tests}")
    print(f"  Прошло:            {total_passed} ✅")
    print(f"  Упало:             {total_failed} ❌")
    print(f"  Success rate:      {overall_rate:.1f}%")
    print(f"  Общее время:       {total_time:.1f}s")

    # Latency summary
    all_latencies = [l for r in all_results for l in r.latencies]
    if all_latencies:
        print(f"  Avg latency:       {round(statistics.mean(all_latencies)*1000)}ms")
        print(f"  P95 latency:       {round(sorted(all_latencies)[int(len(all_latencies)*0.95)-1]*1000)}ms")
        print(f"  Max latency:       {round(max(all_latencies)*1000)}ms")

    print("\n  ОЦЕНКА ПО МЕТРИКАМ:")
    metrics = {
        "Доступность API":          "✅ Отлично" if overall_rate >= 95 else "⚠️  Есть проблемы" if overall_rate >= 70 else "❌ Критично",
        "Корректность row_count":   "↑ см. тест Preview",
        "Обработка ошибок (4xx)":   "↑ см. Error handling",
        "Скорость загрузки файлов": "✅ Норма" if all_latencies and statistics.mean(all_latencies) < 5 else "⚠️  Медленно",
        "Стабильность":             "✅ Стабильно" if total_failed == 0 else f"⚠️  {total_failed} сбоев",
    }
    for k, v in metrics.items():
        print(f"    {k:35s}: {v}")

    if total_failed > 0:
        print("\n  ОШИБКИ:")
        for r in all_results:
            for e in r.errors:
                print(f"    ❌ [{r.name}] {e}")

    print("═" * 70 + "\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"🚀 Запуск тестов data_agent backend")
    print(f"   BASE URL: {BASE}")
    print(f"   Файлов для теста: {len(FILES)}")
    for f in FILES:
        print(f"     · {f.name}")
    print()

    all_results: list[Result] = []
    t_start = time.perf_counter()

    # 1. Error handling (no files needed)
    print("→ [1/7] Error handling...")
    try:
        test_error_handling(all_results)
    except Exception:
        traceback.print_exc()

    # 2. Session lifecycle
    print("→ [2/7] Session lifecycle...")
    try:
        test_session_lifecycle(all_results)
    except Exception:
        traceback.print_exc()

    # 3-6. Per-file tests (first 3 files)
    print("→ [3/7] Per-file upload + quality + versions...")
    for i, file in enumerate(FILES[:3]):
        print(f"     Файл {i+1}/3: {file.name}")
        try:
            # Fresh session per file
            sid = requests.post(f"{BASE}/sessions", timeout=10).json()["session_id"]

            upload_data = test_file_upload(sid, file, all_results)
            if upload_data:
                table_name = upload_data["table_name"]
                row_count = upload_data["row_count"]

                test_table_preview(sid, table_name, row_count, all_results)
                test_quality_check(sid, table_name, all_results)
                test_data_version(sid, table_name, all_results)
                test_memory(sid, all_results)
                test_clean(sid, all_results)
        except Exception:
            traceback.print_exc()

    # 7. Schema + semantic on single file
    print("→ [4/7] Schema analysis...")
    try:
        if FILES:
            sid2 = requests.post(f"{BASE}/sessions", timeout=10).json()["session_id"]
            with open(FILES[0], "rb") as f:
                requests.post(
                    f"{BASE}/sessions/{sid2}/upload",
                    files={"file": (FILES[0].name, f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                    timeout=30,
                )
            test_schema_analysis(sid2, all_results)
            test_semantic_manifest(sid2, all_results)
    except Exception:
        traceback.print_exc()

    # 8. Multi-file session
    print("→ [5/7] Multi-file session...")
    try:
        test_multi_file_session(all_results)
    except Exception:
        traceback.print_exc()

    # 9. All files upload stress
    print("→ [6/7] All files upload stress test...")
    try:
        r = Result(f"Stress: все {len(FILES)} файлов")
        sid3 = requests.post(f"{BASE}/sessions", timeout=10).json()["session_id"]
        for file in FILES:
            try:
                with open(file, "rb") as f:
                    resp, lat = timed_post(
                        f"{BASE}/sessions/{sid3}/upload",
                        files={"file": (file.name, f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                    )
                if resp.status_code == 200:
                    d = resp.json()
                    r.ok(file.name[:40], lat, f"rows={d.get('row_count')}, cols={len(d.get('columns',[]))}")
                else:
                    r.fail(file.name[:40], lat, f"HTTP {resp.status_code}: {resp.text[:100]}")
            except Exception as e:
                r.fail(file.name[:40], 0, str(e))
        # Final tables list
        resp_t, lat_t = timed_get(f"{BASE}/sessions/{sid3}/tables")
        if resp_t.status_code == 200:
            tables = resp_t.json().get("tables", [])
            r.ok("final tables list", lat_t, f"{len(tables)} таблиц в сессии")
        all_results.append(r)
    except Exception:
        traceback.print_exc()

    # 10. Quality on ALL files in one session
    print("→ [7/7] Quality checks on all files...")
    try:
        r2 = Result(f"Quality: все {len(FILES)} файлов")
        sid4 = requests.post(f"{BASE}/sessions", timeout=10).json()["session_id"]
        table_names = []
        for file in FILES:
            with open(file, "rb") as f:
                resp = requests.post(
                    f"{BASE}/sessions/{sid4}/upload",
                    files={"file": (file.name, f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                    timeout=30,
                )
            if resp.status_code == 200:
                table_names.append(resp.json()["table_name"])

        total_issues = 0
        total_cells = 0
        for tn in table_names:
            resp_q, lat_q = timed_get(f"{BASE}/sessions/{sid4}/quality/{tn}")
            if resp_q.status_code == 200:
                summary = resp_q.json().get("summary", {})
                issues = summary.get("total_issues", 0)
                cells = summary.get("total_cells", 0)
                total_issues += issues
                total_cells += cells
                pct = round(issues / cells * 100, 2) if cells else 0
                r2.ok(f"quality {tn[:35]}", lat_q, f"issues={issues}/{cells} ({pct}%)")
            else:
                r2.fail(f"quality {tn[:35]}", lat_q, f"HTTP {resp_q.status_code}")

        if total_cells > 0:
            overall_pct = round(total_issues / total_cells * 100, 2)
            print(f"\n   📊 Общее качество данных: {total_issues} проблем из {total_cells} ячеек ({overall_pct}%)")
        all_results.append(r2)
    except Exception:
        traceback.print_exc()

    total_time = time.perf_counter() - t_start
    print_report(all_results, total_time)


if __name__ == "__main__":
    main()
