"""Role checks for FastAPI endpoints."""
from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, HTTPException, status

from backend.services.auth import CurrentUser, get_current_user


def require_role(*roles: str) -> Callable[[CurrentUser], CurrentUser]:
    allowed = set(roles)

    def dependency(current_user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if current_user.role not in allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Required role: {', '.join(sorted(allowed))}")
        return current_user

    return dependency


require_admin = require_role("admin")
