"""Lightweight semantic manifest for uploaded DuckDB sessions.

This is our local context layer: it profiles tables, assigns practical roles
to columns, and produces compact context for SQL/dashboard agents.
"""
from __future__ import annotations

import re
from typing import Any

import duckdb
from backend.services.data_versions import is_internal_table
from backend.services.project_memory import list_instructions


_ID_RE = re.compile(r"(^|_|\s)(id|key|code|num|number|код|ключ|номер)($|_|\s)", re.IGNORECASE)
_DATE_RE = re.compile(r"(дата|date|time|timestamp|created|updated|период|period)", re.IGNORECASE)
_MEASURE_RE = re.compile(
    r"(сумма|amount|sum|total|qty|quantity|count|value|price|цена|стоимость|срок|дней|days)",
    re.IGNORECASE,
)
_CATEGORY_HINTS = (
    "статус", "стадия", "тип", "источник", "канал", "подразделение", "организация",
    "владелец", "город", "регион", "status", "stage", "type", "source", "department",
    "organization", "owner", "city", "region",
)
_GEO_RE = re.compile(r"(город|регион|страна|область|country|city|region|geo|location)", re.IGNORECASE)


def quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _simple_type(raw_type: str) -> str:
    raw = raw_type.upper()
    if any(token in raw for token in ("INT", "BIGINT", "HUGEINT", "SMALLINT", "TINYINT")):
        return "integer"
    if any(token in raw for token in ("DECIMAL", "DOUBLE", "FLOAT", "REAL", "NUMERIC")):
        return "float"
    if "BOOL" in raw:
        return "boolean"
    if any(token in raw for token in ("DATE", "TIME", "TIMESTAMP")):
        return "datetime"
    return "string"


def _sample_values(conn: duckdb.DuckDBPyConnection, table: str, column: str, simple_type: str) -> list[str]:
    if simple_type not in {"string", "boolean"}:
        return []
    try:
        df = conn.execute(
            f"""
            SELECT DISTINCT CAST({quote_ident(column)} AS VARCHAR) AS value
            FROM {quote_ident(table)}
            WHERE {quote_ident(column)} IS NOT NULL
              AND CAST({quote_ident(column)} AS VARCHAR) <> ''
            LIMIT 5
            """
        ).fetchdf()
        return [str(v) for v in df["value"].tolist()]
    except Exception:
        return []


def _column_role(name: str, simple_type: str, unique_count: int, row_count: int) -> str:
    low = name.lower()
    uniqueness = unique_count / max(row_count, 1)
    if _ID_RE.search(name) and uniqueness >= 0.95:
        return "primary_key"
    if _ID_RE.search(name):
        return "key"
    if simple_type == "datetime" or _DATE_RE.search(name):
        return "date"
    if _GEO_RE.search(name):
        return "geo"
    if simple_type in {"integer", "float"} and (_MEASURE_RE.search(name) or uniqueness < 0.7):
        return "measure"
    if any(hint in low for hint in _CATEGORY_HINTS):
        return "category"
    if simple_type in {"string", "boolean"}:
        return "category" if unique_count <= max(30, row_count * 0.35) else "text"
    return "other"


def _profile_column(conn: duckdb.DuckDBPyConnection, table: str, column: str, row_count: int) -> dict[str, Any]:
    desc = conn.execute(f"DESCRIBE {quote_ident(table)}").fetchdf()
    raw_type = str(desc.loc[desc["column_name"] == column, "column_type"].iloc[0])
    simple = _simple_type(raw_type)
    try:
        unique_count, null_count = conn.execute(
            f"SELECT COUNT(DISTINCT {quote_ident(column)}), COUNT(*) - COUNT({quote_ident(column)}) "
            f"FROM {quote_ident(table)}"
        ).fetchone()
    except Exception:
        unique_count, null_count = 0, row_count

    unique_count = int(unique_count or 0)
    null_count = int(null_count or 0)
    role = _column_role(column, simple, unique_count, row_count)
    return {
        "name": column,
        "raw_type": raw_type,
        "type": simple,
        "role": role,
        "unique_count": unique_count,
        "null_count": null_count,
        "sample_values": _sample_values(conn, table, column, simple),
    }


def _table_role(columns: list[dict[str, Any]], row_count: int) -> str:
    measures = sum(1 for c in columns if c["role"] == "measure")
    dates = sum(1 for c in columns if c["role"] == "date")
    keys = sum(1 for c in columns if c["role"] in {"key", "primary_key"})
    if row_count > 0 and (measures + dates + keys >= 3 or dates >= 1 and keys >= 1):
        return "fact"
    if measures == 0:
        return "dimension"
    return "aggregate"


def _detect_relationships(tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    primary_keys: list[tuple[str, str]] = []
    for table in tables:
        for col in table["columns"]:
            if col["role"] == "primary_key":
                primary_keys.append((table["name"], col["name"]))

    relationships: list[dict[str, Any]] = []
    for table in tables:
        for col in table["columns"]:
            if col["role"] not in {"key", "category"}:
                continue
            col_key = col["name"].lower().replace(" ", "_")
            for pk_table, pk_col in primary_keys:
                if pk_table == table["name"]:
                    continue
                pk_key = pk_col.lower().replace(" ", "_")
                table_key = pk_table.lower().replace(" ", "_")
                if pk_key in col_key or table_key in col_key or col_key in pk_key:
                    relationships.append({
                        "from_table": table["name"],
                        "from_col": col["name"],
                        "to_table": pk_table,
                        "to_col": pk_col,
                        "confidence": 0.8,
                    })
    return relationships


def _recommended_questions(tables: list[dict[str, Any]]) -> list[str]:
    questions: list[str] = []
    for table in tables[:3]:
        categories = [c["name"] for c in table["columns"] if c["role"] == "category"]
        geo_cols = [c["name"] for c in table["columns"] if c["role"] == "geo"]
        dates = [c["name"] for c in table["columns"] if c["role"] == "date"]
        measures = [c["name"] for c in table["columns"] if c["role"] == "measure"]
        nullable = [c["name"] for c in table["columns"] if int(c.get("null_count") or 0) > 0]
        keys = [c["name"] for c in table["columns"] if c["role"] in {"key", "primary_key"}]
        if categories:
            questions.append(f"Покажи распределение записей по полю «{categories[0]}»")
            questions.append(f"Покажи топ-10 значений поля «{categories[0]}»")
        if dates:
            questions.append(f"Покажи динамику количества записей по полю «{dates[0]}»")
        if categories and measures:
            questions.append(f"Где максимальное среднее значение «{measures[0]}» по «{categories[0]}»?")
        if measures:
            questions.append(f"Покажи распределение значений «{measures[0]}» по диапазонам")
        if geo_cols:
            questions.append(f"Покажи карту/распределение по полю «{geo_cols[0]}»")
        if nullable:
            questions.append(f"Где больше всего пустых значений в поле «{nullable[0]}»?")
        if keys:
            questions.append(f"Есть ли дубликаты по ключу «{keys[0]}»?")
    deduped = list(dict.fromkeys(questions))
    return deduped[:8]


def build_semantic_manifest(conn: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    tables_raw = [t for (t,) in conn.execute("SHOW TABLES").fetchall() if not is_internal_table(str(t))]
    tables: list[dict[str, Any]] = []
    for table in tables_raw:
        _count_row = conn.execute(f"SELECT COUNT(*) FROM {quote_ident(table)}").fetchone()
        row_count = int(_count_row[0]) if _count_row is not None else 0
        desc = conn.execute(f"DESCRIBE {quote_ident(table)}").fetchdf()
        columns = [
            _profile_column(conn, table, str(row["column_name"]), row_count)
            for _, row in desc.iterrows()
        ]
        tables.append({
            "name": table,
            "row_count": row_count,
            "role": _table_role(columns, row_count),
            "columns": columns,
        })

    return {
        "tables": tables,
        "relationships": _detect_relationships(tables),
        "recommended_questions": _recommended_questions(tables),
        "instructions": list_instructions(conn),
    }


def format_semantic_context(manifest: dict[str, Any]) -> str:
    lines: list[str] = []
    for table in manifest.get("tables", []):
        lines.append(f'Table "{table["name"]}" ({table["role"]}, {table["row_count"]} rows):')
        for col in table.get("columns", []):
            sample = f" samples={col['sample_values']}" if col.get("sample_values") else ""
            lines.append(f'  - "{col["name"]}" {col["raw_type"]} role={col["role"]}{sample}')
    rels = manifest.get("relationships", [])
    if rels:
        lines.append("Relationships:")
        for rel in rels:
            lines.append(
                f'  - "{rel["from_table"]}"."{rel["from_col"]}" -> '
                f'"{rel["to_table"]}"."{rel["to_col"]}" confidence={rel["confidence"]}'
            )
    questions = manifest.get("recommended_questions", [])
    if questions:
        lines.append("Recommended questions:")
        lines.extend(f"  - {q}" for q in questions)
    instructions = manifest.get("instructions", [])
    if instructions:
        lines.append("Business instructions:")
        for item in instructions:
            lines.append(f"  - {item.get('instruction')}")
    return "\n".join(lines)
