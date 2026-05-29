"""LLM → dashboard chart specs → SQL execution pipeline."""
from __future__ import annotations
import json
import logging
import re
import duckdb
from backend.services.cloudru_client import chat_complete
from backend.services.chart_atlas import recommend_chart_specs
from backend.services.chart_intelligence import enrich_chart
from backend.services.semantic_manifest import build_semantic_manifest, format_semantic_context, quote_ident

logger = logging.getLogger("data_agent.dashboard_agent")

COLORS = ["#6ee7b7", "#818cf8", "#fb923c", "#38bdf8", "#f472b6", "#facc15"]

_SYSTEM_PROMPT = """Ты аналитик данных. Пользователь хочет построить дашборд по теме: {topic}

Схема таблиц в DuckDB:
{schema}

{filter_instruction}Сгенерируй 5-6 виджетов для дашборда. Верни JSON-массив (без markdown):
[
  {{
    "title": "Название виджета",
    "type": "kpi" | "bar" | "line" | "pie",
    "description": "Краткое описание",
    "sql": "SELECT ... FROM ... — валидный DuckDB SQL",
    "x_col": "колонка для оси X или категорий (для bar/line/pie)",
    "y_col": "колонка для значений (для bar/line/pie)",
    "value_col": "колонка для KPI-значения (для kpi)"
  }},
  ...
]

Правила:
- KPI: sql возвращает одну строку с одним числовым значением
- bar/line: sql возвращает 2 колонки: категория + число, ORDER BY значение DESC LIMIT 10
- pie: sql возвращает 2 колонки: метка + число, LIMIT 8
- Двойные кавычки для таблиц и колонок в SQL
- Не добавляй LIMIT в KPI-запросы
"""


def _build_schema_str(conn: duckdb.DuckDBPyConnection) -> str:
    try:
        return format_semantic_context(build_semantic_manifest(conn))
    except Exception:
        logger.exception("Semantic manifest build failed, falling back to raw schema")

    tables = [t for (t,) in conn.execute("SHOW TABLES").fetchall()]
    parts = []
    for table in tables:
        desc = conn.execute(f'DESCRIBE "{table}"').fetchdf()
        cols = ", ".join(f'"{r["column_name"]}" {r["column_type"]}' for _, r in desc.iterrows())
        samples = []
        for _, row in desc.iterrows():
            if any(t in row["column_type"].upper() for t in ("VARCHAR", "TEXT")):
                col = row["column_name"]
                try:
                    vals = conn.execute(
                        f'SELECT DISTINCT "{col}" FROM "{table}" WHERE "{col}" IS NOT NULL LIMIT 4'
                    ).fetchdf()[col].tolist()
                    samples.append(f'  -- "{col}": {vals}')
                except Exception:
                    pass
        sample_str = "\n" + "\n".join(samples) if samples else ""
        parts.append(f'Table "{table}":\n  {cols}{sample_str}')
    return "\n\n".join(parts)


def _strip_json(raw: str) -> str:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?", "", raw, flags=re.IGNORECASE).strip()
    raw = re.sub(r"```$", "", raw).strip()
    # If model prepended reasoning text, skip to first [ or {
    first_bracket = raw.find("[")
    first_curly = raw.find("{")
    starts = [i for i in [first_bracket, first_curly] if i >= 0]
    if starts:
        start = min(starts)
        if start > 0:
            raw = raw[start:]
    return raw


def _repair_json_array(raw: str) -> list:
    """Parse JSON array, tolerating truncation by closing open brackets."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Try to close truncated array: find last complete object
    last_close = raw.rfind("}")
    if last_close != -1:
        candidate = raw[: last_close + 1] + "]"
        # Remove trailing comma before ]
        candidate = re.sub(r",\s*]", "]", candidate)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
    return []


def _execute_chart_sql(conn: duckdb.DuckDBPyConnection, spec: dict) -> dict | None:
    sql = spec.get("sql", "").strip().rstrip(";")
    chart_type = spec.get("type", "bar")
    try:
        df = conn.execute(sql).fetchdf()
    except Exception as e:
        logger.warning("Chart SQL failed for '%s': %s | sql: %s", spec.get("title"), e, sql)
        return None

    if df.empty:
        return None

    cols = list(df.columns)

    if chart_type == "filter":
        label_col = spec.get("options_col") or spec.get("x_col") or cols[0]
        count_col = spec.get("y_col") or (cols[1] if len(cols) > 1 else cols[0])
        if label_col not in df.columns:
            label_col = cols[0]
        if count_col not in df.columns:
            count_col = cols[1] if len(cols) > 1 else cols[0]
        options = [
            {
                "label": str(row[label_col]),
                "value": str(row[label_col]),
                "count": int(float(row[count_col])) if row[count_col] is not None else 0,
            }
            for _, row in df.iterrows()
        ]
        return enrich_chart({
            "type": "filter",
            "paletteType": "filter",
            "title": spec["title"],
            "description": spec.get("description", ""),
            "recommendationReason": spec.get("recommendation_reason", ""),
            "aggregation": spec.get("aggregation", ""),
            "confidence": spec.get("confidence"),
            "metrics": [label_col],
            "filter": {
                "field": label_col,
                "source": spec.get("source_table", ""),
                "multi": True,
                "options": options,
                "selectedValues": [],
            },
            "color": COLORS[1],
        })

    if chart_type == "kpi":
        val_col = spec.get("value_col") or cols[0]
        if val_col not in df.columns:
            val_col = cols[0]
        raw_val = df.iloc[0][val_col]
        value = f"{int(raw_val):,}".replace(",", " ") if isinstance(raw_val, (int, float)) else str(raw_val)
        subtitle = spec.get("subtitle") or ""
        if not subtitle and val_col.lower() not in {"count", "value", "total"}:
            subtitle = val_col
        return enrich_chart({
            "type": "kpi",
            "paletteType": "kpi",
            "title": spec["title"],
            "description": spec.get("description", ""),
            "recommendationReason": spec.get("recommendation_reason", ""),
            "aggregation": spec.get("aggregation", ""),
            "confidence": spec.get("confidence"),
            "metrics": [val_col],
            "value": value,
            "subtitle": subtitle,
            "color": COLORS[0],
            "sparkline": [],
        })

    x_col = spec.get("x_col") or (cols[0] if len(cols) > 0 else None)
    y_col = spec.get("y_col") or (cols[1] if len(cols) > 1 else cols[0])

    if x_col not in df.columns:
        x_col = cols[0]
    if y_col not in df.columns:
        y_col = cols[1] if len(cols) > 1 else cols[0]

    color_idx = hash(spec.get("title", "")) % len(COLORS)
    color = COLORS[color_idx]

    if chart_type == "pie":
        total = df[y_col].sum()
        slices = [
            {
                "label": str(row[x_col]),
                "value": float(row[y_col]),
                "displayValue": f"{float(row[y_col]) / total * 100:.1f}%" if total else "0%",
                "color": COLORS[i % len(COLORS)],
            }
            for i, row in df.iterrows()
        ]
        return enrich_chart({
            "type": "pie",
            "paletteType": "chart",
            "title": spec["title"],
            "description": spec.get("description", ""),
            "recommendationReason": spec.get("recommendation_reason", ""),
            "aggregation": spec.get("aggregation", ""),
            "confidence": spec.get("confidence"),
            "metrics": [x_col, y_col],
            "slices": slices,
        })

    categories = [str(v) for v in df[x_col].tolist()]
    values = [float(v) if v is not None else 0.0 for v in df[y_col].tolist()]
    normalized_type = "country_map" if chart_type in {"country_map", "map", "mosaic_map"} else chart_type
    return enrich_chart({
        "type": normalized_type,
        "paletteType": "chart",
        "title": spec["title"],
        "description": spec.get("description", ""),
        "recommendationReason": spec.get("recommendation_reason", ""),
        "aggregation": spec.get("aggregation", ""),
        "confidence": spec.get("confidence"),
        "metrics": [x_col, y_col],
        "xAxisLabel": x_col,
        "yAxisLabel": y_col,
        "categories": categories,
        "series": [{"name": y_col, "values": values, "color": color}],
    })


def _pick_dashboard_table(conn: duckdb.DuckDBPyConnection) -> str | None:
    tables = [t for (t,) in conn.execute("SHOW TABLES").fetchall()]
    if not tables:
        return None
    scored: list[tuple[int, str]] = []
    for table in tables:
        try:
            count = int(conn.execute(f"SELECT COUNT(*) FROM {quote_ident(table)}").fetchone()[0])
        except Exception:
            count = 0
        scored.append((count, table))
    scored.sort(reverse=True)
    return scored[0][1]


def _is_numeric_type(raw_type: str) -> bool:
    return any(token in raw_type.upper() for token in ("INT", "DECIMAL", "DOUBLE", "FLOAT", "REAL", "NUMERIC"))


def _is_date_type(name: str, raw_type: str) -> bool:
    low = name.lower()
    return "дата" in low or "date" in low or any(token in raw_type.upper() for token in ("DATE", "TIME"))


def _build_deterministic_specs(conn: duckdb.DuckDBPyConnection, topic: str) -> list[dict]:
    try:
        manifest = build_semantic_manifest(conn)
        atlas_specs = recommend_chart_specs(manifest, topic)
        if atlas_specs:
            return atlas_specs
        manifest_tables = sorted(manifest.get("tables", []), key=lambda item: int(item.get("row_count") or 0), reverse=True)
        primary = next((t for t in manifest_tables if t.get("role") == "fact"), None) or (manifest_tables[0] if manifest_tables else None)
        if primary:
            table = str(primary["name"])
            semantic_cols = primary.get("columns", [])
            category_cols = [str(c["name"]) for c in semantic_cols if c.get("role") == "category"]
            geo_cols = [str(c["name"]) for c in semantic_cols if c.get("role") == "geo"]
            numeric_cols = [str(c["name"]) for c in semantic_cols if c.get("role") == "measure"]
            date_cols = [str(c["name"]) for c in semantic_cols if c.get("role") == "date"]
            columns = [(str(c["name"]), str(c["raw_type"])) for c in semantic_cols]
        else:
            table = None
            category_cols = []
            numeric_cols = []
            date_cols = []
            columns = []
    except Exception:
        logger.exception("Semantic dashboard spec build failed, falling back to raw table profile")
        table = None
        category_cols = []
        geo_cols = []
        numeric_cols = []
        date_cols = []
        columns = []

    table = table or _pick_dashboard_table(conn)
    if not table:
        return []

    if not columns:
        desc = conn.execute(f"DESCRIBE {quote_ident(table)}").fetchdf()
        columns = [(str(row["column_name"]), str(row["column_type"])) for _, row in desc.iterrows()]
    if not columns:
        return []

    text_cols = category_cols or [
        name for name, raw_type in columns
        if not _is_numeric_type(raw_type) and not _is_date_type(name, raw_type)
    ]
    numeric_cols = numeric_cols or [name for name, raw_type in columns if _is_numeric_type(raw_type)]
    date_cols = date_cols or [name for name, raw_type in columns if _is_date_type(name, raw_type)]
    geo_cols = geo_cols or [
        name for name, _ in columns
        if any(token in name.lower() for token in ("город", "регион", "страна", "область", "city", "region", "country"))
    ]

    category_priority = (
        "стадия", "статус", "тип", "источник", "подразделение", "организация",
        "stage", "status", "type", "source", "department", "organization",
    )
    sorted_text_cols = sorted(
        text_cols,
        key=lambda col: next((i for i, token in enumerate(category_priority) if token in col.lower()), 99),
    )
    category_col = sorted_text_cols[0] if sorted_text_cols else columns[0][0]
    second_category_col = sorted_text_cols[1] if len(sorted_text_cols) > 1 else category_col
    numeric_col = numeric_cols[0] if numeric_cols else None
    date_col = date_cols[0] if date_cols else None

    table_q = quote_ident(table)
    cat_q = quote_ident(category_col)
    second_cat_q = quote_ident(second_category_col)

    specs: list[dict] = [
        {
            "title": "Всего записей",
            "type": "kpi",
            "description": f"Количество строк в таблице {table}",
            "sql": f"SELECT COUNT(*) AS value FROM {table_q}",
            "value_col": "value",
        },
        {
            "title": f"Распределение по полю «{category_col}»",
            "type": "bar",
            "description": f"Топ значений по теме «{topic}»",
            "sql": (
                f"SELECT CAST({cat_q} AS VARCHAR) AS category, COUNT(*) AS value "
                f"FROM {table_q} WHERE {cat_q} IS NOT NULL "
                f"GROUP BY 1 ORDER BY value DESC LIMIT 10"
            ),
            "x_col": "category",
            "y_col": "value",
        },
        {
            "title": f"Доли по полю «{second_category_col}»",
            "type": "pie",
            "description": "Структура записей по категориям",
            "sql": (
                f"SELECT CAST({second_cat_q} AS VARCHAR) AS category, COUNT(*) AS value "
                f"FROM {table_q} WHERE {second_cat_q} IS NOT NULL "
                f"GROUP BY 1 ORDER BY value DESC LIMIT 8"
            ),
            "x_col": "category",
            "y_col": "value",
        },
    ]

    if numeric_col and category_col:
        num_q = quote_ident(numeric_col)
        specs.append({
            "title": f"Среднее «{numeric_col}» по «{category_col}»",
            "type": "bar",
            "description": "Среднее значение числового показателя по категориям",
            "sql": (
                f"SELECT CAST({cat_q} AS VARCHAR) AS category, AVG({num_q}) AS value "
                f"FROM {table_q} WHERE {cat_q} IS NOT NULL AND {num_q} IS NOT NULL "
                f"GROUP BY 1 ORDER BY value DESC LIMIT 10"
            ),
            "x_col": "category",
            "y_col": "value",
        })

        specs.append({
            "title": f"Диапазоны значений «{numeric_col}»",
            "type": "hbar",
            "description": "Распределение числового показателя по диапазонам",
            "sql": (
                f"WITH bounds AS ("
                f" SELECT MIN({num_q}) AS min_v, MAX({num_q}) AS max_v FROM {table_q} WHERE {num_q} IS NOT NULL"
                f"), bucketed AS ("
                f" SELECT CASE "
                f" WHEN max_v = min_v THEN CAST(min_v AS VARCHAR) "
                f" ELSE CONCAT("
                f"   CAST(ROUND(min_v + FLOOR(({num_q} - min_v) / NULLIF((max_v - min_v) / 5, 0)) * ((max_v - min_v) / 5), 2) AS VARCHAR),"
                f"   ' - ',"
                f"   CAST(ROUND(min_v + (FLOOR(({num_q} - min_v) / NULLIF((max_v - min_v) / 5, 0)) + 1) * ((max_v - min_v) / 5), 2) AS VARCHAR)"
                f" ) END AS bucket"
                f" FROM {table_q}, bounds WHERE {num_q} IS NOT NULL"
                f") SELECT bucket AS category, COUNT(*) AS value FROM bucketed GROUP BY 1 ORDER BY value DESC LIMIT 8"
            ),
            "x_col": "category",
            "y_col": "value",
        })

    if date_col:
        date_q = quote_ident(date_col)
        specs.append({
            "title": f"Динамика по полю «{date_col}»",
            "type": "line",
            "description": "Количество записей по датам",
            "sql": (
                f"SELECT CAST(TRY_CAST({date_q} AS DATE) AS VARCHAR) AS period, COUNT(*) AS value "
                f"FROM {table_q} WHERE TRY_CAST({date_q} AS DATE) IS NOT NULL "
                f"GROUP BY 1 ORDER BY period LIMIT 30"
            ),
            "x_col": "period",
            "y_col": "value",
        })

    if geo_cols:
        geo_col = geo_cols[0]
        geo_q = quote_ident(geo_col)
        specs.append({
            "title": f"Распределение по географии «{geo_col}»",
            "type": "country_map",
            "description": "Карта/гео-распределение записей",
            "sql": (
                f"SELECT CAST({geo_q} AS VARCHAR) AS category, COUNT(*) AS value "
                f"FROM {table_q} WHERE {geo_q} IS NOT NULL "
                f"GROUP BY 1 ORDER BY value DESC LIMIT 8"
            ),
            "x_col": "category",
            "y_col": "value",
        })

    if len(columns) > 0:
        sample_cols = [name for name, _ in columns[:6]]
        select_cols = ", ".join(quote_ident(col) for col in sample_cols)
        specs.append({
            "title": "Фрагмент данных",
            "type": "table",
            "description": "Первые строки основного источника",
            "sql": f"SELECT {select_cols} FROM {table_q} LIMIT 8",
        })

    return specs


def _build_deterministic_dashboard(conn: duckdb.DuckDBPyConnection, topic: str) -> list[dict]:
    charts: list[dict] = []
    for spec in _build_deterministic_specs(conn, topic):
        if spec.get("type") == "table":
            try:
                df = conn.execute(spec["sql"]).fetchdf()
            except Exception as exc:
                logger.warning("Fallback table SQL failed: %s", exc)
                continue
            charts.append(enrich_chart({
                "type": "table",
                "paletteType": "table",
                "title": spec["title"],
                "description": spec.get("description", ""),
                "recommendationReason": spec.get("recommendation_reason", ""),
                "aggregation": spec.get("aggregation", ""),
                "confidence": spec.get("confidence"),
                "metrics": list(df.columns),
                "table": {
                    "columns": list(df.columns),
                    "rows": [[str(value) if value is not None else "" for value in row] for row in df.values.tolist()],
                },
            }))
            continue

        chart = _execute_chart_sql(conn, spec)
        if chart:
            charts.append(chart)
    return charts


def _build_filter_instruction(topic: str) -> str:
    """Build an explicit SQL filter hint when topic mentions cities, categories, or exclusions."""
    topic_lower = topic.lower()

    # Detect city / region filters
    city_map = {
        "москва": "Москва", "moscow": "Москва",
        "санкт-петербург": "Санкт-Петербург", "петербург": "Санкт-Петербург",
        "спб": "Санкт-Петербург", "питер": "Санкт-Петербург", "st. petersburg": "Санкт-Петербург",
        "новосибирск": "Новосибирск", "екатеринбург": "Екатеринбург",
        "казань": "Казань", "нижний новгород": "Нижний Новгород",
    }
    include_cities = [v for k, v in city_map.items() if k in topic_lower]

    # Detect "only" / "without" intent
    only_intent = any(w in topic_lower for w in ("только", "лишь", "исключительно", "only"))
    without_intent = any(w in topic_lower for w in ("без", "кроме", "исключая", "except", "without", "not crm", "не crm"))

    lines = []
    if include_cities and only_intent:
        city_list = ", ".join(f"'{c}'" for c in include_cities)
        lines.append(
            f"ВАЖНО: пользователь хочет видеть ТОЛЬКО города {city_list}. "
            f"В КАЖДЫЙ SQL добавляй WHERE-фильтр по колонке с городом/регионом: "
            f"WHERE \"<city_column>\" IN ({city_list}). "
            f"Найди подходящую колонку по примерам значений в схеме."
        )
    elif include_cities:
        city_list = ", ".join(f"'{c}'" for c in include_cities)
        lines.append(
            f"ВАЖНО: тема касается городов {city_list}. "
            f"Фильтруй SQL: WHERE \"<city_column>\" IN ({city_list})."
        )

    if without_intent and not include_cities:
        lines.append(
            f"ВАЖНО: пользователь просит исключить часть данных по теме «{topic}». "
            f"Добавь соответствующий WHERE-фильтр в каждый SQL-запрос."
        )

    return "\n".join(lines) + "\n\n" if lines else ""


def generate_dashboard(conn: duckdb.DuckDBPyConnection, topic: str) -> list[dict]:
    # Primary path: LLM generates chart specs based on schema + topic
    schema = _build_schema_str(conn)
    filter_instruction = _build_filter_instruction(topic)
    system = _SYSTEM_PROMPT.format(schema=schema, topic=topic, filter_instruction=filter_instruction)

    try:
        raw = chat_complete(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": f"Построй дашборд по теме: {topic}"},
            ],
            max_tokens=3000,
            temperature=0,
        )
        logger.info("Dashboard LLM raw (FULL): %s", raw)

        specs = _repair_json_array(_strip_json(raw))
        if isinstance(specs, list) and len(specs) > 0:
            charts = []
            for i, spec in enumerate(specs):
                if not isinstance(spec, dict) or "sql" not in spec:
                    continue
                if "title" not in spec:
                    spec["title"] = f"Виджет {i + 1}"
                chart = _execute_chart_sql(conn, spec)
                if chart:
                    charts.append(chart)
            if charts:
                logger.info("Generated %d LLM dashboard charts for topic '%s'", len(charts), topic)
                return charts
        logger.warning("LLM returned no valid charts for topic '%s', falling back to deterministic", topic)
    except Exception as e:
        logger.warning("LLM dashboard failed for topic '%s': %s — falling back to deterministic", topic, e)

    # Fallback: deterministic rule-based chart generation
    deterministic = _build_deterministic_dashboard(conn, topic)
    if deterministic:
        logger.info("Generated %d deterministic fallback charts for topic '%s'", len(deterministic), topic)
        return deterministic

    raise ValueError(f"Не удалось построить дашборд по теме «{topic}»: LLM и детерминированный путь оба не вернули виджеты")
