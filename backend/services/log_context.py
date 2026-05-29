"""JSON logging with request/user/session context."""
from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
user_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("user_id", default=None)
session_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("session_id", default=None)
method_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("method", default=None)
path_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("path", default=None)


def set_context(
    *,
    request_id: str | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
    method: str | None = None,
    path: str | None = None,
) -> None:
    if request_id is not None:
        request_id_var.set(request_id)
    if user_id is not None:
        user_id_var.set(user_id)
    if session_id is not None:
        session_id_var.set(session_id)
    if method is not None:
        method_var.set(method)
    if path is not None:
        path_var.set(path)


def clear_context() -> None:
    request_id_var.set(None)
    user_id_var.set(None)
    session_id_var.set(None)
    method_var.set(None)
    path_var.set(None)


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get() or "-"
        record.user_id = user_id_var.get() or "-"
        record.session_id = session_id_var.get() or "-"
        record.method = method_var.get() or "-"
        record.path = path_var.get() or "-"
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "user_id": getattr(record, "user_id", "-"),
            "session_id": getattr(record, "session_id", "-"),
            "request_id": getattr(record, "request_id", "-"),
            "method": getattr(record, "method", "-"),
            "path": getattr(record, "path", "-"),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(*, structured: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout)
    context_filter = ContextFilter()
    handler.addFilter(context_filter)
    if structured:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] user=%(user_id)s session=%(session_id)s %(message)s"
        ))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
