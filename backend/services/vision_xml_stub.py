"""Stub for _XmlPlanParser — not used in data_agent (no Triplex XML plans)."""
from __future__ import annotations
from pathlib import Path
from typing import Any, Dict, List, Optional


class _XmlPlanParser:
    """No-op stub. XML plan features disabled in data_agent."""

    @staticmethod
    def latest_plan(upload_dir: Path) -> Optional[Path]:
        return None

    @staticmethod
    def parse_plan(path: Path) -> List[Dict[str, Any]]:
        return []

    @staticmethod
    def parse_plan_by_screen(path: Path) -> Dict[str, List[Dict[str, Any]]]:
        return {}
