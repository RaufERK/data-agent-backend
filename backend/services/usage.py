"""Per-user quota and upload-size checks."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from fastapi import HTTPException, status

from backend.config import get_settings
from backend.services import app_db

QuotaKey = Literal["upload_files", "assistant_questions", "dashboard_generations", "vision_analyses"]


@dataclass(frozen=True)
class QuotaStatus:
    key: QuotaKey
    limit: int
    used: int
    remaining: int
    reset_at: str


def _default_limit(key: QuotaKey) -> int:
    settings = get_settings()
    return {
        "upload_files": settings.quota_upload_files_daily,
        "assistant_questions": settings.quota_assistant_questions_daily,
        "dashboard_generations": settings.quota_dashboard_generations_daily,
        "vision_analyses": settings.quota_vision_analyses_daily,
    }[key]


def daily_limit(user_id: uuid.UUID, key: QuotaKey) -> int:
    override = app_db.get_quota_override(user_id, key)
    return _default_limit(key) if override is None else override


def upload_limit_mb(user_id: uuid.UUID) -> int:
    return app_db.get_upload_limit_mb(user_id) or get_settings().max_upload_mb


def quota_status(user_id: uuid.UUID, key: QuotaKey) -> QuotaStatus:
    now = app_db.now_utc()
    since = now - timedelta(hours=24)
    limit = daily_limit(user_id, key)
    used = app_db.usage_count_since(user_id, key, since)
    return QuotaStatus(
        key=key,
        limit=limit,
        used=used,
        remaining=max(0, limit - used),
        reset_at=(now + timedelta(hours=24)).isoformat(),
    )


def all_quota_statuses(user_id: uuid.UUID) -> dict[str, dict[str, int | str]]:
    statuses = [quota_status(user_id, key) for key in ("upload_files", "assistant_questions", "dashboard_generations", "vision_analyses")]
    return {
        item.key: {
            "limit": item.limit,
            "used": item.used,
            "remaining": item.remaining,
            "reset_at": item.reset_at,
        }
        for item in statuses
    }


def consume(user_id: uuid.UUID, key: QuotaKey, *, role: str = "user", session_id: str | None = None, amount: int = 1) -> QuotaStatus:
    if role == "admin":
        app_db.record_usage(user_id, key, amount=amount, session_id=session_id)
        return quota_status(user_id, key)
    status_before = quota_status(user_id, key)
    if status_before.remaining < amount:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Daily quota exceeded for {key}: "
                f"{status_before.used}/{status_before.limit}. Resets within 24 hours."
            ),
        )
    app_db.record_usage(user_id, key, amount=amount, session_id=session_id)
    return quota_status(user_id, key)


def enforce_upload_size(user_id: uuid.UUID, byte_count: int) -> None:
    limit_mb = upload_limit_mb(user_id)
    limit_bytes = limit_mb * 1024 * 1024
    if byte_count > limit_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Upload is too large: max {limit_mb} MB for this user.",
        )
