"""Versioned data slices derived from immutable uploaded tables."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import duckdb
import pandas as pd

META_TABLE = "__data_versions"
VERSION_PREFIX = "__data_version_"


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def ensure_meta(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {quote_ident(META_TABLE)} (
            version_id VARCHAR,
            version_number INTEGER,
            name VARCHAR,
            source_table VARCHAR,
            table_name VARCHAR,
            instruction VARCHAR,
            sql VARCHAR,
            row_count INTEGER,
            column_count INTEGER,
            created_at VARCHAR
        )
        """
    )


def is_internal_table(table_name: str) -> bool:
    return table_name == META_TABLE or table_name.startswith(VERSION_PREFIX) or table_name.startswith("__agent_")


def user_tables(conn: duckdb.DuckDBPyConnection) -> list[str]:
    return [name for (name,) in conn.execute("SHOW TABLES").fetchall() if not is_internal_table(name)]


def list_versions(conn: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    ensure_meta(conn)
    df = conn.execute(
        f"""
        SELECT version_id, version_number, name, source_table, table_name, instruction,
               row_count, column_count, created_at
        FROM {quote_ident(META_TABLE)}
        ORDER BY version_number
        """
    ).fetchdf()
    return df.to_dict(orient="records")


def get_version(conn: duckdb.DuckDBPyConnection, version_id: str) -> dict[str, Any] | None:
    ensure_meta(conn)
    df = conn.execute(
        f"SELECT * FROM {quote_ident(META_TABLE)} WHERE version_id = ?",
        [version_id],
    ).fetchdf()
    if df.empty:
        return None
    return df.iloc[0].to_dict()


def dataframe_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    """Convert a dataframe to JSON-safe records for ORJSONResponse."""
    safe = df.astype(object).where(pd.notna(df), None)
    for column in safe.columns:
        safe[column] = safe[column].map(lambda value: value.isoformat() if hasattr(value, "isoformat") else value)
    return safe.to_dict(orient="records")


def _next_version_number(conn: duckdb.DuckDBPyConnection) -> int:
    ensure_meta(conn)
    value = conn.execute(f"SELECT COALESCE(MAX(version_number), 0) + 1 FROM {quote_ident(META_TABLE)}").fetchone()[0]
    return int(value)


def _describe_columns(conn: duckdb.DuckDBPyConnection, table_name: str) -> list[str]:
    desc = conn.execute(f"DESCRIBE {quote_ident(table_name)}").fetchdf()
    return [str(v) for v in desc["column_name"].tolist()]


def _text_columns(conn: duckdb.DuckDBPyConnection, table_name: str) -> list[str]:
    desc = conn.execute(f"DESCRIBE {quote_ident(table_name)}").fetchdf()
    columns: list[str] = []
    for _, row in desc.iterrows():
        if any(token in str(row["column_type"]).upper() for token in ("VARCHAR", "TEXT", "STRING")):
            columns.append(str(row["column_name"]))
    return columns


def _detect_city_values(instruction: str) -> list[str]:
    text = instruction.lower()
    city_map = {
        "москва": "Москва",
        "москв": "Москва",
        "moscow": "Москва",
        "питер": "Санкт-Петербург",
        "спб": "Санкт-Петербург",
        "петербург": "Санкт-Петербург",
        "санкт-петербург": "Санкт-Петербург",
        "st. petersburg": "Санкт-Петербург",
        "новосибирск": "Новосибирск",
        "екатеринбург": "Екатеринбург",
        "казань": "Казань",
        "нижний новгород": "Нижний Новгород",
    }
    values: list[str] = []
    for marker, value in city_map.items():
        if marker in text and value not in values:
            values.append(value)
    return values


def _find_filter_sql(conn: duckdb.DuckDBPyConnection, instruction: str) -> tuple[str, str] | None:
    values = _detect_city_values(instruction)
    if not values:
        return None

    for table in user_tables(conn):
        for column in _text_columns(conn, table):
          placeholders = ", ".join("?" for _ in values)
          count = conn.execute(
              f"""
              SELECT COUNT(*) FROM {quote_ident(table)}
              WHERE lower(CAST({quote_ident(column)} AS VARCHAR)) IN ({placeholders})
              """,
              [value.lower() for value in values],
          ).fetchone()[0]
          if int(count) > 0:
              literal_values = ", ".join("'" + value.replace("'", "''").lower() + "'" for value in values)
              sql = (
                  f"SELECT * FROM {quote_ident(table)} "
                  f"WHERE lower(CAST({quote_ident(column)} AS VARCHAR)) IN ({literal_values})"
              )
              return table, sql
    return None


def _detect_drop_column(instruction: str) -> str | None:
    match = re.search(r"(?:убери|удали|исключи)\s+(?:колонку|столбец|поле)?\s*[«\"']?([^»\"'\n,.;]+)", instruction, re.IGNORECASE)
    if not match:
        return None
    column = match.group(1).strip()
    stop_words = {"колонку", "столбец", "поле"}
    return None if column.lower() in stop_words else column


def _find_drop_column_sql(conn: duckdb.DuckDBPyConnection, instruction: str) -> tuple[str, str] | None:
    requested = _detect_drop_column(instruction)
    if not requested:
        return None
    requested_norm = requested.strip().lower()
    for table in user_tables(conn):
        columns = _describe_columns(conn, table)
        match = next((col for col in columns if col.lower() == requested_norm), None)
        if not match:
            match = next((col for col in columns if requested_norm in col.lower()), None)
        if not match:
            continue
        selected = [quote_ident(col) for col in columns if col != match]
        if not selected:
            raise ValueError("Нельзя создать срез без колонок")
        return table, f"SELECT {', '.join(selected)} FROM {quote_ident(table)}"
    return None


def _build_slice_sql(conn: duckdb.DuckDBPyConnection, instruction: str) -> tuple[str, str]:
    drop_sql = _find_drop_column_sql(conn, instruction)
    if drop_sql:
        return drop_sql

    filter_sql = _find_filter_sql(conn, instruction)
    if filter_sql:
        return filter_sql

    tables = user_tables(conn)
    if not tables:
        raise ValueError("Нет исходных таблиц для создания версии")
    table = tables[0]
    return table, f"SELECT * FROM {quote_ident(table)}"


def create_version(conn: duckdb.DuckDBPyConnection, instruction: str, name: str | None = None) -> dict[str, Any]:
    ensure_meta(conn)
    version_number = _next_version_number(conn)
    version_id = f"v{version_number}"
    table_name = f"{VERSION_PREFIX}{version_number}"
    source_table, sql = _build_slice_sql(conn, instruction)

    conn.execute(f"DROP TABLE IF EXISTS {quote_ident(table_name)}")
    conn.execute(f"CREATE TABLE {quote_ident(table_name)} AS {sql}")
    row_count = int(conn.execute(f"SELECT COUNT(*) FROM {quote_ident(table_name)}").fetchone()[0])
    column_count = len(_describe_columns(conn, table_name))
    created_at = datetime.now(timezone.utc).isoformat()
    display_name = name or f"Версия {version_number}: {instruction[:80]}"

    conn.execute(
        f"INSERT INTO {quote_ident(META_TABLE)} VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [version_id, version_number, display_name, source_table, table_name, instruction, sql, row_count, column_count, created_at],
    )
    return {
        "version_id": version_id,
        "version_number": version_number,
        "name": display_name,
        "source_table": source_table,
        "table_name": table_name,
        "instruction": instruction,
        "sql": sql,
        "row_count": row_count,
        "column_count": column_count,
        "created_at": created_at,
    }
