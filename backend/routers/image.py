"""Image upload → async dashboard vision analysis → FactDashboard vitrina."""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import ORJSONResponse, StreamingResponse

from backend.services import usage, vision_jobs
from backend.services.auth import CurrentUser, get_current_user

router = APIRouter(prefix="/image", tags=["image"])
logger = logging.getLogger("data_agent.image")


def _sse_event(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


@router.post(
    "/analyze",
    summary="Upload a dashboard screenshot → vision analysis + synthetic витрина",
)
async def analyze_image(
    file: UploadFile = File(...),
    current_user: CurrentUser = Depends(get_current_user),
):
    tmp_path, _suffix = await vision_jobs.save_upload_for_job(file, current_user.id)
    usage.consume(current_user.id, "vision_analyses", role=current_user.role)
    try:
        logger.info("Vision direct analysis started filename=%s", file.filename)
        result = await vision_jobs.analyze_direct(tmp_path)
        logger.info("Vision direct analysis complete widgets=%s", result["summary"]["widgets"])
        return ORJSONResponse(result)
    finally:
        tmp_path.unlink(missing_ok=True)


@router.post(
    "/analyze-jobs",
    summary="Queue dashboard screenshot vision analysis",
)
async def create_image_analysis_job(
    file: UploadFile = File(...),
    current_user: CurrentUser = Depends(get_current_user),
):
    tmp_path, _suffix = await vision_jobs.save_upload_for_job(file, current_user.id)
    usage.consume(current_user.id, "vision_analyses", role=current_user.role)
    job_id = vision_jobs.submit(tmp_path, user_id=current_user.id, filename=file.filename)
    return ORJSONResponse({"job_id": job_id, "status": "queued", "pct": 0, "label": vision_jobs.stage_label("queued")})


@router.get(
    "/analyze-jobs/{job_id}",
    summary="Get queued vision analysis status",
)
async def get_image_analysis_job(job_id: str, current_user: CurrentUser = Depends(get_current_user)):
    return ORJSONResponse(vision_jobs.get_job(job_id, current_user.id))


@router.post(
    "/analyze-stream",
    summary="Upload a dashboard screenshot → SSE progress + final result",
)
async def analyze_image_stream(file: UploadFile = File(...), current_user: CurrentUser = Depends(get_current_user)):
    tmp_path, _suffix = await vision_jobs.save_upload_for_job(file, current_user.id)
    usage.consume(current_user.id, "vision_analyses", role=current_user.role)
    job_id = vision_jobs.submit(tmp_path, user_id=current_user.id, filename=file.filename)

    async def event_generator():
        last_stage = ""
        last_pct = -1
        while True:
            job = vision_jobs.get_job(job_id, current_user.id)
            stage = str(job.get("stage") or job.get("status") or "queued")
            pct = int(job.get("pct") or 0)
            if stage != last_stage or pct != last_pct:
                yield _sse_event({"stage": stage, "pct": pct, "label": job.get("label") or vision_jobs.stage_label(stage)})
                last_stage, last_pct = stage, pct

            if job.get("status") == "done":
                yield _sse_event({"stage": "done", "pct": 100, "label": "Готово", "result": job.get("result")})
                return
            if job.get("status") == "failed":
                yield _sse_event({"error": job.get("error") or "Vision analysis failed"})
                return
            await asyncio.sleep(1)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/analyze-with-data",
    summary="Queue dashboard image + session data analysis (returns job_id for polling)",
)
async def analyze_image_with_data(
    file: UploadFile = File(...),
    session_id: str = Query(..., description="Session ID that contains the uploaded data files"),
    current_user: CurrentUser = Depends(get_current_user),
):
    tmp_path, _suffix = await vision_jobs.save_upload_for_job(file, current_user.id)
    usage.consume(current_user.id, "vision_analyses", role=current_user.role)
    job_id = vision_jobs.submit(
        tmp_path,
        user_id=current_user.id,
        filename=file.filename,
        session_id=session_id,
    )
    return ORJSONResponse({"job_id": job_id, "status": "queued", "pct": 0, "label": vision_jobs.stage_label("queued")})
