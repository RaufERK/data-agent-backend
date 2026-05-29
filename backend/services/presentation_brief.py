"""Dataset-aware presentation brief builder."""
from __future__ import annotations

import json
import logging
from typing import Any

import duckdb

from backend.services.cloudru_client import chat_complete
from backend.services.semantic_manifest import build_semantic_manifest, quote_ident

logger = logging.getLogger("data_agent.presentation_brief")


def _safe_number(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _records(conn: duckdb.DuckDBPyConnection, sql: str, limit: int = 20) -> list[dict[str, Any]]:
    try:
        df = conn.execute(sql).fetchdf().head(limit)
        return [
            {str(col): (None if row[col] is None else row[col]) for col in df.columns}
            for _, row in df.iterrows()
        ]
    except Exception as exc:
        logger.warning("Presentation SQL failed: %s | %s", exc, sql)
        return []


def _first_table(manifest: dict[str, Any]) -> dict[str, Any] | None:
    tables = sorted(manifest.get("tables", []), key=lambda table: int(table.get("row_count") or 0), reverse=True)
    return next((table for table in tables if table.get("role") == "fact"), None) or (tables[0] if tables else None)


def _profile(conn: duckdb.DuckDBPyConnection, manifest: dict[str, Any]) -> dict[str, Any]:
    table = _first_table(manifest)
    if not table:
        return {"table": None, "sections": []}

    table_name = str(table["name"])
    table_q = quote_ident(table_name)
    columns = table.get("columns", [])
    categories = [col for col in columns if col.get("role") in {"category", "geo"}][:4]
    dates = [col for col in columns if col.get("role") == "date"][:2]
    measures = [col for col in columns if col.get("role") == "measure"][:4]
    nullable = sorted(columns, key=lambda col: int(col.get("null_count") or 0), reverse=True)[:5]

    sections: list[dict[str, Any]] = [{
        "name": "dataset_overview",
        "sql": f"SELECT COUNT(*) AS rows_count FROM {table_q}",
        "data": _records(conn, f"SELECT COUNT(*) AS rows_count FROM {table_q}", 1),
    }]

    for col in categories:
        col_name = str(col["name"])
        col_q = quote_ident(col_name)
        sql = (
            f"SELECT CAST({col_q} AS VARCHAR) AS label, COUNT(*) AS rows_count "
            f"FROM {table_q} WHERE {col_q} IS NOT NULL "
            f"GROUP BY 1 ORDER BY rows_count DESC LIMIT 8"
        )
        sections.append({"name": f"top_by_{col_name}", "column": col_name, "sql": sql, "data": _records(conn, sql, 8)})

    for col in dates:
        col_name = str(col["name"])
        col_q = quote_ident(col_name)
        sql = (
            f"SELECT CAST(TRY_CAST({col_q} AS DATE) AS VARCHAR) AS period, COUNT(*) AS rows_count "
            f"FROM {table_q} WHERE TRY_CAST({col_q} AS DATE) IS NOT NULL "
            f"GROUP BY 1 ORDER BY period LIMIT 30"
        )
        sections.append({"name": f"trend_by_{col_name}", "column": col_name, "sql": sql, "data": _records(conn, sql, 30)})

    for measure in measures:
        measure_name = str(measure["name"])
        measure_q = quote_ident(measure_name)
        sql = (
            f"SELECT MIN({measure_q}) AS min_value, AVG({measure_q}) AS avg_value, "
            f"MAX({measure_q}) AS max_value, SUM({measure_q}) AS total_value "
            f"FROM {table_q} WHERE {measure_q} IS NOT NULL"
        )
        sections.append({"name": f"measure_{measure_name}", "column": measure_name, "sql": sql, "data": _records(conn, sql, 1)})

    null_rows = [
        {"column": str(col["name"]), "null_count": int(col.get("null_count") or 0)}
        for col in nullable
        if int(col.get("null_count") or 0) > 0
    ]
    if null_rows:
        sections.append({"name": "missing_values", "sql": "", "data": null_rows})

    return {
        "table": {
            "name": table_name,
            "row_count": int(table.get("row_count") or 0),
            "column_count": len(columns),
            "roles": {
                "categories": [str(col["name"]) for col in categories],
                "dates": [str(col["name"]) for col in dates],
                "measures": [str(col["name"]) for col in measures],
            },
        },
        "sections": sections,
    }


def _fallback_brief(title: str, manifest: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    table = profile.get("table") or {}
    sections = profile.get("sections") or []
    top_sections = [section for section in sections if str(section.get("name", "")).startswith("top_by_") and section.get("data")]
    measure_sections = [section for section in sections if str(section.get("name", "")).startswith("measure_") and section.get("data")]
    trend_sections = [section for section in sections if str(section.get("name", "")).startswith("trend_by_") and section.get("data")]
    missing = next((section for section in sections if section.get("name") == "missing_values"), None)

    key_findings: list[str] = []
    for section in top_sections[:3]:
        top = section["data"][0]
        key_findings.append(f"В поле «{section.get('column')}» лидирует «{top.get('label')}» с {top.get('rows_count')} строками.")
    for section in measure_sections[:2]:
        row = section["data"][0]
        key_findings.append(
            f"По показателю «{section.get('column')}»: сумма {_safe_number(row.get('total_value')):,.0f}, "
            f"среднее {_safe_number(row.get('avg_value')):,.1f}."
        )
    for section in trend_sections[:1]:
        data = section.get("data") or []
        if len(data) >= 2:
            key_findings.append(f"Динамика по «{section.get('column')}» покрывает период от {data[0].get('period')} до {data[-1].get('period')}.")

    risks = []
    if missing and missing.get("data"):
        first = missing["data"][0]
        risks.append(f"Есть пропуски: поле «{first.get('column')}» содержит {first.get('null_count')} пустых значений.")
    if not risks:
        risks.append("Критичных проблем качества в профиле презентации не обнаружено.")

    return {
        "title": title,
        "dataset_summary": (
            f"Основной источник «{table.get('name', 'dataset')}»: {table.get('row_count', 0)} строк, "
            f"{table.get('column_count', 0)} колонок. "
            f"Найдены измерения: {', '.join((table.get('roles') or {}).get('categories') or []) or 'нет'}; "
            f"меры: {', '.join((table.get('roles') or {}).get('measures') or []) or 'нет'}."
        ),
        "executive_summary": key_findings[:3] or ["Датасет загружен и готов к анализу, но сильных сигналов в базовом профиле мало."],
        "key_findings": key_findings[:6],
        "risks": risks[:4],
        "recommended_actions": [
            "Проверить лидирующие категории и подтвердить, что распределение соответствует бизнес-реальности.",
            "Разобрать числовые показатели по основным измерениям и найти причины выбросов.",
            "Зафиксировать набор KPI и периодичность обновления отчёта.",
        ],
        "sql_evidence": sections[:8],
    }


def _parse_chart_count(value: Any) -> int | None:
    if value is None:
        return None
    try:
        normalized = str(value).replace(" ", "").replace("\u00a0", "").replace(",", "")
        parsed = int(float(normalized))
        return parsed if parsed > 0 else None
    except Exception:
        return None


def _reconcile_profile_with_charts(profile: dict[str, Any], charts: list[dict[str, Any]] | None) -> dict[str, Any]:
    if not charts:
        return profile
    kpi_counts = [
        count
        for chart in charts
        if str(chart.get("type")) == "kpi"
        for count in [_parse_chart_count(chart.get("value"))]
        if count is not None
    ]
    if not kpi_counts:
        return profile
    chart_row_count = max(kpi_counts)
    table = profile.get("table")
    if isinstance(table, dict) and chart_row_count > int(table.get("row_count") or 0):
        table["row_count"] = chart_row_count
        for section in profile.get("sections") or []:
            if section.get("name") == "dataset_overview" and section.get("data"):
                section["data"][0]["rows_count"] = chart_row_count
    return profile


def build_presentation_brief(conn: duckdb.DuckDBPyConnection, title: str, charts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    manifest = build_semantic_manifest(conn)
    profile = _reconcile_profile_with_charts(_profile(conn, manifest), charts)
    fallback = _fallback_brief(title, manifest, profile)

    prompt_payload = {
        "title": title,
        "manifest": manifest,
        "profile": profile,
        "dashboard_charts": (charts or [])[:12],
    }
    prompt = (
        "Ты senior BI analyst. Составь содержательный executive presentation brief на русском. "
        "Нельзя писать общие фразы без опоры на данные. Используй только profile/sql_evidence. "
        "Если profile row_count был согласован с dashboard KPI, используй именно profile.table.row_count как размер датасета. "
        "Верни JSON строго с полями: title, dataset_summary, executive_summary[], key_findings[], risks[], recommended_actions[]. "
        "Тон: деловой, конкретный, без слов 'AI', без технического мусора.\n\n"
        + json.dumps(prompt_payload, ensure_ascii=False, default=str)[:18000]
    )
    try:
        raw = chat_complete(
            messages=[
                {"role": "system", "content": "Ты делаешь короткие управленческие презентации по данным. Возвращай только JSON."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=2200,
            temperature=0.2,
        )
        parsed = json.loads(raw.strip().strip("`").replace("json\n", "", 1))
        if isinstance(parsed, dict):
            return {**fallback, **parsed, "sql_evidence": fallback["sql_evidence"], "profile": profile}
    except Exception:
        logger.exception("LLM presentation brief failed, using fallback")

    return {**fallback, "profile": profile}
