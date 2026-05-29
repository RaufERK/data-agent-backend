"""DuckDB session store — persists each session to disk so data survives restarts."""
from __future__ import annotations
import logging
import os
import shutil
import threading
import uuid
from pathlib import Path
import duckdb
from backend.config import get_settings
from backend.services import app_db
from backend.services.data_versions import is_internal_table

logger = logging.getLogger(__name__)

_session_ids: set[str] = set()
_lock = threading.Lock()

# Thread-local storage: each thread gets its own DuckDB connection per session.
_tls = threading.local()


def user_upload_root(user_id: uuid.UUID | str) -> Path:
    root = Path(get_settings().upload_dir)
    path = root / str(user_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def session_dir(user_id: uuid.UUID | str, sid: str) -> Path:
    path = user_upload_root(user_id) / f"session_{sid}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _session_path(sid: str, user_id: uuid.UUID | str) -> Path:
    return session_dir(user_id, sid) / "session.duckdb"


def _conn_key(sid: str, user_id: uuid.UUID | str) -> str:
    return f"{user_id}:{sid}"


def _open_conn(sid: str, user_id: uuid.UUID | str) -> duckdb.DuckDBPyConnection:
    """Open a new DuckDB connection for this thread."""
    path = _session_path(sid, user_id)
    conn = duckdb.connect(str(path))
    return conn


def _tls_conns() -> dict[str, duckdb.DuckDBPyConnection]:
    if not hasattr(_tls, "conns"):
        _tls.conns = {}
    return _tls.conns


def close_thread_connections() -> None:
    conns = _tls_conns()
    for conn in list(conns.values()):
        try:
            conn.close()
        except Exception:
            pass
    conns.clear()


def create_session(user_id: uuid.UUID) -> str:
    sid = str(app_db.create_session(user_id))
    with _lock:
        _session_ids.add(sid)
    _session_path(sid, user_id)  # ensure dir exists
    conn = _open_conn(sid, user_id)
    conn.close()
    logger.info("Session created user=%s sid=%s path=%s", user_id, sid, _session_path(sid, user_id))
    return sid


def get_conn(sid: str, user_id: uuid.UUID) -> duckdb.DuckDBPyConnection:
    if not app_db.get_session(sid, user_id):
        raise KeyError(f"Session '{sid}' not found")

    path = _session_path(sid, user_id)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)

    with _lock:
        _session_ids.add(sid)

    conn = _open_conn(sid, user_id)
    logger.info("Opened connection for session user=%s sid=%s thread=%s", user_id, sid, threading.current_thread().name)
    return conn


def delete_session(sid: str, user_id: uuid.UUID) -> None:
    app_db.delete_session(sid, user_id)
    with _lock:
        _session_ids.discard(sid)
    path = session_dir(user_id, sid)
    if path.exists() and path.is_dir():
        try:
            shutil.rmtree(path)
            logger.info("Session deleted user=%s sid=%s", user_id, sid)
        except Exception as exc:
            logger.warning("Could not delete session file sid=%s: %s", sid, exc)


def list_tables(sid: str, user_id: uuid.UUID) -> list[str]:
    conn = get_conn(sid, user_id)
    return [t for (t,) in conn.execute("SHOW TABLES").fetchall() if not is_internal_table(t)]
