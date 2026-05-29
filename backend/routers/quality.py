"""Data quality endpoints."""
from __future__ import annotations
import logging
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import ORJSONResponse, StreamingResponse
from pydantic import BaseModel
from backend.services import session_store
from backend.services import data_versions
from backend.services.quality import run_quality_checks
from backend.services.cleaning import clean_session
from backend.services.model_advisor import analyze_model_options
from backend.services.schema_analyzer import analyze_schema
from backend.services.semantic_manifest import build_semantic_manifest
from backend.services.project_memory import add_instruction, delete_instruction, list_instructions
from backend.services.auth import CurrentUser, get_current_user

router = APIRouter(prefix="/sessions", tags=["quality"])
logger = logging.getLogger("data_agent.quality")


class DataVersionCreateRequest(BaseModel):
    instruction: str
    name: str | None = None


class MemoryCreateRequest(BaseModel):
    instruction: str
    scope: str = "project"


@router.get("/{session_id}/quality/{table_name}", summary="Run data quality checks")
def quality_check(session_id: str, table_name: str, current_user: CurrentUser = Depends(get_current_user)):
    logger.info("Quality requested session=%s table=%s", session_id, table_name)
    try:
        conn = session_store.get_conn(session_id, current_user.id)
    except KeyError:
        raise HTTPException(404, f"Session '{session_id}' not found")

    tables = session_store.list_tables(session_id, current_user.id)
    if table_name not in tables:
        raise HTTPException(404, f"Table '{table_name}' not found in session")

    result = run_quality_checks(conn, table_name)
    logger.info("Quality completed session=%s table=%s summary=%s", session_id, table_name, result.get("summary"))
    return ORJSONResponse({"session_id": session_id, "table_name": table_name, **result})


@router.post("/{session_id}/clean", summary="Run server-side data cleaning")
def clean_data(session_id: str, current_user: CurrentUser = Depends(get_current_user)):
    logger.info("Clean requested session=%s", session_id)
    try:
        conn = session_store.get_conn(session_id, current_user.id)
    except KeyError:
        raise HTTPException(404, f"Session '{session_id}' not found")

    tables = session_store.list_tables(session_id, current_user.id)
    if not tables:
        raise HTTPException(400, "No tables in session. Upload a file first.")

    cleaned_tables = clean_session(conn, tables)
    logger.info("Clean completed session=%s cleaned_tables=%s", session_id, cleaned_tables)
    return ORJSONResponse({"session_id": session_id, "cleaned_tables": cleaned_tables})


@router.get("/{session_id}/schema", summary="Analyze schema: detect PK/FK, suggest data model")
def schema_analysis(session_id: str, current_user: CurrentUser = Depends(get_current_user)):
    logger.info("Schema analysis requested session=%s", session_id)
    try:
        conn = session_store.get_conn(session_id, current_user.id)
    except KeyError:
        raise HTTPException(404, f"Session '{session_id}' not found")

    result = analyze_schema(conn)
    logger.info("Schema analysis done session=%s tables=%d", session_id, len(result["tables"]))
    return ORJSONResponse({"session_id": session_id, **result})


@router.get("/{session_id}/semantic-manifest", summary="Build lightweight semantic manifest for agents")
def semantic_manifest(session_id: str, current_user: CurrentUser = Depends(get_current_user)):
    logger.info("Semantic manifest requested session=%s", session_id)
    try:
        conn = session_store.get_conn(session_id, current_user.id)
    except KeyError:
        raise HTTPException(404, f"Session '{session_id}' not found")

    tables = session_store.list_tables(session_id, current_user.id)
    if not tables:
        raise HTTPException(400, "No tables in session. Upload a file first.")

    result = build_semantic_manifest(conn)
    logger.info("Semantic manifest done session=%s tables=%d", session_id, len(result["tables"]))
    return ORJSONResponse({"session_id": session_id, **result})


@router.get("/{session_id}/memory", summary="List session-scoped business instructions")
def memory_list(session_id: str, current_user: CurrentUser = Depends(get_current_user)):
    try:
        conn = session_store.get_conn(session_id, current_user.id)
    except KeyError:
        raise HTTPException(404, f"Session '{session_id}' not found")
    return ORJSONResponse({"session_id": session_id, "items": list_instructions(conn)})


@router.post("/{session_id}/memory", summary="Add business instruction to agent memory")
def memory_create(session_id: str, body: MemoryCreateRequest, current_user: CurrentUser = Depends(get_current_user)):
    try:
        conn = session_store.get_conn(session_id, current_user.id)
    except KeyError:
        raise HTTPException(404, f"Session '{session_id}' not found")
    try:
        item = add_instruction(conn, body.instruction, body.scope)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return ORJSONResponse({"session_id": session_id, "item": item})


@router.delete("/{session_id}/memory/{memory_id}", summary="Delete business instruction from agent memory")
def memory_delete(session_id: str, memory_id: str, current_user: CurrentUser = Depends(get_current_user)):
    try:
        conn = session_store.get_conn(session_id, current_user.id)
    except KeyError:
        raise HTTPException(404, f"Session '{session_id}' not found")
    deleted = delete_instruction(conn, memory_id)
    return ORJSONResponse({"session_id": session_id, "deleted": deleted, "memory_id": memory_id})


@router.get("/{session_id}/model-advice", summary="Recommend whether a data model and detail layer are needed")
def model_advice(session_id: str, current_user: CurrentUser = Depends(get_current_user)):
    logger.info("Model advice requested session=%s", session_id)
    try:
        conn = session_store.get_conn(session_id, current_user.id)
    except KeyError:
        raise HTTPException(404, f"Session '{session_id}' not found")

    tables = session_store.list_tables(session_id, current_user.id)
    if not tables:
        raise HTTPException(400, "No tables in session. Upload a file first.")

    result = analyze_model_options(conn)
    logger.info(
        "Model advice done session=%s recommended=%s need_model=%s need_detail=%s",
        session_id,
        result.get("recommended_option"),
        result.get("need_data_model"),
        result.get("need_detail_layer"),
    )
    return ORJSONResponse({"session_id": session_id, **result})


@router.get("/{session_id}/versions", summary="List derived data versions")
def list_data_versions(session_id: str, current_user: CurrentUser = Depends(get_current_user)):
    logger.info("Data versions list requested session=%s", session_id)
    try:
        conn = session_store.get_conn(session_id, current_user.id)
    except KeyError:
        raise HTTPException(404, f"Session '{session_id}' not found")
    return ORJSONResponse({"session_id": session_id, "versions": data_versions.list_versions(conn)})


@router.post("/{session_id}/versions", summary="Create a derived data version")
def create_data_version(session_id: str, body: DataVersionCreateRequest, current_user: CurrentUser = Depends(get_current_user)):
    logger.info("Data version create requested session=%s instruction=%s", session_id, body.instruction)
    try:
        conn = session_store.get_conn(session_id, current_user.id)
    except KeyError:
        raise HTTPException(404, f"Session '{session_id}' not found")

    if not body.instruction.strip():
        raise HTTPException(400, "instruction is required")
    try:
        version = data_versions.create_version(conn, body.instruction.strip(), body.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        logger.exception("Data version create failed session=%s", session_id)
        raise HTTPException(500, f"Data version error: {exc}")
    return ORJSONResponse({"session_id": session_id, "version": version})


@router.get("/{session_id}/versions/{version_id}/preview", summary="Preview a derived data version")
def preview_data_version(session_id: str, version_id: str, limit: int = 1000, current_user: CurrentUser = Depends(get_current_user)):
    logger.info("Data version preview requested session=%s version=%s", session_id, version_id)
    try:
        conn = session_store.get_conn(session_id, current_user.id)
    except KeyError:
        raise HTTPException(404, f"Session '{session_id}' not found")

    version = data_versions.get_version(conn, version_id)
    if not version:
        raise HTTPException(404, f"Version '{version_id}' not found")
    try:
        safe_limit = max(1, min(limit, 5000))
        table_name = str(version["table_name"])
        total_rows = conn.execute(f"SELECT COUNT(*) FROM {data_versions.quote_ident(table_name)}").fetchone()[0]
        df = conn.execute(f"SELECT * FROM {data_versions.quote_ident(table_name)} LIMIT {safe_limit}").fetchdf()
    except Exception as exc:
        raise HTTPException(400, f"Cannot preview version: {exc}")
    return ORJSONResponse({
        "session_id": session_id,
        "version_id": version_id,
        "table_name": version["table_name"],
        "columns": list(df.columns),
        "data": data_versions.dataframe_records(df),
        "row_count": total_rows,
    })


@router.get("/{session_id}/versions/{version_id}/csv", summary="Download a derived data version as CSV")
def download_data_version_csv(session_id: str, version_id: str, current_user: CurrentUser = Depends(get_current_user)):
    logger.info("Data version CSV requested session=%s version=%s", session_id, version_id)
    try:
        conn = session_store.get_conn(session_id, current_user.id)
    except KeyError:
        raise HTTPException(404, f"Session '{session_id}' not found")

    version = data_versions.get_version(conn, version_id)
    if not version:
        raise HTTPException(404, f"Version '{version_id}' not found")
    table_name = str(version["table_name"])
    df = conn.execute(f"SELECT * FROM {data_versions.quote_ident(table_name)}").fetchdf()
    csv_bytes = df.to_csv(index=False).encode("utf-8-sig")
    filename = f"data_agent_{version_id}.csv"
    return StreamingResponse(
        iter([csv_bytes]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
