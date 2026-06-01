"""OIDC authentication routes for FastAPI.

Flow:
  GET  /api/oidc/login      → redirect to OIDC provider
  GET  /api/oidc/callback   → exchange code, upsert user, set JWT cookie
  POST /api/oidc/logout     → clear cookie, redirect to OIDC end_session
  GET  /api/oidc/status     → current OIDC config (enabled/disabled)

Role mapping from OIDC groups claim:
  "admin"       → role=admin       (unlimited quotas)
  "supertester" → role=supertester (halved quotas)
  "megatester"  → role=user        (default quotas, alias kept for compat)
  <anything>    → role=user        (default quotas)

Groups with ":" are ignored (org-level metadata, not roles).
"""
from __future__ import annotations

import logging
import uuid

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from backend.config import get_settings
from backend.services import app_db
from backend.services.auth import create_access_token, CurrentUser

logger = logging.getLogger("data_agent.oidc")

router = APIRouter(prefix="/oidc", tags=["oidc"])

# ---------------------------------------------------------------------------
# OIDC group → app role mapping
# ---------------------------------------------------------------------------

_ROLE_PRIORITY = {"admin": 3, "supertester": 2, "user": 1}
_GROUP_TO_ROLE: dict[str, str] = {
    "admin": "admin",
    "supertester": "supertester",
    "megatester": "user",  # legacy alias
}


def _groups_to_role(groups: list[str]) -> str:
    """Pick the highest-priority role from OIDC groups list."""
    best = "user"
    for g in groups:
        if ":" in g:
            continue  # skip org metadata like dept:analytics
        role = _GROUP_TO_ROLE.get(g.lower())
        if role and _ROLE_PRIORITY.get(role, 0) > _ROLE_PRIORITY.get(best, 0):
            best = role
    return best


# ---------------------------------------------------------------------------
# OIDC metadata + token helpers (stateless, cached per process)
# ---------------------------------------------------------------------------

_oidc_meta_cache: dict | None = None


def _oidc_meta() -> dict:
    global _oidc_meta_cache
    if _oidc_meta_cache:
        return _oidc_meta_cache
    settings = get_settings()
    issuer = settings.oidc_issuer.rstrip("/")
    url = f"{issuer}/.well-known/openid-configuration"
    resp = httpx.get(url, timeout=10, verify=settings.oidc_verify_ssl)
    resp.raise_for_status()
    _oidc_meta_cache = resp.json()
    return _oidc_meta_cache


def _fetch_token(code: str, redirect_uri: str) -> dict:
    s = get_settings()
    resp = httpx.post(
        _oidc_meta()["token_endpoint"],
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": s.oidc_client,
            "client_secret": s.oidc_secret,
        },
        timeout=15,
        verify=s.oidc_verify_ssl,
    )
    resp.raise_for_status()
    return resp.json()


def _fetch_userinfo(access_token: str) -> dict:
    settings = get_settings()
    resp = httpx.get(
        _oidc_meta()["userinfo_endpoint"],
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
        verify=settings.oidc_verify_ssl,
    )
    resp.raise_for_status()
    return resp.json()


def _build_redirect_uri(request: Request) -> str:
    override = get_settings().oidc_redirect_uri
    if override:
        return override
    proto = request.headers.get("X-Forwarded-Proto", request.url.scheme)
    host = request.headers.get("X-Forwarded-Host", request.headers.get("host", "localhost"))
    return f"{proto}://{host}/api/oidc/callback"


# ---------------------------------------------------------------------------
# User upsert: create or update role in DB
# ---------------------------------------------------------------------------

def _upsert_oidc_user(email: str, role: str) -> CurrentUser:
    """Create user if not exists, or update role. Returns CurrentUser."""
    with app_db.connect() as conn:
        row = conn.execute(
            "SELECT id, role FROM users WHERE email = %s", (email,)
        ).fetchone()

        if row:
            existing_role = str(row["role"])
            user_id = row["id"]
            # Only upgrade role, never downgrade (admin stays admin)
            if _ROLE_PRIORITY.get(role, 0) > _ROLE_PRIORITY.get(existing_role, 0):
                conn.execute(
                    "UPDATE users SET role = %s WHERE id = %s", (role, user_id)
                )
                logger.info("OIDC: upgraded role %s → %s for %s", existing_role, role, email)
            else:
                role = existing_role  # keep current
        else:
            user_id = uuid.uuid4()
            # New OIDC user: no password (they use OIDC only)
            conn.execute(
                "INSERT INTO users (id, email, hashed_password, role) VALUES (%s, %s, %s, %s)",
                (user_id, email, "oidc:no-password", role),
            )
            logger.info("OIDC: created user %s role=%s", email, role)

    # Apply supertester quotas if needed
    if role == "supertester":
        _apply_supertester_quotas(user_id)

    return CurrentUser(id=user_id, email=email, role=role)


def _apply_supertester_quotas(user_id: uuid.UUID) -> None:
    import math
    s = get_settings()
    halved = {
        "upload_files":          max(1, math.floor(s.quota_upload_files_daily / 2)),
        "assistant_questions":   max(1, math.floor(s.quota_assistant_questions_daily / 2)),
        "dashboard_generations": max(1, math.floor(s.quota_dashboard_generations_daily / 2)),
        "vision_analyses":       max(1, math.floor(s.quota_vision_analyses_daily / 2)),
    }
    for key, limit in halved.items():
        app_db.set_quota_override(user_id, key, limit)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/status")
def oidc_status():
    s = get_settings()
    return {"enabled": bool(s.oidc_issuer and s.oidc_client and s.oidc_secret)}


@router.get("/login")
def oidc_login(request: Request):
    s = get_settings()
    if not s.oidc_issuer:
        return JSONResponse({"error": "OIDC not configured"}, status_code=501)

    import secrets
    state = secrets.token_urlsafe(16)
    redirect_uri = _build_redirect_uri(request)
    meta = _oidc_meta()

    params = (
        f"?response_type=code"
        f"&client_id={s.oidc_client}"
        f"&redirect_uri={redirect_uri}"
        f"&scope=openid+email+groups+offline_access"
        f"&state={state}"
    )
    return RedirectResponse(meta["authorization_endpoint"] + params)


@router.get("/callback")
def oidc_callback(request: Request, code: str = "", error: str = "", state: str = ""):
    if error:
        logger.error("OIDC error from provider: %s", error)
        return RedirectResponse("/login?error=oidc_error")

    if not code:
        return JSONResponse({"error": "missing code"}, status_code=400)

    try:
        redirect_uri = _build_redirect_uri(request)
        token_data = _fetch_token(code, redirect_uri)
        userinfo = _fetch_userinfo(token_data["access_token"])
    except Exception as exc:
        logger.error("OIDC callback failed: %s", exc)
        return RedirectResponse("/login?error=oidc_failed")

    email = userinfo.get("email") or userinfo.get("preferred_username")
    if not email:
        logger.error("OIDC userinfo missing email: %s", userinfo)
        return RedirectResponse("/login?error=no_email")

    groups = userinfo.get("groups", [])
    role = _groups_to_role(groups)
    logger.info("OIDC login: %s groups=%s → role=%s", email, groups, role)

    user = _upsert_oidc_user(email.strip().lower(), role)
    jwt_token = create_access_token(user)

    settings = get_settings()
    response = RedirectResponse("/", status_code=302)
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=jwt_token,
        max_age=settings.auth_jwt_ttl_minutes * 60,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite=settings.auth_cookie_samesite,
        path="/",
    )
    return response


@router.post("/logout")
def oidc_logout(request: Request):
    settings = get_settings()
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(settings.auth_cookie_name, path="/")

    # Try to redirect to OIDC end_session if configured
    try:
        end_session = _oidc_meta().get("end_session_endpoint")
        if end_session and settings.oidc_issuer:
            response = RedirectResponse(end_session, status_code=302)
            response.delete_cookie(settings.auth_cookie_name, path="/")
    except Exception:
        pass

    return response
