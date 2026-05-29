"""Session-scoped agent memory for business rules and user instructions."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import duckdb


MEMORY_TABLE = "__agent_memory"


def quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def ensure_memory(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {quote_ident(MEMORY_TABLE)} (
            memory_id VARCHAR,
            instruction VARCHAR,
            scope VARCHAR,
            created_at VARCHAR
        )
        """
    )


def list_instructions(conn: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    ensure_memory(conn)
    df = conn.execute(
        f"""
        SELECT memory_id, instruction, scope, created_at
        FROM {quote_ident(MEMORY_TABLE)}
        ORDER BY created_at
        """
    ).fetchdf()
    return df.to_dict(orient="records")


def add_instruction(conn: duckdb.DuckDBPyConnection, instruction: str, scope: str = "project") -> dict[str, Any]:
    ensure_memory(conn)
    text = instruction.strip()
    if not text:
        raise ValueError("instruction is required")
    existing = conn.execute(
        f"SELECT memory_id, instruction, scope, created_at FROM {quote_ident(MEMORY_TABLE)} "
        "WHERE lower(instruction) = lower(?) LIMIT 1",
        [text],
    ).fetchdf()
    if not existing.empty:
        return existing.iloc[0].to_dict()

    count = int(conn.execute(f"SELECT COUNT(*) FROM {quote_ident(MEMORY_TABLE)}").fetchone()[0])
    item = {
        "memory_id": f"m{count + 1}",
        "instruction": text,
        "scope": scope.strip() or "project",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    conn.execute(
        f"INSERT INTO {quote_ident(MEMORY_TABLE)} VALUES (?, ?, ?, ?)",
        [item["memory_id"], item["instruction"], item["scope"], item["created_at"]],
    )
    return item


def delete_instruction(conn: duckdb.DuckDBPyConnection, memory_id: str) -> bool:
    ensure_memory(conn)
    before = int(conn.execute(f"SELECT COUNT(*) FROM {quote_ident(MEMORY_TABLE)}").fetchone()[0])
    conn.execute(f"DELETE FROM {quote_ident(MEMORY_TABLE)} WHERE memory_id = ?", [memory_id])
    after = int(conn.execute(f"SELECT COUNT(*) FROM {quote_ident(MEMORY_TABLE)}").fetchone()[0])
    return after < before
