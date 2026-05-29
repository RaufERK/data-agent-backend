"""Data Agent — FastAPI backend."""
from __future__ import annotations

import logging
import re
import time
import uuid
from mimetypes import guess_type

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.exceptions import HTTPException
from fastapi import Request
from fastapi.middleware.cors import CORSMiddleware

from backend.config import get_settings
from backend.routers import admin, auth, oidc, upload, quality, chat, image, export
from backend.services.app_db import init_db
from backend.services.auth import decode_access_token
from backend.services import vision_jobs
from backend.services.log_context import clear_context, configure_logging, set_context
from backend.services.session_store import close_thread_connections

settings = get_settings()
configure_logging(structured=settings.structured_logs)
logger = logging.getLogger("data_agent")

app = FastAPI(title="Data Agent API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup() -> None:
    last_error: Exception | None = None
    for _ in range(20):
        try:
            init_db()
            vision_jobs.start_workers()
            return
        except Exception as exc:
            last_error = exc
            logger.warning("App DB is not ready yet: %s", exc)
            time.sleep(1)
    raise RuntimeError(f"App DB initialization failed: {last_error}")


@app.on_event("shutdown")
async def shutdown() -> None:
    await vision_jobs.stop_workers()
    close_thread_connections()


app.include_router(auth.router, prefix="/api")
app.include_router(oidc.router, prefix="/api")
app.include_router(admin.router, prefix="/api")
app.include_router(upload.router, prefix="/api")
app.include_router(quality.router, prefix="/api")
app.include_router(chat.router, prefix="/api")
app.include_router(image.router, prefix="/api")
app.include_router(export.router, prefix="/api")


@app.middleware("http")
async def log_requests(request: Request, call_next):
    started_at = time.perf_counter()
    request_id = str(uuid.uuid4())
    user_id = "-"
    token = request.cookies.get(settings.auth_cookie_name)
    if token:
        try:
            user_id = str(decode_access_token(token).get("sub") or "-")
        except Exception:
            user_id = "-"
    session_match = re.search(r"/sessions/([0-9a-fA-F-]{36})", request.url.path)
    session_id = session_match.group(1) if session_match else "-"
    set_context(request_id=request_id, user_id=user_id, session_id=session_id, method=request.method, path=request.url.path)
    logger.info("HTTP start %s %s", request.method, request.url.path)
    try:
        response = await call_next(request)
    except Exception:
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        logger.exception("HTTP error %s %s in %.1fms", request.method, request.url.path, elapsed_ms)
        clear_context()
        raise
    finally:
        close_thread_connections()
    elapsed_ms = (time.perf_counter() - started_at) * 1000
    logger.info(
        "HTTP done %s %s -> %s in %.1fms",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    clear_context()
    return response


@app.get("/{full_path:path}")
async def send_static_and_spa(full_path: str):
    # Полный путь к запрошенному файлу
    file_path = Path(__file__).resolve().parent / 'static' / full_path

    # Если это файл (с расширением), пытаемся его отдать
    if '.' in full_path and file_path.exists() and file_path.is_file():
        with open(file_path, 'rb') as f:
            content = f.read()
        content_type, _ = guess_type(str(file_path))
        return Response(content, media_type=content_type)

    # Если файл не найден или это "чистый" путь (без расширения) — отдаём index.html
    # Это нужно для корректной работы React Router
    index_path = Path(__file__).resolve().parent / 'static' / "index.html"
    if index_path.exists():
        with open(index_path, encoding='utf8') as f:
            content = f.read()
        return Response(content, media_type='text/html')

    raise HTTPException(status_code=404, detail="File or page not found")


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "data-agent"}
