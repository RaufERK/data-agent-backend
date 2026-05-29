"""Shared HTTP client for OpenAI-compatible proxy calls."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Optional

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore


def _verify_ssl() -> bool:
    return os.getenv("HTTP_CLIENT_VERIFY_SSL", "true").lower() not in {"0", "false", "no", "off"}


@lru_cache(maxsize=1)
def _make_client() -> Optional["httpx.Client"]:
    if httpx is None:
        return None
    return httpx.Client(verify=_verify_ssl(), timeout=180.0, follow_redirects=True)


def get_http_client() -> Optional["httpx.Client"]:
    return _make_client()
