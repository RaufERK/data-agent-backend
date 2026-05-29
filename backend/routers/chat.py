"""Chat → SQL → result endpoint."""
from __future__ import annotations
import logging
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import ORJSONResponse
from pydantic import BaseModel
from backend.services import session_store
from backend.services import usage
from backend.services.quality import run_quality_checks
from backend.services.sql_agent import answer_with_sql, _df_to_records
from backend.services.dashboard_agent import generate_dashboard
from backend.services.presentation_brief import build_presentation_brief
from backend.services.project_memory import add_instruction, list_instructions
from backend.services.semantic_manifest import build_semantic_manifest
from backend.services import app_db
from backend.services.auth import CurrentUser, get_current_user

router = APIRouter(prefix="/sessions", tags=["chat"])
logger = logging.getLogger("data_agent.chat")


class ChatRequest(BaseModel):
    question: str


ISSUE_LABELS = {
    "null": "пустые значения",
    "duplicate": "дубликаты",
    "case_mismatch": "разный регистр",
    "invalid_date": "некорректные даты",
    "date_order": "дата окончания раньше даты выдачи",
    "non_numeric": "нечисловые значения",
    "missing_required_approval": "нет обязательного согласования СЭБ",
    "reference_mismatch": "значения не найдены в справочнике",
}


def _looks_like_quality_question(text: str) -> bool:
    lowered = text.lower()
    markers = (
        "качество",
        "проблем",
        "ошиб",
        "предупреж",
        "дублик",
        "пуст",
        "пропуск",
        "null",
        "сэб",
        "конфликт",
        "дата",
        "справочник",
        "регистр",
    )
    return any(marker in lowered for marker in markers)


def _quality_summary_payload(conn, session_id: str, question: str, tables: list[str]) -> dict:
    rows: list[dict] = []
    total_errors = 0
    total_warnings = 0

    for table in tables:
        result = run_quality_checks(conn, table)
        total_errors += int(result.get("summary", {}).get("errors", 0) or 0)
        total_warnings += int(result.get("summary", {}).get("warnings", 0) or 0)
        for column in result.get("columns", []):
            for issue in column.get("issues", []):
                rows.append({
                    "table": table,
                    "column": column.get("column"),
                    "issue": ISSUE_LABELS.get(str(issue.get("type")), str(issue.get("type"))),
                    "severity": issue.get("severity"),
                    "count": issue.get("count"),
                    "pct": issue.get("pct"),
                })

    rows.sort(key=lambda item: (0 if item["severity"] == "error" else 1, -(int(item["count"] or 0))))
    top_rows = rows[:8]
    if not rows:
        answer = "По текущим данным критичных проблем качества не нашёл. Можно переходить к модели данных или дашборду."
    else:
        lines = [
            f"Нашёл {len(rows)} проблем качества: {total_errors} ошибок и {total_warnings} предупреждений.",
            "Главное:",
        ]
        for item in top_rows:
            pct = f" ({item['pct']}%)" if item.get("pct") is not None else ""
            severity = "ошибка" if item["severity"] == "error" else "предупреждение"
            lines.append(
                f"• {item['table']}.{item['column']}: {item['issue']} — {item['count']} строк{pct}, {severity}."
            )
        lines.append("Рекомендую сначала исправить ошибки, затем предупреждения по справочникам и пустым значениям.")
        answer = "\n".join(lines)

    return {
        "session_id": session_id,
        "question": question,
        "sql": "",
        "answer": answer,
        "columns": ["table", "column", "issue", "severity", "count", "pct"],
        "data": top_rows,
        "row_count": len(rows),
    }


def _extract_memory_instruction(text: str) -> str | None:
    lowered = text.strip().lower()
    prefixes = ("запомни, что", "запомни что", "учти, что", "учти что")
    for prefix in prefixes:
        if lowered.startswith(prefix):
            return text.strip()[len(prefix):].strip(" .:;")
    return None


@router.post("/{session_id}/chat", summary="Ask a question → get SQL + data")
def chat(session_id: str, body: ChatRequest, current_user: CurrentUser = Depends(get_current_user)):
    logger.info("Chat requested session=%s question=%s", session_id, body.question)
    try:
        conn = session_store.get_conn(session_id, current_user.id)
    except KeyError:
        raise HTTPException(404, f"Session '{session_id}' not found")

    tables = session_store.list_tables(session_id, current_user.id)
    if not tables:
        raise HTTPException(400, "No tables in session. Upload a file first.")

    try:
        usage.consume(current_user.id, "assistant_questions", role=current_user.role, session_id=session_id)
        app_db.add_chat_message(user_id=current_user.id, session_id=session_id, role="user", content=body.question)
        memory_instruction = _extract_memory_instruction(body.question)
        if memory_instruction:
            item = add_instruction(conn, memory_instruction)
            answer = f"Запомнил правило: {item['instruction']}"
            app_db.add_chat_message(user_id=current_user.id, session_id=session_id, role="assistant", content=answer)
            return ORJSONResponse({
                "session_id": session_id,
                "question": body.question,
                "sql": "",
                "answer": answer,
                "columns": [],
                "data": [],
                "row_count": 0,
            })

        if any(marker in body.question.lower() for marker in ("какие вопросы", "предложи вопросы", "что спросить")):
            manifest = build_semantic_manifest(conn)
            questions = manifest.get("recommended_questions", [])
            memory = list_instructions(conn)
            answer = "Можно начать с:\n" + "\n".join(f"• {q}" for q in questions[:6])
            if memory:
                answer += "\n\nУчту сохранённые правила:\n" + "\n".join(f"• {item['instruction']}" for item in memory)
            app_db.add_chat_message(
                user_id=current_user.id,
                session_id=session_id,
                role="assistant",
                content=answer,
                payload={"questions": questions},
            )
            return ORJSONResponse({
                "session_id": session_id,
                "question": body.question,
                "sql": "",
                "answer": answer,
                "columns": ["question"],
                "data": [{"question": q} for q in questions],
                "row_count": len(questions),
            })

        if _looks_like_quality_question(body.question):
            payload = _quality_summary_payload(conn, session_id, body.question, tables)
            app_db.add_chat_message(
                user_id=current_user.id,
                session_id=session_id,
                role="assistant",
                content=str(payload.get("answer") or ""),
                payload={"fallback": "quality_summary"},
            )
            return ORJSONResponse(payload)

        result = answer_with_sql(conn, body.question)
        app_db.add_chat_message(
            user_id=current_user.id,
            session_id=session_id,
            role="assistant",
            content=str(result.get("answer") or ""),
            sql=str(result.get("sql") or ""),
            payload={"columns": result.get("columns"), "row_count": result.get("row_count")},
        )
    except ValueError as e:
        logger.warning("Chat value error session=%s question=%s error=%s", session_id, body.question, e)
        answer = (
            "Не смог построить SQL для этого вопроса.\n\n"
            f"Причина: {e}"
        )
        app_db.add_chat_message(
            user_id=current_user.id,
            session_id=session_id,
            role="assistant",
            content=answer,
            payload={"fallback": "sql_generation_error", "error": str(e)},
        )
        return ORJSONResponse({
            "session_id": session_id,
            "question": body.question,
            "sql": "",
            "answer": answer,
            "columns": [],
            "data": [],
            "row_count": 0,
        })
    except Exception as e:
        logger.exception("Chat agent error session=%s question=%s", session_id, body.question)
        raise HTTPException(500, f"Agent error: {e}")

    logger.info("Chat completed session=%s sql=%s rows=%s", session_id, result.get("sql"), result.get("row_count"))
    return ORJSONResponse({"session_id": session_id, "question": body.question, **result})


@router.get("/{session_id}/chat/history", summary="List persisted chat history")
def chat_history(session_id: str, current_user: CurrentUser = Depends(get_current_user), limit: int = 200):
    try:
        session_store.get_conn(session_id, current_user.id)
    except KeyError:
        raise HTTPException(404, f"Session '{session_id}' not found")
    return ORJSONResponse({
        "session_id": session_id,
        "items": app_db.list_chat_history(session_id, current_user.id, max(1, min(limit, 500))),
    })


class DashboardRequest(BaseModel):
    topic: str


class PresentationBriefRequest(BaseModel):
    title: str = "Data brief"
    charts: list[dict] = []


@router.post("/{session_id}/dashboard", summary="Generate dashboard charts for a topic")
def generate_dashboard_endpoint(session_id: str, body: DashboardRequest, current_user: CurrentUser = Depends(get_current_user)):
    logger.info("Dashboard requested session=%s topic=%s", session_id, body.topic)
    try:
        conn = session_store.get_conn(session_id, current_user.id)
    except KeyError:
        raise HTTPException(404, f"Session '{session_id}' not found")

    if not session_store.list_tables(session_id, current_user.id):
        raise HTTPException(400, "No tables in session. Upload a file first.")

    try:
        usage.consume(current_user.id, "dashboard_generations", role=current_user.role, session_id=session_id)
        charts = generate_dashboard(conn, body.topic)
    except Exception as e:
        logger.exception("Dashboard generation failed session=%s topic=%s", session_id, body.topic)
        raise HTTPException(500, f"Dashboard error: {e}")

    logger.info("Dashboard done session=%s charts=%d", session_id, len(charts))
    return ORJSONResponse({"session_id": session_id, "topic": body.topic, "charts": charts})


@router.post("/{session_id}/presentation-brief", summary="Generate dataset-aware presentation brief")
def presentation_brief_endpoint(session_id: str, body: PresentationBriefRequest, current_user: CurrentUser = Depends(get_current_user)):
    logger.info("Presentation brief requested session=%s title=%s", session_id, body.title)
    try:
        conn = session_store.get_conn(session_id, current_user.id)
    except KeyError:
        raise HTTPException(404, f"Session '{session_id}' not found")

    if not session_store.list_tables(session_id, current_user.id):
        raise HTTPException(400, "No tables in session. Upload a file first.")

    try:
        usage.consume(current_user.id, "assistant_questions", role=current_user.role, session_id=session_id)
        brief = build_presentation_brief(conn, body.title, body.charts)
    except Exception as e:
        logger.exception("Presentation brief failed session=%s", session_id)
        raise HTTPException(500, f"Presentation brief error: {e}")

    return ORJSONResponse({"session_id": session_id, "brief": brief})


@router.post("/{session_id}/query", summary="Run raw SQL directly")
def raw_query(session_id: str, body: dict, current_user: CurrentUser = Depends(get_current_user)):
    logger.info("Raw query requested session=%s", session_id)
    try:
        conn = session_store.get_conn(session_id, current_user.id)
    except KeyError:
        raise HTTPException(404, f"Session '{session_id}' not found")

    sql = body.get("sql", "").strip()
    if not sql:
        raise HTTPException(400, "sql is required")

    try:
        df = conn.execute(sql).fetchdf().head(1000)
    except Exception as e:
        logger.warning("Raw query failed session=%s sql=%s error=%s", session_id, sql, e)
        raise HTTPException(400, f"SQL error: {e}")

    logger.info("Raw query completed session=%s rows=%s", session_id, len(df))
    return ORJSONResponse({
        "columns": list(df.columns),
        "data": _df_to_records(df),
        "row_count": len(df),
    })
