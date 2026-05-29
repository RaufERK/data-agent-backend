"""Seed predefined users into the database.

Run once after DB initialisation:
    python -m backend.seed_users

Roles:
  admin       — unlimited (quota checks skipped in usage.consume)
  user        — default quotas from settings
  supertester — quotas halved relative to defaults (set via quota_override)

Idempotent: existing users are skipped, quotas are always re-applied.
"""
from __future__ import annotations

import math
import sys

from backend.services import app_db
from backend.services.auth import create_user, hash_password
from backend.config import get_settings

# ---------------------------------------------------------------------------
# User catalogue
# ---------------------------------------------------------------------------
# role values: "admin" | "user" | "supertester"
USERS: list[dict] = [
    {"email": "annasukhanova321@gmail.com",  "password": "admin",        "role": "admin"},
    {"email": "kozitskiy@sberanalytics.ru",  "password": "password2026", "role": "admin"},
    {"email": "megatester@sberanalytics.ru", "password": "megatester",   "role": "user"},
    {"email": "supertester_1@sberanalytics.ru",  "password": "supertester_1",  "role": "supertester"},
    {"email": "supertester_2@sberanalytics.ru",  "password": "supertester_2",  "role": "supertester"},
    {"email": "supertester_3@sberanalytics.ru",  "password": "supertester_3",  "role": "supertester"},
    {"email": "supertester_4@sberanalytics.ru",  "password": "supertester_4",  "role": "supertester"},
    {"email": "supertester_5@sberanalytics.ru",  "password": "supertester_5",  "role": "supertester"},
    {"email": "supertester_6@sberanalytics.ru",  "password": "supertester_6",  "role": "supertester"},
    {"email": "supertester_7@sberanalytics.ru",  "password": "supertester_7",  "role": "supertester"},
    {"email": "supertester_8@sberanalytics.ru",  "password": "supertester_8",  "role": "supertester"},
    {"email": "supertester_9@sberanalytics.ru",  "password": "supertester_9",  "role": "supertester"},
    {"email": "supertester_10@sberanalytics.ru", "password": "supertester_10", "role": "supertester"},
]

QUOTA_KEYS = ("upload_files", "assistant_questions", "dashboard_generations", "vision_analyses")


def _halved_quotas(settings) -> dict[str, int]:
    """Return quota limits halved (floor) relative to defaults."""
    defaults = {
        "upload_files":              settings.quota_upload_files_daily,
        "assistant_questions":       settings.quota_assistant_questions_daily,
        "dashboard_generations":     settings.quota_dashboard_generations_daily,
        "vision_analyses":           settings.quota_vision_analyses_daily,
    }
    return {k: max(1, math.floor(v / 2)) for k, v in defaults.items()}


def _hash_any(password: str) -> str:
    """Hash without minimum-length check (seed only)."""
    import os, hashlib, base64
    def b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 260_000)
    return f"pbkdf2_sha256$260000${b64url(salt)}${b64url(digest)}"


def _upsert_user(email: str, password: str, role: str) -> tuple[str, bool]:
    """Insert or update user. Returns (user_id_str, created)."""
    hashed = _hash_any(password)
    import uuid
    with app_db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE email = %s", (email,)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE users SET hashed_password = %s, role = %s WHERE email = %s",
                (hashed, role, email),
            )
            return str(row["id"]), False
        user_id = uuid.uuid4()
        conn.execute(
            "INSERT INTO users (id, email, hashed_password, role) VALUES (%s, %s, %s, %s)",
            (user_id, email, hashed, role),
        )
        return str(user_id), True


def _set_role(user_id_str: str, role: str) -> None:
    """Ensure the stored role matches the catalogue."""
    import uuid
    with app_db.connect() as conn:
        conn.execute(
            "UPDATE users SET role = %s WHERE id = %s",
            (role, uuid.UUID(user_id_str)),
        )


def run() -> None:
    settings = get_settings()
    halved = _halved_quotas(settings)

    print("Seeding users...")
    for spec in USERS:
        email = spec["email"].strip().lower()
        role  = spec["role"]

        user_id_str, created = _upsert_user(email, spec["password"], role)

        # Always sync role in case it changed in catalogue
        if not created:
            _set_role(user_id_str, role)

        import uuid
        uid = uuid.UUID(user_id_str)

        # Apply quota overrides for supertester (halved limits)
        if role == "supertester":
            for key, limit in halved.items():
                app_db.set_quota_override(uid, key, limit)

        action = "created" if created else "updated"
        print(f"  [{action}] {email}  role={role}", end="")
        if role == "supertester":
            print(f"  quotas={halved}", end="")
        elif role == "admin":
            print("  quotas=unlimited", end="")
        else:
            print("  quotas=default", end="")
        print()

    print("Done.")


if __name__ == "__main__":
    run()
