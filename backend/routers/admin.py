"""Admin endpoints for user quotas and upload limits."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from fastapi.responses import ORJSONResponse
from pydantic import BaseModel, Field

from backend.services import app_db, usage
from backend.config import get_settings
from backend.services.auth import CurrentUser
from backend.services.roles import require_admin

router = APIRouter(prefix="/admin", tags=["admin"])


class QuotaOverrideRequest(BaseModel):
    quota_key: usage.QuotaKey
    daily_limit: int = Field(ge=0)


class UploadLimitRequest(BaseModel):
    max_upload_mb: int = Field(gt=0)


class ModelSettingsRequest(BaseModel):
    cloudru_model: str | None = None
    gigachat_vision_model: str | None = None


@router.get("/users")
def list_users(_admin: CurrentUser = Depends(require_admin)):
    users = []
    for item in app_db.list_users():
        user_id = item["id"]
        users.append({
            "id": str(user_id),
            "email": item["email"],
            "role": item["role"],
            "created_at": item["created_at"],
            "quotas": usage.all_quota_statuses(user_id),
            "upload": {"max_upload_mb": usage.upload_limit_mb(user_id)},
        })
    return ORJSONResponse({"users": users})


@router.get("/sessions")
def list_all_sessions(_admin: CurrentUser = Depends(require_admin)):
    return ORJSONResponse({"sessions": app_db.list_sessions()})


@router.put("/users/{user_id}/quotas")
def set_user_quota(user_id: uuid.UUID, body: QuotaOverrideRequest, _admin: CurrentUser = Depends(require_admin)):
    app_db.set_quota_override(user_id, body.quota_key, body.daily_limit)
    return ORJSONResponse({"user_id": str(user_id), "quotas": usage.all_quota_statuses(user_id)})


@router.put("/users/{user_id}/upload-limit")
def set_user_upload_limit(user_id: uuid.UUID, body: UploadLimitRequest, _admin: CurrentUser = Depends(require_admin)):
    app_db.set_upload_limit_mb(user_id, body.max_upload_mb)
    return ORJSONResponse({"user_id": str(user_id), "upload": {"max_upload_mb": usage.upload_limit_mb(user_id)}})


@router.get("/model-settings")
def get_model_settings(_admin: CurrentUser = Depends(require_admin)):
    settings = get_settings()
    stored = app_db.list_model_settings()
    return {
        "cloudru_model": stored.get("cloudru_model") or settings.cloudru_model,
        "gigachat_vision_model": stored.get("gigachat_vision_model") or settings.gigachat_vision_model,
        "overrides": stored,
    }


@router.put("/model-settings")
def set_model_settings(body: ModelSettingsRequest, admin: CurrentUser = Depends(require_admin)):
    for key in ("cloudru_model", "gigachat_vision_model"):
        value = getattr(body, key)
        if value is not None and value.strip():
            app_db.set_model_setting(key, value.strip(), admin.id)
    return get_model_settings(admin)
