"""Chart recommendation atlas.

Turns semantic column profiles into executable chart specs plus explanation
metadata. The output intentionally uses chart types already supported by the UI.
"""
from __future__ import annotations

from typing import Any

from backend.services.semantic_manifest import quote_ident


def _first(columns: list[dict[str, Any]], role: str) -> dict[str, Any] | None:
    return next((col for col in columns if col.get("role") == role), None)


def _all(columns: list[dict[str, Any]], role: str) -> list[dict[str, Any]]:
    return [col for col in columns if col.get("role") == role]


def _spec(
    *,
    title: str,
    chart_type: str,
    description: str,
    sql: str,
    reason: str,
    aggregation: str,
    confidence: float,
    x_col: str | None = None,
    y_col: str | None = None,
    value_col: str | None = None,
    options_col: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "title": title,
        "type": chart_type,
        "description": description,
        "sql": sql,
        "recommendation_reason": reason,
        "aggregation": aggregation,
        "confidence": round(confidence, 2),
    }
    if x_col:
        payload["x_col"] = x_col
    if y_col:
        payload["y_col"] = y_col
    if value_col:
        payload["value_col"] = value_col
    if options_col:
        payload["options_col"] = options_col
    return payload


def recommend_chart_specs(manifest: dict[str, Any], topic: str) -> list[dict[str, Any]]:
    tables = sorted(manifest.get("tables", []), key=lambda table: int(table.get("row_count") or 0), reverse=True)
    table = next((item for item in tables if item.get("role") == "fact"), None) or (tables[0] if tables else None)
    if not table:
        return []

    table_name = str(table["name"])
    table_q = quote_ident(table_name)
    columns = table.get("columns", [])
    categories = _all(columns, "category")
    geos = _all(columns, "geo")
    measures = _all(columns, "measure")
    dates = _all(columns, "date")

    category = categories[0] if categories else (geos[0] if geos else None)
    second_category = categories[1] if len(categories) > 1 else category
    measure = measures[0] if measures else None
    date = dates[0] if dates else None
    geo = geos[0] if geos else None

    specs: list[dict[str, Any]] = [
        _spec(
            title="Всего записей",
            chart_type="kpi",
            description=f"Количество строк в таблице {table_name}",
            sql=f"SELECT COUNT(*) AS value FROM {table_q}",
            value_col="value",
            reason="KPI выбран как базовая сводная метрика объёма данных.",
            aggregation="count",
            confidence=0.98,
        )
    ]

    if category:
        cat_name = str(category["name"])
        cat_q = quote_ident(cat_name)
        specs.append(_spec(
            title=f"Фильтр по «{cat_name}»",
            chart_type="filter",
            description="Автофильтр по главному категориальному измерению",
            sql=(
                f"SELECT CAST({cat_q} AS VARCHAR) AS label, COUNT(*) AS count "
                f"FROM {table_q} WHERE {cat_q} IS NOT NULL "
                f"GROUP BY 1 ORDER BY count DESC LIMIT 12"
            ),
            reason="Auto filter добавлен, потому что поле похоже на главное измерение для срезов.",
            aggregation="filter values",
            confidence=0.84,
            options_col="label",
            y_col="count",
        ))
        specs.append(_spec(
            title=f"Распределение по полю «{cat_name}»",
            chart_type="bar",
            description=f"Топ значений по теме «{topic}»",
            sql=(
                f"SELECT CAST({cat_q} AS VARCHAR) AS category, COUNT(*) AS value "
                f"FROM {table_q} WHERE {cat_q} IS NOT NULL "
                f"GROUP BY 1 ORDER BY value DESC LIMIT 10"
            ),
            x_col="category",
            y_col="value",
            reason="Bar выбран для сравнения частот категориального поля.",
            aggregation="count by category",
            confidence=0.9,
        ))

    if second_category:
        second_name = str(second_category["name"])
        second_q = quote_ident(second_name)
        specs.append(_spec(
            title=f"Доли по полю «{second_name}»",
            chart_type="pie",
            description="Структура записей по категориям",
            sql=(
                f"SELECT CAST({second_q} AS VARCHAR) AS category, COUNT(*) AS value "
                f"FROM {table_q} WHERE {second_q} IS NOT NULL "
                f"GROUP BY 1 ORDER BY value DESC LIMIT 8"
            ),
            x_col="category",
            y_col="value",
            reason="Pie выбран для компактного отображения долей топ-категорий.",
            aggregation="count share",
            confidence=0.74,
        ))

    if measure and category:
        measure_name = str(measure["name"])
        cat_name = str(category["name"])
        measure_q = quote_ident(measure_name)
        cat_q = quote_ident(cat_name)
        specs.append(_spec(
            title=f"Среднее «{measure_name}» по «{cat_name}»",
            chart_type="bar",
            description="Среднее значение числового показателя по категориям",
            sql=(
                f"SELECT CAST({cat_q} AS VARCHAR) AS category, AVG({measure_q}) AS value "
                f"FROM {table_q} WHERE {cat_q} IS NOT NULL AND {measure_q} IS NOT NULL "
                f"GROUP BY 1 ORDER BY value DESC LIMIT 10"
            ),
            x_col="category",
            y_col="value",
            reason="Bar выбран для сравнения средней меры между категориями.",
            aggregation=f"avg({measure_name}) by {cat_name}",
            confidence=0.86,
        ))
        specs.append(_spec(
            title=f"Диапазоны значений «{measure_name}»",
            chart_type="hbar",
            description="Распределение числового показателя по диапазонам",
            sql=(
                f"WITH stats AS ("
                f" SELECT MIN({measure_q}) AS min_v, MAX({measure_q}) AS max_v FROM {table_q} WHERE {measure_q} IS NOT NULL"
                f"), bucketed AS ("
                f" SELECT CASE "
                f" WHEN max_v = min_v THEN CAST(min_v AS VARCHAR) "
                f" ELSE CONCAT("
                f"   CAST(ROUND(min_v + FLOOR(({measure_q} - min_v) / NULLIF((max_v - min_v) / 5, 0)) * ((max_v - min_v) / 5), 2) AS VARCHAR),"
                f"   ' - ',"
                f"   CAST(ROUND(min_v + (FLOOR(({measure_q} - min_v) / NULLIF((max_v - min_v) / 5, 0)) + 1) * ((max_v - min_v) / 5), 2) AS VARCHAR)"
                f" ) END AS bucket"
                f" FROM {table_q}, stats WHERE {measure_q} IS NOT NULL"
                f") SELECT bucket AS category, COUNT(*) AS value FROM bucketed GROUP BY 1 ORDER BY value DESC LIMIT 8"
            ),
            x_col="category",
            y_col="value",
            reason="Horizontal bar используется как histogram-like виджет для числовых диапазонов.",
            aggregation=f"histogram count({measure_name})",
            confidence=0.8,
        ))

    if date:
        date_name = str(date["name"])
        date_q = quote_ident(date_name)
        specs.append(_spec(
            title=f"Фильтр по датам «{date_name}»",
            chart_type="filter",
            description="Автофильтр по временному измерению",
            sql=(
                f"SELECT CAST(TRY_CAST({date_q} AS DATE) AS VARCHAR) AS label, COUNT(*) AS count "
                f"FROM {table_q} WHERE TRY_CAST({date_q} AS DATE) IS NOT NULL "
                f"GROUP BY 1 ORDER BY label DESC LIMIT 12"
            ),
            reason="Auto filter добавлен, потому что дата часто нужна для периода отчёта.",
            aggregation="date filter",
            confidence=0.78,
            options_col="label",
            y_col="count",
        ))
        specs.append(_spec(
            title=f"Динамика по полю «{date_name}»",
            chart_type="line",
            description="Количество записей по датам",
            sql=(
                f"SELECT CAST(TRY_CAST({date_q} AS DATE) AS VARCHAR) AS period, COUNT(*) AS value "
                f"FROM {table_q} WHERE TRY_CAST({date_q} AS DATE) IS NOT NULL "
                f"GROUP BY 1 ORDER BY period LIMIT 30"
            ),
            x_col="period",
            y_col="value",
            reason="Line выбран для временной динамики по date/datetime полю.",
            aggregation="count by date",
            confidence=0.92,
        ))

    if geo:
        geo_name = str(geo["name"])
        geo_q = quote_ident(geo_name)
        specs.append(_spec(
            title=f"Распределение по географии «{geo_name}»",
            chart_type="country_map",
            description="Карта/гео-распределение записей",
            sql=(
                f"SELECT CAST({geo_q} AS VARCHAR) AS category, COUNT(*) AS value "
                f"FROM {table_q} WHERE {geo_q} IS NOT NULL "
                f"GROUP BY 1 ORDER BY value DESC LIMIT 8"
            ),
            x_col="category",
            y_col="value",
            reason="Map-like виджет выбран, потому что поле похоже на город/регион/страну.",
            aggregation="count by geography",
            confidence=0.88,
        ))

    sample_cols = [str(col["name"]) for col in columns[:6]]
    if sample_cols:
        select_cols = ", ".join(quote_ident(col) for col in sample_cols)
        specs.append(_spec(
            title="Фрагмент данных",
            chart_type="table",
            description="Первые строки основного источника",
            sql=f"SELECT {select_cols} FROM {table_q} LIMIT 8",
            reason="Table добавлена для проверки исходных строк и детализации.",
            aggregation="sample rows",
            confidence=0.7,
        ))

    return specs[:10]
