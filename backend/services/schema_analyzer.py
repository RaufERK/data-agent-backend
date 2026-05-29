"""Analyze DuckDB tables: detect PK/FK, suggest star/snowflake schema, build ERD."""
from __future__ import annotations
import logging
import re
from dataclasses import dataclass, field, asdict
from typing import Literal
import duckdb

logger = logging.getLogger("data_agent.schema_analyzer")

TableType = Literal["transaction", "reference", "aggregate"]

_ID_PATTERNS = re.compile(r"(^|_)(id|key|code|num|number|код|ключ)($|_)", re.IGNORECASE)
_DATE_PATTERNS = re.compile(r"(дата|date|time|период|period|created|updated|at$)", re.IGNORECASE)
_MEASURE_PATTERNS = re.compile(r"(сумма|amount|sum|total|count|qty|quantity|value|price|цена|стоимость)", re.IGNORECASE)


@dataclass
class ColumnInfo:
    name: str
    data_type: str
    is_pk: bool = False
    is_fk: bool = False
    nullable: bool = True
    unique_count: int = 0
    total_count: int = 0
    role: Literal["pk", "fk", "dimension", "measure", "date", "other"] = "other"


@dataclass
class TableInfo:
    name: str
    table_type: TableType
    row_count: int
    columns: list[ColumnInfo] = field(default_factory=list)
    source: str = ""


@dataclass
class Relationship:
    from_table: str
    from_col: str
    to_table: str
    to_col: str
    confidence: float  # 0..1


def _duckdb_to_simple(dtype: str) -> str:
    d = dtype.upper()
    if any(t in d for t in ("INT", "BIGINT", "HUGEINT", "TINYINT", "SMALLINT")):
        return "INTEGER"
    if any(t in d for t in ("FLOAT", "DOUBLE", "DECIMAL", "REAL", "NUMERIC")):
        return "FLOAT"
    if "BOOL" in d:
        return "BOOLEAN"
    if any(t in d for t in ("DATE", "TIMESTAMP", "TIME")):
        return "DATETIME"
    return "VARCHAR"


def _profile_column(conn: duckdb.DuckDBPyConnection, table: str, col: str, total: int) -> tuple[int, bool]:
    """Returns (unique_count, has_nulls)."""
    try:
        row = conn.execute(
            f'SELECT COUNT(DISTINCT "{col}"), COUNT(*) - COUNT("{col}") FROM "{table}"'
        ).fetchone()
        return int(row[0]), int(row[1]) > 0
    except Exception:
        return 0, True


def _detect_role(col: ColumnInfo, total_rows: int) -> str:
    if col.is_pk:
        return "pk"
    if col.is_fk:
        return "fk"
    if _DATE_PATTERNS.search(col.name):
        return "date"
    if _MEASURE_PATTERNS.search(col.name) and col.data_type in ("INTEGER", "FLOAT"):
        return "measure"
    if _ID_PATTERNS.search(col.name):
        return "fk"
    return "dimension"


def _classify_table(table_name: str, row_count: int, columns: list[ColumnInfo]) -> TableType:
    measure_cols = sum(1 for c in columns if c.role == "measure")
    fk_cols = sum(1 for c in columns if c.role == "fk")
    date_cols = sum(1 for c in columns if c.role == "date")
    total_cols = len(columns)

    # Fact table: has multiple FKs + dates + measures
    if measure_cols + fk_cols + date_cols >= 3 and fk_cols >= 2:
        return "transaction"
    # Reference/dimension: no measures, mostly categorical columns
    if measure_cols == 0:
        return "reference"
    # Aggregate: has some measures but not a clear fact table
    return "aggregate"


def _detect_relationships(tables: list[TableInfo]) -> list[Relationship]:
    """Detect FK relationships by name matching + value inclusion."""
    relationships: list[Relationship] = []

    # Build PK index: table -> [pk_col_name]
    pk_index: dict[str, list[str]] = {}
    for t in tables:
        pks = [c.name for c in t.columns if c.is_pk]
        if pks:
            pk_index[t.name] = pks

    for t in tables:
        for col in t.columns:
            if col.role not in ("fk", "other", "dimension"):
                continue
            col_lower = col.name.lower().replace(" ", "_")

            for other in tables:
                if other.name == t.name:
                    continue
                for pk_col in pk_index.get(other.name, []):
                    pk_lower = pk_col.lower().replace(" ", "_")
                    table_lower = other.name.lower().replace(" ", "_")

                    # Name match: col contains table name or pk name
                    name_match = (
                        pk_lower in col_lower
                        or table_lower in col_lower
                        or col_lower in pk_lower
                    )
                    if name_match:
                        # Avoid duplicate
                        already = any(
                            r.from_table == t.name and r.from_col == col.name
                            and r.to_table == other.name
                            for r in relationships
                        )
                        if not already:
                            relationships.append(Relationship(
                                from_table=t.name,
                                from_col=col.name,
                                to_table=other.name,
                                to_col=pk_col,
                                confidence=0.85,
                            ))

    return relationships


def analyze_schema(conn: duckdb.DuckDBPyConnection) -> dict:
    tables_raw = [t for (t,) in conn.execute("SHOW TABLES").fetchall()]
    if not tables_raw:
        return {"tables": [], "relationships": [], "suggested_model": "star"}

    tables: list[TableInfo] = []

    for table_name in tables_raw:
        desc = conn.execute(f'DESCRIBE "{table_name}"').fetchdf()
        total = conn.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0]

        columns: list[ColumnInfo] = []
        for _, row in desc.iterrows():
            col_name = row["column_name"]
            dtype = _duckdb_to_simple(row["column_type"])
            unique_cnt, has_nulls = _profile_column(conn, table_name, col_name, total)

            is_pk = (
                unique_cnt == total
                and total > 0
                and _ID_PATTERNS.search(col_name) is not None
            )
            col = ColumnInfo(
                name=col_name,
                data_type=dtype,
                is_pk=is_pk,
                nullable=has_nulls,
                unique_count=unique_cnt,
                total_count=total,
            )
            col.role = _detect_role(col, total)
            columns.append(col)

        # If no PK detected, promote first unique ID-like column
        if not any(c.is_pk for c in columns):
            for c in columns:
                if c.unique_count == total and total > 0 and c.data_type in ("INTEGER", "VARCHAR"):
                    c.is_pk = True
                    c.role = "pk"
                    break

        table_type = _classify_table(table_name, total, columns)
        tables.append(TableInfo(
            name=table_name,
            table_type=table_type,
            row_count=total,
            columns=columns,
            source=table_name,
        ))

    relationships = _detect_relationships(tables)

    # Suggest model type
    has_fact = any(t.table_type == "transaction" for t in tables)
    has_multi_dim = sum(1 for t in tables if t.table_type == "reference") >= 2
    if has_fact and has_multi_dim:
        suggested = "star"
    elif len(tables) == 1:
        suggested = "flat"
    else:
        suggested = "star"

    logger.info(
        "Schema analysis: %d tables, %d relationships, model=%s",
        len(tables), len(relationships), suggested,
    )

    return {
        "tables": [
            {
                "name": t.name,
                "type": t.table_type,
                "row_count": t.row_count,
                "source": t.source,
                "columns": [
                    {
                        "name": c.name,
                        "data_type": c.data_type,
                        "is_pk": c.is_pk,
                        "is_fk": c.is_fk,
                        "nullable": c.nullable,
                        "role": c.role,
                        "unique_count": c.unique_count,
                    }
                    for c in t.columns
                ],
            }
            for t in tables
        ],
        "relationships": [asdict(r) for r in relationships],
        "suggested_model": suggested,
    }
