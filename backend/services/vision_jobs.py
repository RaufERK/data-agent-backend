"""In-process async queue for blocking Vision LLM analysis."""
from __future__ import annotations

import asyncio
import logging
import tempfile
import uuid
from pathlib import Path
from typing import Any

from fastapi import HTTPException, UploadFile, status

from backend.config import get_settings
from backend.services import session_store, usage
from backend.services.dashboard_vision import DashboardVisionService
from backend.services.log_context import clear_context, set_context
from backend.services.synth import build_vitrina, inject_session_data_into_spec

logger = logging.getLogger("data_agent.vision_jobs")

_ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

_STAGE_LABELS: dict[str, str] = {
    "queued": "В очереди",
    "running": "Анализ изображения",
    "ocr": "OCR — распознавание текста",
    "ocr_done": "OCR — распознавание текста",
    "detail": "Детальный анализ элементов",
    "detail_done": "Детальный анализ элементов",
    "tables": "Извлечение таблиц",
    "tables_done": "Извлечение таблиц",
    "normalize": "Нормализация данных",
    "normalize_done": "Нормализация данных",
    "vitrina": "Генерация витрины данных",
    "done": "Готово",
    "failed": "Ошибка",
}

_queue: asyncio.Queue[str] | None = None
_jobs: dict[str, dict[str, Any]] = {}
_worker_tasks: list[asyncio.Task] = []


def stage_label(stage: str) -> str:
    return _STAGE_LABELS.get(stage, stage)


def _assert_non_empty_vitrina(spec: dict, vitrina: dict) -> None:
    widget_count = len(vitrina.get("widget_meta") or {})
    kpi_count = len(vitrina.get("FactKPIs") or [])
    if widget_count + kpi_count == 0:
        diagnostics = spec.get("stage_diagnostics") if isinstance(spec, dict) else None
        raise RuntimeError(f"Vision analysis returned 0 widgets. diagnostics={diagnostics}")


def _summary(spec: dict, vitrina: dict) -> dict[str, int]:
    return {
        "charts_detected": len(spec.get("charts") or []),
        "kpis_detected": len(spec.get("kpis") or []),
        "fact_rows": len(vitrina["FactDashboard"]),
        "kpi_rows": len(vitrina["FactKPIs"]),
        "widgets": len(vitrina["widget_meta"]) + len(vitrina["FactKPIs"]),
    }


def _result_from_spec(spec: dict) -> dict[str, Any]:
    vitrina = build_vitrina(spec)
    _assert_non_empty_vitrina(spec, vitrina)
    return {"spec": spec, "vitrina": vitrina, "summary": _summary(spec, vitrina)}


def _validate_suffix(filename: str | None) -> str:
    suffix = Path(filename or "upload.png").suffix.lower()
    if suffix not in _ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{suffix}'. Allowed: {sorted(_ALLOWED_EXTENSIONS)}",
        )
    return suffix


async def save_upload_for_job(file: UploadFile, user_id: uuid.UUID) -> tuple[Path, str]:
    suffix = _validate_suffix(file.filename)
    file_bytes = await file.read()
    usage.enforce_upload_size(user_id, len(file_bytes))
    tmp_dir = session_store.user_upload_root(user_id) / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False, dir=tmp_dir) as tmp:
        tmp.write(file_bytes)
        return Path(tmp.name), suffix


def start_workers() -> None:
    global _queue
    if _queue is not None:
        return
    _queue = asyncio.Queue()
    for index in range(max(1, get_settings().vision_worker_count)):
        _worker_tasks.append(asyncio.create_task(_worker(index + 1)))
    logger.info("Vision queue started workers=%s", len(_worker_tasks))


async def stop_workers() -> None:
    for task in _worker_tasks:
        task.cancel()
    if _worker_tasks:
        await asyncio.gather(*_worker_tasks, return_exceptions=True)
    _worker_tasks.clear()


def submit(path: Path, *, user_id: uuid.UUID, filename: str | None = None, session_id: str | None = None) -> str:
    if _queue is None:
        raise RuntimeError("Vision queue is not started")
    job_id = str(uuid.uuid4())
    _jobs[job_id] = {
        "job_id": job_id,
        "user_id": str(user_id),
        "filename": filename,
        "path": str(path),
        "session_id": session_id,
        "status": "queued",
        "stage": "queued",
        "pct": 0,
        "label": stage_label("queued"),
        "result": None,
        "error": None,
    }
    _queue.put_nowait(job_id)
    return job_id


def get_job(job_id: str, user_id: uuid.UUID) -> dict[str, Any]:
    job = _jobs.get(job_id)
    if not job or job.get("user_id") != str(user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vision job not found")
    return {
        key: value
        for key, value in job.items()
        if key not in {"path", "user_id"}
    }


async def analyze_direct(path: Path, *, on_progress: Any | None = None) -> dict[str, Any]:
    spec = await asyncio.to_thread(DashboardVisionService.analyze_dashboard, path, on_progress=on_progress)
    return _result_from_spec(spec)


async def analyze_with_session_data(
    path: Path,
    session_id: str,
    user_id: uuid.UUID,
    *,
    on_progress: Any | None = None,
) -> dict[str, Any]:
    """OCR the image, then inject real data from the session into the spec before building vitrina."""
    spec = await asyncio.to_thread(DashboardVisionService.analyze_dashboard, path, on_progress=on_progress)
    try:
        conn = session_store.get_conn(session_id, user_id)
        tables = session_store.list_tables(session_id, user_id)
        if tables:
            spec = inject_session_data_into_spec(spec, conn, tables)
    except Exception as exc:
        logger.warning("Session data injection skipped session=%s: %s", session_id, exc)
    return _result_from_spec(spec)


async def _worker(worker_id: int) -> None:
    assert _queue is not None
    while True:
        job_id = await _queue.get()
        job = _jobs.get(job_id)
        if not job:
            _queue.task_done()
            continue
        path = Path(str(job["path"]))
        loop = asyncio.get_running_loop()

        def update_progress(stage: str, pct: int) -> None:
            def apply() -> None:
                current = _jobs.get(job_id)
                if not current:
                    return
                current["stage"] = stage
                current["pct"] = pct
                current["label"] = stage_label(stage)

            loop.call_soon_threadsafe(apply)

        try:
            job_session_id = job.get("session_id")
            job_user_id = uuid.UUID(str(job.get("user_id")))
            set_context(user_id=str(job.get("user_id") or "-"), session_id=job_session_id or "-")
            logger.info("Vision job started worker=%s job=%s session=%s", worker_id, job_id, job_session_id or "-")
            job.update({"status": "running", "stage": "running", "pct": 1, "label": stage_label("running")})
            spec = await asyncio.to_thread(DashboardVisionService.analyze_dashboard, path, on_progress=update_progress)
            job.update({"stage": "vitrina", "pct": 92, "label": stage_label("vitrina")})
            if job_session_id:
                try:
                    conn = session_store.get_conn(job_session_id, job_user_id)
                    tables = session_store.list_tables(job_session_id, job_user_id)
                    if tables:
                        spec = inject_session_data_into_spec(spec, conn, tables)
                except Exception as exc:
                    logger.warning("Session data injection skipped job=%s: %s", job_id, exc)
            result = _result_from_spec(spec)
            job.update({"status": "done", "stage": "done", "pct": 100, "label": stage_label("done"), "result": result})
            logger.info("Vision job done worker=%s job=%s", worker_id, job_id)
        except Exception as exc:
            logger.exception("Vision job failed worker=%s job=%s", worker_id, job_id)
            job.update({"status": "failed", "stage": "failed", "pct": job.get("pct") or 0, "label": stage_label("failed"), "error": str(exc)})
        finally:
            path.unlink(missing_ok=True)
            clear_context()
            _queue.task_done()
