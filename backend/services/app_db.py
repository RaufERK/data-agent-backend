"""PostgreSQL metadata store for users, sessions, uploads, and chat history."""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from backend.config import get_settings


def _conninfo() -> str:
    settings = get_settings()
    return _conninfo_for_db(settings.app_db_name)


def _conninfo_for_db(db_name: str) -> str:
    settings = get_settings()
    return (
        f"host={settings.app_db_host} "
        f"port={settings.app_db_port} "
        f"dbname={db_name} "
        f"user={settings.app_db_user} "
        f"password={settings.app_db_password} "
        f"sslmode={settings.app_db_sslmode}"
    )


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    try:
        conn = psycopg.connect(_conninfo(), row_factory=dict_row)
    except (psycopg.errors.InvalidCatalogName, psycopg.OperationalError) as exc:
        if "does not exist" not in str(exc) and not isinstance(exc, psycopg.errors.InvalidCatalogName):
            raise
        ensure_database()
        conn = psycopg.connect(_conninfo(), row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_database() -> None:
    settings = get_settings()
    db_name = settings.app_db_name
    if db_name in {"postgres", "template0", "template1"}:
        return
    maintenance = psycopg.connect(_conninfo_for_db("postgres"), autocommit=True)
    try:
        exists = maintenance.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s",
            (db_name,),
        ).fetchone()
        if not exists:
            with maintenance.cursor() as cur:
                cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name)))
    finally:
        maintenance.close()


def init_db() -> None:
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id UUID PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                hashed_password TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id UUID PRIMARY KEY,
                user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                deleted_at TIMESTAMPTZ
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS uploads (
                id UUID PRIMARY KEY,
                user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                session_id UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                filename TEXT NOT NULL,
                table_name TEXT NOT NULL,
                path TEXT NOT NULL,
                row_count INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_uploads_session_id ON uploads(session_id);")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_history (
                id UUID PRIMARY KEY,
                user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                session_id UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
                content TEXT NOT NULL,
                sql TEXT,
                payload JSONB,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_history_session_id ON chat_history(session_id, created_at);")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_quota_overrides (
                user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                quota_key TEXT NOT NULL,
                daily_limit INTEGER NOT NULL CHECK (daily_limit >= 0),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (user_id, quota_key)
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_upload_limits (
                user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                max_upload_mb INTEGER NOT NULL CHECK (max_upload_mb > 0),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_events (
                id UUID PRIMARY KEY,
                user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                session_id UUID REFERENCES sessions(id) ON DELETE SET NULL,
                quota_key TEXT NOT NULL,
                amount INTEGER NOT NULL CHECK (amount > 0),
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_events_user_key_time ON usage_events(user_id, quota_key, created_at);")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS model_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_by UUID REFERENCES users(id) ON DELETE SET NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def create_session(user_id: uuid.UUID) -> uuid.UUID:
    sid = uuid.uuid4()
    with connect() as conn:
        conn.execute(
            "INSERT INTO sessions (id, user_id) VALUES (%s, %s)",
            (sid, user_id),
        )
    return sid


def get_session(session_id: str, user_id: uuid.UUID) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT id, user_id, created_at, updated_at
            FROM sessions
            WHERE id = %s AND user_id = %s AND deleted_at IS NULL
            """,
            (session_id, user_id),
        ).fetchone()
    return dict(row) if row else None


def touch_session(session_id: str, user_id: uuid.UUID) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE sessions SET updated_at = now() WHERE id = %s AND user_id = %s AND deleted_at IS NULL",
            (session_id, user_id),
        )


def delete_session(session_id: str, user_id: uuid.UUID) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE sessions SET deleted_at = now(), updated_at = now() WHERE id = %s AND user_id = %s",
            (session_id, user_id),
        )


def record_upload(
    *,
    user_id: uuid.UUID,
    session_id: str,
    filename: str,
    table_name: str,
    path: str,
    row_count: int,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO uploads (id, user_id, session_id, filename, table_name, path, row_count)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (uuid.uuid4(), user_id, session_id, filename, table_name, path, row_count),
        )
        conn.execute(
            "UPDATE sessions SET updated_at = now() WHERE id = %s AND user_id = %s",
            (session_id, user_id),
        )


def add_chat_message(
    *,
    user_id: uuid.UUID,
    session_id: str,
    role: str,
    content: str,
    sql: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO chat_history (id, user_id, session_id, role, content, sql, payload)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (uuid.uuid4(), user_id, session_id, role, content, sql, Jsonb(payload or {})),
        )
        conn.execute(
            "UPDATE sessions SET updated_at = now() WHERE id = %s AND user_id = %s",
            (session_id, user_id),
        )


def list_chat_history(session_id: str, user_id: uuid.UUID, limit: int = 200) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT role, content, sql, payload, created_at
            FROM chat_history
            WHERE session_id = %s AND user_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (session_id, user_id, limit),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def get_quota_override(user_id: uuid.UUID, quota_key: str) -> int | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT daily_limit FROM user_quota_overrides WHERE user_id = %s AND quota_key = %s",
            (user_id, quota_key),
        ).fetchone()
    return int(row["daily_limit"]) if row else None


def get_upload_limit_mb(user_id: uuid.UUID) -> int | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT max_upload_mb FROM user_upload_limits WHERE user_id = %s",
            (user_id,),
        ).fetchone()
    return int(row["max_upload_mb"]) if row else None


def usage_count_since(user_id: uuid.UUID, quota_key: str, since: datetime) -> int:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS used
            FROM usage_events
            WHERE user_id = %s AND quota_key = %s AND created_at >= %s
            """,
            (user_id, quota_key, since),
        ).fetchone()
    return int(row["used"] or 0)


def record_usage(user_id: uuid.UUID, quota_key: str, amount: int = 1, session_id: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO usage_events (id, user_id, session_id, quota_key, amount)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (uuid.uuid4(), user_id, session_id, quota_key, amount),
        )


def list_users() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, email, role, created_at FROM users ORDER BY created_at DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def set_quota_override(user_id: uuid.UUID, quota_key: str, daily_limit: int) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO user_quota_overrides (user_id, quota_key, daily_limit, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (user_id, quota_key)
            DO UPDATE SET daily_limit = EXCLUDED.daily_limit, updated_at = now()
            """,
            (user_id, quota_key, daily_limit),
        )


def set_upload_limit_mb(user_id: uuid.UUID, max_upload_mb: int) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO user_upload_limits (user_id, max_upload_mb, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (user_id)
            DO UPDATE SET max_upload_mb = EXCLUDED.max_upload_mb, updated_at = now()
            """,
            (user_id, max_upload_mb),
        )


def list_sessions(user_id: uuid.UUID | None = None) -> list[dict[str, Any]]:
    where = "WHERE s.deleted_at IS NULL"
    params: tuple[Any, ...] = ()
    if user_id is not None:
        where += " AND s.user_id = %s"
        params = (user_id,)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
                s.id,
                s.user_id,
                u.email,
                s.created_at,
                s.updated_at,
                COUNT(DISTINCT up.id) AS upload_count,
                COUNT(DISTINCT ch.id) AS chat_count
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            LEFT JOIN uploads up ON up.session_id = s.id
            LEFT JOIN chat_history ch ON ch.session_id = s.id
            {where}
            GROUP BY s.id, s.user_id, u.email, s.created_at, s.updated_at
            ORDER BY s.updated_at DESC
            LIMIT 200
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def list_uploads(session_id: str, user_id: uuid.UUID | None = None) -> list[dict[str, Any]]:
    where = "WHERE session_id = %s"
    params: tuple[Any, ...] = (session_id,)
    if user_id is not None:
        where += " AND user_id = %s"
        params = (session_id, user_id)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT id, user_id, session_id, filename, table_name, path, row_count, created_at
            FROM uploads
            {where}
            ORDER BY created_at ASC
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def list_model_settings() -> dict[str, str]:
    with connect() as conn:
        rows = conn.execute("SELECT key, value FROM model_settings").fetchall()
    return {str(row["key"]): str(row["value"]) for row in rows}


def get_model_setting(key: str) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT value FROM model_settings WHERE key = %s", (key,)).fetchone()
    return str(row["value"]) if row else None


def set_model_setting(key: str, value: str, updated_by: uuid.UUID) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO model_settings (key, value, updated_by, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (key)
            DO UPDATE SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, updated_at = now()
            """,
            (key, value, updated_by),
        )
