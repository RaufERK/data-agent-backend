"""Authentication helpers: password hashing, signed JWTs, and FastAPI dependency."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, Request, status

from backend.config import get_settings
from backend.services import app_db


@dataclass(frozen=True)
class CurrentUser:
    id: uuid.UUID
    email: str
    role: str


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode((data + padding).encode("ascii"))


def hash_password(password: str) -> str:
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters")
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 260_000)
    return f"pbkdf2_sha256$260000${_b64url(salt)}${_b64url(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations_raw, salt_raw, digest_raw = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        iterations = int(iterations_raw)
        salt = _b64url_decode(salt_raw)
        expected = _b64url_decode(digest_raw)
    except Exception:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def create_user(email: str, password: str, role: str = "user") -> CurrentUser:
    normalized_email = email.strip().lower()
    if not normalized_email or "@" not in normalized_email:
        raise ValueError("Valid email is required")
    hashed = hash_password(password)
    user_id = uuid.uuid4()
    try:
        with app_db.connect() as conn:
            conn.execute(
                "INSERT INTO users (id, email, hashed_password, role) VALUES (%s, %s, %s, %s)",
                (user_id, normalized_email, hashed, role),
            )
    except Exception as exc:
        if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
            raise ValueError("User with this email already exists") from exc
        raise
    return CurrentUser(id=user_id, email=normalized_email, role=role)


def authenticate_user(email: str, password: str) -> CurrentUser | None:
    normalized_email = email.strip().lower()
    with app_db.connect() as conn:
        row = conn.execute(
            "SELECT id, email, hashed_password, role FROM users WHERE email = %s",
            (normalized_email,),
        ).fetchone()
    if not row or not verify_password(password, str(row["hashed_password"])):
        return None
    return CurrentUser(id=row["id"], email=str(row["email"]), role=str(row["role"]))


def _sign(message: bytes) -> str:
    secret = get_settings().auth_jwt_secret.encode("utf-8")
    return _b64url(hmac.new(secret, message, hashlib.sha256).digest())


def create_access_token(user: CurrentUser) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": str(user.id),
        "email": user.email,
        "role": user.role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.auth_jwt_ttl_minutes)).timestamp()),
    }
    signing_input = ".".join([
        _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8")),
        _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
    ]).encode("ascii")
    return f"{signing_input.decode('ascii')}.{_sign(signing_input)}"


def decode_access_token(token: str) -> dict[str, Any]:
    try:
        header_raw, payload_raw, signature = token.split(".", 2)
        signing_input = f"{header_raw}.{payload_raw}".encode("ascii")
        if not hmac.compare_digest(signature, _sign(signing_input)):
            raise ValueError("Bad signature")
        payload = json.loads(_b64url_decode(payload_raw))
        if int(payload.get("exp") or 0) < int(datetime.now(timezone.utc).timestamp()):
            raise ValueError("Token expired")
        return payload
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication") from exc


def get_current_user(request: Request) -> CurrentUser:
    settings = get_settings()
    token = request.cookies.get(settings.auth_cookie_name)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    payload = decode_access_token(token)
    user_id = uuid.UUID(str(payload["sub"]))
    with app_db.connect() as conn:
        row = conn.execute(
            "SELECT id, email, role FROM users WHERE id = %s",
            (user_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return CurrentUser(id=row["id"], email=str(row["email"]), role=str(row["role"]))
