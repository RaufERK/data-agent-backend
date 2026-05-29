"""Authentication endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, EmailStr

from backend.config import get_settings
from backend.services import usage
from backend.services.auth import CurrentUser, authenticate_user, create_access_token, create_user, get_current_user

router = APIRouter(prefix="/auth", tags=["auth"])


class AuthRequest(BaseModel):
    email: EmailStr
    password: str


class UserResponse(BaseModel):
    id: str
    email: str
    role: str


class QuotasResponse(BaseModel):
    quotas: dict
    upload: dict


def _user_response(user: CurrentUser) -> dict:
    return {"id": str(user.id), "email": user.email, "role": user.role}


def _set_auth_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=token,
        max_age=settings.auth_jwt_ttl_minutes * 60,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite=settings.auth_cookie_samesite,
        path="/",
    )


@router.post("/register", response_model=UserResponse)
def register(body: AuthRequest, response: Response, _admin: CurrentUser = Depends(get_current_user)):
    if _admin.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only admins can register new users")
    try:
        user = create_user(body.email, body.password)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _user_response(user)


@router.post("/login", response_model=UserResponse)
def login(body: AuthRequest, response: Response):
    user = authenticate_user(body.email, body.password)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
    _set_auth_cookie(response, create_access_token(user))
    return _user_response(user)


@router.post("/logout")
def logout(response: Response):
    settings = get_settings()
    response.delete_cookie(settings.auth_cookie_name, path="/")
    return {"ok": True}


@router.get("/me", response_model=UserResponse)
def me(current_user: CurrentUser = Depends(get_current_user)):
    return _user_response(current_user)


@router.get("/me/quotas", response_model=QuotasResponse)
def me_quotas(current_user: CurrentUser = Depends(get_current_user)):
    return {
        "quotas": usage.all_quota_statuses(current_user.id),
        "upload": {"max_upload_mb": usage.upload_limit_mb(current_user.id)},
    }
