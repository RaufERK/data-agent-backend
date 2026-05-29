"""Recommend whether a dataset needs a data model and detail layer."""
from __future__ import annotations

import json
import logging
import re
from statistics import mean
from typing import Any

import duckdb

from backend.services.cloudru_client import chat_complete
from backend.services.schema_analyzer import analyze_schema

logger = logging.getLogger("data_agent.model_advisor")

MODEL_CATALOG = {
    "no_model": {
        "label": "Без модели данных",
        "description": "Работа напрямую по исходным таблицам без detail layer и ERD.",
    },
    "star": {
        "label": "Звезда",
        "description": "Одна факт-таблица и денормализованные измерения для BI и дашбордов.",
    },
    "snowflake": {
        "label": "Снежинка",
        "description": "Нормализованные измерения и отдельные иерархические справочники.",
    },
    "datavault": {
        "label": "Data Vault",
        "description": "Хабы, линки и сателлиты для lineage, историзации и сложной интеграции.",
    },
}

PREFERRED_DIMENSION_TOKENS = (
    "stage",
    "стад",
    "status",
    "статус",
    "type",
    "тип",
    "category",
    "катег",
    "region",
    "регион",
    "department",
    "подраздел",
    "organization",
    "организац",
)

HIERARCHY_GROUPS = (
    ("region", "district"),
    ("регион", "округ"),
    ("category", "subcategory"),
    ("катег", "подкатег"),
    ("year", "quarter", "month"),
    ("год", "кварт", "месяц"),
    ("department", "block", "parent"),
    ("подраздел", "блок", "родител"),
)


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _strip_json(raw: str) -> str:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?", "", raw, flags=re.IGNORECASE).strip()
    raw = re.sub(r"```$", "", raw).strip()
    return raw


def _parse_json(raw: str) -> dict[str, Any] | None:
    cleaned = _strip_json(raw)
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            parsed = json.loads(cleaned[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _preview_df(df) -> dict[str, Any]:
    return {
        "columns": list(df.columns),
        "data": df.head(5).to_dict(orient="records"),
        "row_count": len(df),
    }


def _execute_evidence(conn: duckdb.DuckDBPyConnection, evidence_id: str, title: str, sql: str) -> dict[str, Any]:
    df = conn.execute(sql).fetchdf()
    return {
        "id": evidence_id,
        "title": title,
        "sql": sql,
        **_preview_df(df),
    }


def _build_inventory_evidence(conn: duckdb.DuckDBPyConnection, tables: list[str]) -> dict[str, Any] | None:
    if not tables:
        return None
    sql = " UNION ALL ".join(
        f"SELECT {_quote_literal(table)} AS table_name, COUNT(*) AS row_count FROM {_quote_ident(table)}"
        for table in tables
    ) + " ORDER BY row_count DESC, table_name"
    return _execute_evidence(conn, "inventory", "Размер исходных таблиц", sql)


def _build_relationship_evidences(
    conn: duckdb.DuckDBPyConnection,
    relationships: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[float]]:
    evidences: list[dict[str, Any]] = []
    match_rates: list[float] = []

    for index, rel in enumerate(relationships[:5], start=1):
        source_table = rel["from_table"]
        source_col = rel["from_col"]
        target_table = rel["to_table"]
        target_col = rel["to_col"]
        sql = f'''
SELECT
  COUNT(*) AS total_rows,
  COUNT(*) FILTER (WHERE src.{_quote_ident(source_col)} IS NOT NULL) AS rows_with_key,
  COUNT(dim.join_key) AS matched_rows,
  ROUND(
    100.0 * COUNT(dim.join_key)
    / NULLIF(COUNT(*) FILTER (WHERE src.{_quote_ident(source_col)} IS NOT NULL), 0),
    1
  ) AS match_rate_pct
FROM {_quote_ident(source_table)} AS src
LEFT JOIN (
  SELECT DISTINCT CAST({_quote_ident(target_col)} AS VARCHAR) AS join_key
  FROM {_quote_ident(target_table)}
  WHERE {_quote_ident(target_col)} IS NOT NULL
) AS dim
  ON CAST(src.{_quote_ident(source_col)} AS VARCHAR) = dim.join_key
'''.strip()
        evidence = _execute_evidence(
            conn,
            f"relationship_{index}",
            f"Покрытие связи {source_table}.{source_col} -> {target_table}.{target_col}",
            sql,
        )
        evidences.append(evidence)
        if evidence["data"]:
            value = evidence["data"][0].get("match_rate_pct")
            if isinstance(value, (int, float)):
                match_rates.append(float(value))

    return evidences, match_rates


def _pick_breakdown_columns(schema_tables: list[dict[str, Any]]) -> list[tuple[str, str]]:
    selected: list[tuple[str, str]] = []
    for table in schema_tables:
        row_count = max(int(table.get("row_count", 0)), 1)
        for column in table.get("columns", []):
            name = str(column.get("name", ""))
            low = name.lower()
            unique_count = int(column.get("unique_count", 0) or 0)
            if any(token in low for token in PREFERRED_DIMENSION_TOKENS) and 1 < unique_count <= min(20, row_count):
                selected.append((table["name"], name))
                if len(selected) >= 3:
                    return selected
    return selected


def _build_breakdown_evidences(conn: duckdb.DuckDBPyConnection, picks: list[tuple[str, str]]) -> list[dict[str, Any]]:
    evidences: list[dict[str, Any]] = []
    for index, (table_name, column_name) in enumerate(picks, start=1):
        sql = f'''
SELECT
  CAST({_quote_ident(column_name)} AS VARCHAR) AS value,
  COUNT(*) AS row_count
FROM {_quote_ident(table_name)}
GROUP BY 1
ORDER BY row_count DESC, value
LIMIT 5
'''.strip()
        evidences.append(
            _execute_evidence(
                conn,
                f"breakdown_{index}",
                f"Распределение по {table_name}.{column_name}",
                sql,
            )
        )
    return evidences


def _detect_hierarchy_signals(schema_tables: list[dict[str, Any]]) -> list[str]:
    signals: list[str] = []
    for table in schema_tables:
        names = [str(column.get("name", "")).lower() for column in table.get("columns", [])]
        for group in HIERARCHY_GROUPS:
            matches = [token for token in group if any(token in name for name in names)]
            if len(matches) >= 2:
                signals.append(f"{table['name']}: {'/'.join(matches)}")
    return signals


def _build_dataset_profile(schema: dict[str, Any], match_rates: list[float]) -> dict[str, Any]:
    tables = schema["tables"]
    transaction_tables = [table for table in tables if table.get("type") == "transaction"]
    reference_tables = [table for table in tables if table.get("type") == "reference"]
    aggregate_tables = [table for table in tables if table.get("type") == "aggregate"]
    hierarchy_signals = _detect_hierarchy_signals(tables)

    return {
        "table_count": len(tables),
        "total_rows": sum(int(table.get("row_count", 0) or 0) for table in tables),
        "transaction_table_count": len(transaction_tables),
        "reference_table_count": len(reference_tables),
        "aggregate_table_count": len(aggregate_tables),
        "relationship_count": len(schema["relationships"]),
        "average_relationship_match_rate_pct": round(mean(match_rates), 1) if match_rates else None,
        "hierarchy_signals": hierarchy_signals,
        "source_complexity": "low"
        if len(tables) <= 3
        else "medium"
        if len(tables) <= 7
        else "high",
        "schema": tables,
    }


def _normalize_option(raw: dict[str, Any], evidence_ids: list[str]) -> dict[str, Any]:
    option_id = str(raw.get("id", "")).strip().lower()
    if option_id not in MODEL_CATALOG:
        raise ValueError(f"Unknown option id: {option_id}")

    fit_score = int(raw.get("fit_score", 0) or 0)
    fit_score = max(0, min(100, fit_score))
    rationale = [str(item).strip() for item in raw.get("rationale", []) if str(item).strip()][:4]
    tradeoffs = [str(item).strip() for item in raw.get("tradeoffs", []) if str(item).strip()][:3]
    refs = [ref for ref in raw.get("sql_evidence_ids", []) if ref in evidence_ids]

    return {
        "id": option_id,
        "label": MODEL_CATALOG[option_id]["label"],
        "description": MODEL_CATALOG[option_id]["description"],
        "fit_score": fit_score,
        "fit_label": "high" if fit_score >= 75 else "medium" if fit_score >= 45 else "low",
        "summary": str(raw.get("summary", "")).strip(),
        "rationale": rationale,
        "tradeoffs": tradeoffs,
        "needs_detail_layer": bool(raw.get("needs_detail_layer")),
        "recommended": bool(raw.get("recommended")),
        "sql_evidence_ids": refs,
    }


def _heuristic_advice(profile: dict[str, Any], evidences: list[dict[str, Any]]) -> dict[str, Any]:
    table_count = profile["table_count"]
    total_rows = profile["total_rows"]
    relationship_count = profile["relationship_count"]
    hierarchy_count = len(profile["hierarchy_signals"])
    has_fact = profile["transaction_table_count"] > 0
    has_dims = profile["reference_table_count"] >= 2
    simple_dataset = table_count <= 3 and total_rows <= 100_000 and relationship_count <= 2

    if simple_dataset:
        recommended_option = "no_model"
        need_data_model = False
        need_detail_layer = False
        options = [
            {
                "id": "no_model",
                "label": MODEL_CATALOG["no_model"]["label"],
                "description": MODEL_CATALOG["no_model"]["description"],
                "fit_score": 90,
                "fit_label": "high",
                "summary": "Таблиц немного, связи читаются напрямую, а объём данных небольшой для прямого построения дашборда.",
                "rationale": [
                    "SQL-профиль показывает ограниченное число таблиц и короткую цепочку JOIN.",
                    "Для текущего объёма данных можно работать прямо по исходным таблицам без отдельного semantic layer.",
                ],
                "tradeoffs": [
                    "При росте числа источников придётся вернуться к нормальной модели.",
                ],
                "needs_detail_layer": False,
                "recommended": True,
                "sql_evidence_ids": [evidences[0]["id"]] if evidences else [],
            },
            {
                "id": "star",
                "label": MODEL_CATALOG["star"]["label"],
                "description": MODEL_CATALOG["star"]["description"],
                "fit_score": 62,
                "fit_label": "medium",
                "summary": "Подойдёт, если нужен переиспользуемый слой для BI и стандартизированные KPI.",
                "rationale": [
                    "Детерминированные связи уже есть, поэтому факт и справочники можно собрать без большого риска.",
                ],
                "tradeoffs": ["Это дополнительный слой поверх уже понятных исходных таблиц."],
                "needs_detail_layer": True,
                "recommended": False,
                "sql_evidence_ids": [e["id"] for e in evidences[:2]],
            },
            {
                "id": "snowflake",
                "label": MODEL_CATALOG["snowflake"]["label"],
                "description": MODEL_CATALOG["snowflake"]["description"],
                "fit_score": 34,
                "fit_label": "low",
                "summary": "Избыточна для небольшого числа справочников и коротких иерархий.",
                "rationale": ["Дополнительная нормализация усложнит JOIN без явной выгоды на этом датасете."],
                "tradeoffs": ["Появится больше промежуточных таблиц и сложнее отладка."],
                "needs_detail_layer": True,
                "recommended": False,
                "sql_evidence_ids": [e["id"] for e in evidences[:1]],
            },
            {
                "id": "datavault",
                "label": MODEL_CATALOG["datavault"]["label"],
                "description": MODEL_CATALOG["datavault"]["description"],
                "fit_score": 18,
                "fit_label": "low",
                "summary": "Нужен только если критичны историзация, lineage и постоянное добавление новых источников.",
                "rationale": ["Текущий профиль данных слишком простой для стоимости Data Vault."],
                "tradeoffs": ["Потребуются дополнительные витрины и более тяжёлый пайплайн."],
                "needs_detail_layer": True,
                "recommended": False,
                "sql_evidence_ids": [e["id"] for e in evidences[:1]],
            },
        ]
        executive_summary = (
            "Для этого датасета отдельная модель данных не обязательна: таблиц мало, связи короткие, "
            "а дашборд можно строить напрямую по исходным таблицам. Detail layer нужен только если вы "
            "хотите стандартизовать повторное использование метрик и витрин."
        )
    else:
        recommended_option = "snowflake" if hierarchy_count >= 2 else "star" if has_fact and has_dims else "no_model"
        need_data_model = recommended_option != "no_model"
        need_detail_layer = recommended_option in {"star", "snowflake", "datavault"}
        options = [
            {
                "id": "no_model",
                "label": MODEL_CATALOG["no_model"]["label"],
                "description": MODEL_CATALOG["no_model"]["description"],
                "fit_score": 40,
                "fit_label": "low",
                "summary": "Можно использовать только для прототипа или разовой аналитики.",
                "rationale": ["Объём или число связей уже делает прямую работу по источникам менее устойчивой."],
                "tradeoffs": ["Сложнее переиспользовать KPI и контролировать семантику."],
                "needs_detail_layer": False,
                "recommended": recommended_option == "no_model",
                "sql_evidence_ids": [evidences[0]["id"]] if evidences else [],
            },
            {
                "id": "star",
                "label": MODEL_CATALOG["star"]["label"],
                "description": MODEL_CATALOG["star"]["description"],
                "fit_score": 84 if recommended_option == "star" else 66,
                "fit_label": "high" if recommended_option == "star" else "medium",
                "summary": "Хороший базовый вариант для BI, если нужен понятный факт и измерения.",
                "rationale": ["Есть транзакционный слой и несколько справочников, пригодных для витрины."],
                "tradeoffs": ["Часть справочников останется денормализованной."],
                "needs_detail_layer": True,
                "recommended": recommended_option == "star",
                "sql_evidence_ids": [e["id"] for e in evidences[:2]],
            },
            {
                "id": "snowflake",
                "label": MODEL_CATALOG["snowflake"]["label"],
                "description": MODEL_CATALOG["snowflake"]["description"],
                "fit_score": 86 if recommended_option == "snowflake" else 58,
                "fit_label": "high" if recommended_option == "snowflake" else "medium",
                "summary": "Оправдана, если иерархические справочники реально будут жить отдельно и переиспользоваться.",
                "rationale": ["Профиль показывает признаки иерархий и нормализуемых справочников."],
                "tradeoffs": ["Больше JOIN и выше стоимость сопровождения."],
                "needs_detail_layer": True,
                "recommended": recommended_option == "snowflake",
                "sql_evidence_ids": [e["id"] for e in evidences[:3]],
            },
            {
                "id": "datavault",
                "label": MODEL_CATALOG["datavault"]["label"],
                "description": MODEL_CATALOG["datavault"]["description"],
                "fit_score": 28,
                "fit_label": "low",
                "summary": "Имеет смысл только для enterprise-интеграции с историей изменений и lineage.",
                "rationale": ["Если историзация не обязательна, Data Vault будет слишком тяжёлым решением."],
                "tradeoffs": ["Нужны отдельные пользовательские витрины поверх vault-слоя."],
                "needs_detail_layer": True,
                "recommended": recommended_option == "datavault",
                "sql_evidence_ids": [e["id"] for e in evidences[:1]],
            },
        ]
        executive_summary = (
            "По профилю датасета отдельная модель данных уже даёт пользу: появляется больше связей, справочников "
            "или иерархий, которые лучше стабилизировать отдельным detail layer."
        )

    return {
        "executive_summary": executive_summary,
        "need_data_model": need_data_model,
        "need_detail_layer": need_detail_layer,
        "recommended_option": recommended_option,
        "options": options,
    }


def _build_llm_prompt(profile: dict[str, Any], evidences: list[dict[str, Any]]) -> str:
    payload = {
        "dataset_profile": {
            key: value
            for key, value in profile.items()
            if key != "schema"
        },
        "schema": profile["schema"],
        "sql_evidence": evidences,
        "options": MODEL_CATALOG,
    }
    return f"""Ты архитектор данных и BI.

Твоя задача: по реальному профилю датасета определить,
1. нужна ли вообще модель данных
2. нужен ли detail layer
3. какой вариант лучше: no_model, star, snowflake, datavault

Критично:
- опирайся только на входной профиль и SQL evidence
- не придумывай таблицы, поля и проблемы, которых нет во входе
- если датасет маленький, связи простые и дашборд можно строить напрямую, честно рекомендуй no_model
- summary и rationale должны быть именно про этот датасет, а не общими словами

Верни только JSON без markdown в формате:
{{
  "executive_summary": "1-3 предложения по датасету",
  "need_data_model": true,
  "need_detail_layer": true,
  "recommended_option": "no_model|star|snowflake|datavault",
  "options": [
    {{
      "id": "no_model|star|snowflake|datavault",
      "fit_score": 0,
      "recommended": false,
      "summary": "краткое объяснение",
      "rationale": ["пункт 1", "пункт 2"],
      "tradeoffs": ["компромисс 1"],
      "needs_detail_layer": false,
      "sql_evidence_ids": ["inventory"]
    }}
  ]
}}

В options должны быть все 4 варианта ровно по одному разу.

Входные данные:
{json.dumps(payload, ensure_ascii=False)}
"""


def _llm_advice(profile: dict[str, Any], evidences: list[dict[str, Any]]) -> dict[str, Any] | None:
    prompt = _build_llm_prompt(profile, evidences)
    raw = chat_complete(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1800,
        temperature=0,
    )
    parsed = _parse_json(raw)
    if not parsed:
        logger.warning("Model advisor returned non-JSON response: %.300s", raw)
        return None

    evidence_ids = [evidence["id"] for evidence in evidences]
    try:
        options = [_normalize_option(option, evidence_ids) for option in parsed.get("options", [])]
    except Exception as error:
        logger.warning("Model advisor option parsing failed: %s", error)
        return None

    max_score = max((option["fit_score"] for option in options), default=0)
    if 0 < max_score <= 10:
        for option in options:
            option["fit_score"] *= 10
            option["fit_label"] = "high" if option["fit_score"] >= 75 else "medium" if option["fit_score"] >= 45 else "low"

    if {option["id"] for option in options} != set(MODEL_CATALOG):
        logger.warning("Model advisor returned incomplete options set")
        return None

    recommended_option = str(parsed.get("recommended_option", "")).strip().lower()
    if recommended_option not in MODEL_CATALOG:
        logger.warning("Model advisor returned unknown recommendation: %s", recommended_option)
        return None

    for option in options:
        option["recommended"] = option["id"] == recommended_option

    return {
        "executive_summary": str(parsed.get("executive_summary", "")).strip(),
        "need_data_model": bool(parsed.get("need_data_model")),
        "need_detail_layer": bool(parsed.get("need_detail_layer")),
        "recommended_option": recommended_option,
        "options": sorted(options, key=lambda item: item["fit_score"], reverse=True),
    }


def analyze_model_options(conn: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    schema = analyze_schema(conn)
    tables = [table["name"] for table in schema["tables"]]

    evidences = [evidence for evidence in [_build_inventory_evidence(conn, tables)] if evidence]
    relationship_evidences, match_rates = _build_relationship_evidences(conn, schema["relationships"])
    evidences.extend(relationship_evidences)
    evidences.extend(_build_breakdown_evidences(conn, _pick_breakdown_columns(schema["tables"])))

    profile = _build_dataset_profile(schema, match_rates)

    advice = None
    try:
        advice = _llm_advice(profile, evidences)
    except Exception as error:
        logger.exception("Model advisor LLM failed: %s", error)

    if advice is None:
        advice = _heuristic_advice(profile, evidences)

    return {
        "dataset_profile": {
            key: value
            for key, value in profile.items()
            if key != "schema"
        },
        "sql_evidence": evidences,
        **advice,
    }