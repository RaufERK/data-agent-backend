"""CSV/Excel upload → DuckDB in-memory table."""
from __future__ import annotations
from datetime import date, datetime
import io
import logging
import math
from numbers import Real
from pathlib import Path
from typing import Any, Dict, List, Optional
import pandas as pd
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import ORJSONResponse
from pydantic import BaseModel
from backend.services import session_store
from backend.services import app_db
from backend.services import usage
from backend.services.auth import CurrentUser, get_current_user

router = APIRouter(prefix="/sessions", tags=["upload"])
logger = logging.getLogger("data_agent.upload")


def _duckdb_type(dtype: str) -> str:
    d = dtype.upper()
    if any(t in d for t in ("INT", "BIGINT", "HUGEINT")):
        return "integer"
    if any(t in d for t in ("FLOAT", "DOUBLE", "DECIMAL", "REAL")):
        return "float"
    if "BOOL" in d:
        return "boolean"
    if any(t in d for t in ("DATE", "TIME", "TIMESTAMP")):
        return "datetime"
    return "string"


def _is_temporal_scalar(value: Any) -> bool:
    return isinstance(value, (pd.Timestamp, datetime, date))


def _is_numeric_scalar(value: Any) -> bool:
    return isinstance(value, Real) and not isinstance(value, bool) and not pd.isna(value)


def _parse_numeric_timestamp(value: Any) -> Optional[pd.Timestamp]:
    if not _is_numeric_scalar(value):
        return None

    numeric = float(value)
    if not math.isfinite(numeric):
        return None

    parsed = pd.NaT
    if 20_000 <= numeric <= 80_000:
        parsed = pd.to_datetime(numeric, unit="D", origin="1899-12-30", errors="coerce")
    else:
        abs_value = abs(int(numeric))
        unit: Optional[str] = None
        if abs_value >= 10**17:
            unit = "ns"
        elif abs_value >= 10**14:
            unit = "us"
        elif abs_value >= 10**11:
            unit = "ms"
        elif abs_value >= 10**9:
            unit = "s"
        if unit:
            parsed = pd.to_datetime(numeric, unit=unit, errors="coerce")

    return None if pd.isna(parsed) else pd.Timestamp(parsed)


def _stringify_temporal_value(value: Any) -> Any:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, str):
        return value
    if _is_temporal_scalar(value):
        return pd.Timestamp(value).isoformat(sep=" ")

    parsed = _parse_numeric_timestamp(value)
    if parsed is not None:
        return parsed.isoformat(sep=" ")
    return str(value)


def _normalize_mixed_temporal_columns(df: pd.DataFrame) -> pd.DataFrame:
    prepared = df.copy()
    normalized_columns: List[str] = []

    for column in prepared.columns:
        series = prepared[column]
        if not pd.api.types.is_object_dtype(series):
            continue

        non_null_values = [value for value in series.tolist() if not pd.isna(value)]
        if not non_null_values:
            continue

        has_temporal = any(_is_temporal_scalar(value) for value in non_null_values)
        has_numeric = any(_is_numeric_scalar(value) for value in non_null_values)
        if not (has_temporal and has_numeric):
            continue

        prepared[column] = series.map(_stringify_temporal_value)
        normalized_columns.append(str(column))

    if normalized_columns:
        logger.info("Normalized mixed temporal columns before DuckDB write columns=%s", normalized_columns)

    return prepared


def _table_name_from_filename(filename: str) -> str:
    return filename.rsplit(".", 1)[0].replace(" ", "_").replace("-", "_")


def _parse_upload_dataframe(raw: bytes, filename: str) -> pd.DataFrame:
    if filename.endswith((".xlsx", ".xls")):
        df = pd.read_excel(io.BytesIO(raw))
    else:
        df = pd.read_csv(io.BytesIO(raw))
    return _normalize_mixed_temporal_columns(df)


def _write_dataframe_to_session(conn, table_name: str, df: pd.DataFrame) -> tuple[list[dict], int]:
    conn.execute(f'DROP TABLE IF EXISTS "{table_name}"')
    conn.register("__tmp__", df)
    conn.execute(f'CREATE TABLE "{table_name}" AS SELECT * FROM "__tmp__"')
    conn.unregister("__tmp__")
    desc = conn.execute(f'DESCRIBE "{table_name}"').fetchdf()
    columns = [
        {"name": r["column_name"], "type": _duckdb_type(r["column_type"]), "raw_type": r["column_type"]}
        for _, r in desc.iterrows()
    ]
    row_count = conn.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0]
    return columns, int(row_count)


@router.post("", summary="Create a new session")
def create_session(current_user: CurrentUser = Depends(get_current_user)):
    sid = session_store.create_session(current_user.id)
    logger.info("Created session user=%s session=%s", current_user.id, sid)
    return {"session_id": sid}


@router.get("", summary="List persisted sessions")
def list_sessions(current_user: CurrentUser = Depends(get_current_user)):
    user_filter = None if current_user.role == "admin" else current_user.id
    return ORJSONResponse({"sessions": app_db.list_sessions(user_filter)})


@router.get("/{session_id}/history", summary="List uploads and chat history for a session")
def session_history(session_id: str, current_user: CurrentUser = Depends(get_current_user)):
    owner_filter = None if current_user.role == "admin" else current_user.id
    session = app_db.get_session(session_id, current_user.id) if owner_filter else None
    if owner_filter and not session:
        raise HTTPException(404, f"Session '{session_id}' not found")
    return ORJSONResponse({
        "session_id": session_id,
        "uploads": app_db.list_uploads(session_id, owner_filter),
        "chat": app_db.list_chat_history(session_id, current_user.id, 200) if owner_filter else [],
    })


@router.post("/{session_id}/upload", summary="Upload CSV or Excel into session")
async def upload_file(
    session_id: str,
    file: UploadFile = File(...),
    table_name: str = Form(None),
    current_user: CurrentUser = Depends(get_current_user),
):
    logger.info("Upload requested user=%s session=%s filename=%s table_name=%s", current_user.id, session_id, file.filename, table_name)
    try:
        conn = session_store.get_conn(session_id, current_user.id)
    except KeyError:
        raise HTTPException(404, f"Session '{session_id}' not found")

    logger.info("Reading raw bytes for session=%s filename=%s", session_id, file.filename)
    raw = await file.read()
    usage.enforce_upload_size(current_user.id, len(raw))
    usage.consume(current_user.id, "upload_files", role=current_user.role, session_id=session_id)
    fname = Path(file.filename or "upload").name
    logger.info("Read %s bytes for session=%s filename=%s", len(raw), session_id, fname)
    upload_path = session_store.session_dir(current_user.id, session_id) / fname
    upload_path.write_bytes(raw)

    if not table_name:
        table_name = _table_name_from_filename(fname)

    try:
        logger.info("Parsing file session=%s filename=%s table=%s", session_id, fname, table_name)
        df = _parse_upload_dataframe(raw, fname)
        logger.info("Parsed dataframe session=%s table=%s shape=%s", session_id, table_name, df.shape)
    except Exception as e:
        logger.exception("Cannot parse file session=%s filename=%s", session_id, fname)
        raise HTTPException(400, f"Cannot parse file: {e}")

    try:
        logger.info("Writing dataframe to DuckDB session=%s table=%s", session_id, table_name)
        columns, row_count = _write_dataframe_to_session(conn, table_name, df)
        logger.info("DuckDB write completed session=%s table=%s", session_id, table_name)
    except Exception as e:
        logger.exception("DuckDB write failed session=%s table=%s", session_id, table_name)
        raise HTTPException(500, f"DuckDB error: {e}")

    app_db.record_upload(
        user_id=current_user.id,
        session_id=session_id,
        filename=fname,
        table_name=table_name,
        path=str(upload_path),
        row_count=row_count,
    )
    logger.info("Upload finished session=%s table=%s rows=%s cols=%s", session_id, table_name, row_count, len(columns))

    return ORJSONResponse({
        "session_id": session_id,
        "table_name": table_name,
        "row_count": row_count,
        "columns": columns,
    })


@router.post("/{session_id}/reimport", summary="Reimport a persisted session from saved uploaded files")
def reimport_session(session_id: str, current_user: CurrentUser = Depends(get_current_user)):
    owner_filter = None if current_user.role == "admin" else current_user.id
    if owner_filter and not app_db.get_session(session_id, current_user.id):
        raise HTTPException(404, f"Session '{session_id}' not found")
    uploads = app_db.list_uploads(session_id, owner_filter)
    if not uploads:
        raise HTTPException(400, "No persisted uploads found for this session")

    target_user_id = current_user.id if owner_filter else uploads[0]["user_id"]
    new_sid = str(session_store.create_session(target_user_id))
    conn = session_store.get_conn(new_sid, target_user_id)
    imported = []
    for item in uploads:
        path = Path(str(item["path"]))
        if not path.exists():
            logger.warning("Reimport skipped missing file session=%s path=%s", session_id, path)
            continue
        raw = path.read_bytes()
        fname = str(item["filename"])
        table_name = str(item["table_name"] or _table_name_from_filename(fname))
        try:
            df = _parse_upload_dataframe(raw, fname)
            columns, row_count = _write_dataframe_to_session(conn, table_name, df)
            app_db.record_upload(
                user_id=target_user_id,
                session_id=new_sid,
                filename=fname,
                table_name=table_name,
                path=str(path),
                row_count=row_count,
            )
            imported.append({"filename": fname, "table_name": table_name, "row_count": row_count, "columns": columns})
        except Exception as exc:
            logger.exception("Reimport failed source_session=%s new_session=%s file=%s", session_id, new_sid, fname)
            raise HTTPException(500, f"Cannot reimport {fname}: {exc}") from exc
    if not imported:
        raise HTTPException(400, "No files could be reimported")
    return ORJSONResponse({"source_session_id": session_id, "session_id": new_sid, "uploads": imported})


@router.get("/{session_id}/tables", summary="List tables in session")
def list_tables(session_id: str, current_user: CurrentUser = Depends(get_current_user)):
    logger.info("List tables requested for session=%s", session_id)
    try:
        tables = session_store.list_tables(session_id, current_user.id)
    except KeyError:
        raise HTTPException(404, f"Session '{session_id}' not found")
    return {"session_id": session_id, "tables": tables}


@router.get("/{session_id}/tables/{table_name}/preview", summary="Preview table contents")
def table_preview(session_id: str, table_name: str, limit: int = 1000, current_user: CurrentUser = Depends(get_current_user)):
    logger.info("Preview requested for session=%s table=%s limit=%s", session_id, table_name, limit)
    try:
        conn = session_store.get_conn(session_id, current_user.id)
    except KeyError:
        raise HTTPException(404, f"Session '{session_id}' not found")

    tables = session_store.list_tables(session_id, current_user.id)
    if table_name not in tables:
        raise HTTPException(404, f"Table '{table_name}' not found in session")

    try:
        total_rows = conn.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0]
        df = conn.execute(f'SELECT * FROM "{table_name}" LIMIT {max(1, min(limit, 5000))}').fetchdf()
    except Exception as e:
        raise HTTPException(400, f"Cannot preview table: {e}")

    # Cast timestamps and NaN to JSON-safe types
    df = df.astype(object).where(df.notna(), None)
    for col in df.columns:
        df[col] = df[col].apply(lambda v: str(v) if hasattr(v, 'isoformat') else v)

    return ORJSONResponse({
        "session_id": session_id,
        "table_name": table_name,
        "columns": list(df.columns),
        "data": df.to_dict(orient="records"),
        "row_count": total_rows,
    })


class VitrinaPayload(BaseModel):
    FactDashboard: List[Dict[str, Any]] = []
    FactKPIs: List[Dict[str, Any]] = []
    widget_meta: Optional[Dict[str, Any]] = None


@router.post("/{session_id}/vitrina", summary="Load vision vitrina tables into session")
def load_vitrina(session_id: str, body: VitrinaPayload, current_user: CurrentUser = Depends(get_current_user)):
    logger.info("Vitrina load requested for session=%s", session_id)
    try:
        conn = session_store.get_conn(session_id, current_user.id)
    except KeyError:
        raise HTTPException(404, f"Session '{session_id}' not found")

    tables_created = []
    try:
        if body.FactDashboard:
            df = pd.DataFrame(body.FactDashboard)
            conn.execute('DROP TABLE IF EXISTS "FactDashboard"')
            conn.register("__tmp_fd__", df)
            conn.execute('CREATE TABLE "FactDashboard" AS SELECT * FROM "__tmp_fd__"')
            conn.unregister("__tmp_fd__")
            tables_created.append("FactDashboard")

        if body.FactKPIs:
            df = pd.DataFrame(body.FactKPIs)
            conn.execute('DROP TABLE IF EXISTS "FactKPIs"')
            conn.register("__tmp_kpi__", df)
            conn.execute('CREATE TABLE "FactKPIs" AS SELECT * FROM "__tmp_kpi__"')
            conn.unregister("__tmp_kpi__")
            tables_created.append("FactKPIs")

    except Exception as e:
        logger.exception("Vitrina load failed session=%s", session_id)
        raise HTTPException(500, f"DuckDB error: {e}")

    logger.info("Vitrina loaded session=%s tables=%s", session_id, tables_created)
    return ORJSONResponse({"session_id": session_id, "tables_created": tables_created})


@router.delete("/{session_id}", summary="Delete session")
def delete_session(session_id: str, current_user: CurrentUser = Depends(get_current_user)):
    logger.info("Delete session requested for session=%s", session_id)
    session_store.delete_session(session_id, current_user.id)
    return {"deleted": session_id}
