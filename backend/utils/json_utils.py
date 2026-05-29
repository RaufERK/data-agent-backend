"""JSON parsing utilities."""
from __future__ import annotations

from typing import Optional


def extract_json_object(raw: str) -> Optional[str]:
    """Extract the first complete JSON object from *raw*, or return None."""
    if not raw:
        return None
    start = raw.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for idx in range(start, len(raw)):
        ch = raw[idx]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return raw[start:idx + 1]
    return None


def safe_json_loads(raw: str) -> Optional[dict]:
    """Parse JSON from *raw*, trying full parse first then brace-extraction fallback."""
    import json
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
        return None
    except Exception:
        pass
    candidate = extract_json_object(raw)
    if not candidate:
        return None
    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        return None
    return None
