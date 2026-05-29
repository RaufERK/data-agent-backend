"""Minimal config adapter for dashboard_vision services (data_agent)."""
from __future__ import annotations
import os


class _VisionConfig:
    """Dict-like wrapper over env variables for dashboard_vision compatibility."""

    def get(self, key: str, default=None):
        return os.getenv(key, default)


_config = _VisionConfig()


def get_config():
    return _config
