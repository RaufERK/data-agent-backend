"""Data quality checks and domain-specific validation."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

import duckdb
import pandas as pd


EMPTY_MARKERS = {"", "n/a", "(пусто)", "-"}


def _is_empty(value: Any) -> bool:
    if pd.isna(value):
        return True
    return str(value).strip().lower() in EMPTY_MARKERS


def _normalized_text(value: Any) -> str:
    if _is_empty(value):
        return ""
    return str(value).strip()


def _normalized_key(value: Any) -> str:
    return _normalized_text(value).lower()


def _date_like(name: str) -> bool:
    low = name.lower()
    return "дата" in low or "date" in low


def _numeric_semantic(name: str) -> bool:
    low = name.lower()
    return any(token in low for token in ("срок", "дней", "количество", "count", "amount", "sum", "price"))


def _identifier(name: str) -> bool:
    low = name.lower()
    return (
        low == "id"
        or low.endswith("_id")
        or low in {"request_id", "rule_id", "access_type_id", "organization_id", "department_id", "инн"}
        or "идентификатор" in low
        or low == "номер"
    )


def _generic_null_required(name: str) -> bool:
    low = name.lower()
    return low not in {"комментарий", "comment", "согласование сэб"}


def _issue(issue_type: str, rows: list[int], severity: str, pct_total: int) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": issue_type,
        "count": len(rows),
        "severity": severity,
        "rows": rows[:200],
    }
    if issue_type == "null":
        payload["pct"] = round((len(rows) / max(pct_total, 1)) * 100, 1)
    return payload


def _add_issue(columns_info: dict[str, dict[str, Any]], column: str, issue: dict[str, Any]) -> None:
    col = columns_info[column]
    col["issues"].append(issue)
    if issue["severity"] == "error":
        col["severity"] = "error"
    elif col["severity"] == "ok":
        col["severity"] = "warning"


def _registry_aliases(conn: duckdb.DuckDBPyConnection, current_table: str) -> set[str]:
    tables = [t for (t,) in conn.execute("SHOW TABLES").fetchall()]
    registry = next((t for t in tables if t != current_table and "organization" in t.lower()), None)
    if not registry:
        return set()
    df = conn.execute(f'SELECT * FROM "{registry}"').fetchdf()
    aliases: set[str] = set()
    for col in ("Каноническое название", "Организация"):
        if col in df.columns:
            aliases.update(_normalized_key(v) for v in df[col].tolist() if not _is_empty(v))
    return aliases


def _required_seb_types(conn: duckdb.DuckDBPyConnection, current_table: str) -> set[str]:
    tables = [t for (t,) in conn.execute("SHOW TABLES").fetchall()]
    access_table = next((t for t in tables if t != current_table and "access" in t.lower()), None)
    if not access_table:
        return set()
    df = conn.execute(f'SELECT * FROM "{access_table}"').fetchdf()
    if "Тип доступа" not in df.columns or "Требует СЭБ" not in df.columns:
        return set()
    mask = df["Требует СЭБ"].apply(lambda v: _normalized_key(v) == "да")
    return {_normalized_text(v) for v in df.loc[mask, "Тип доступа"].tolist() if not _is_empty(v)}


def run_quality_checks(conn: duckdb.DuckDBPyConnection, table_name: str) -> dict[str, Any]:
    df = conn.execute(f'SELECT * FROM "{table_name}"').fetchdf()
    total = len(df)
    if total == 0:
        return {"total_rows": 0, "columns": [], "summary": {"errors": 0, "warnings": 0}}

    desc = conn.execute(f'DESCRIBE "{table_name}"').fetchdf()
    columns_info: dict[str, dict[str, Any]] = {
        str(row["column_name"]): {
            "column": str(row["column_name"]),
            "type": str(row["column_type"]).upper(),
            "severity": "ok",
            "issues": [],
        }
        for _, row in desc.iterrows()
    }

    for col, col_info in columns_info.items():
        series = df[col]
        normalized = series.map(_normalized_text)
        lowered = normalized.str.lower()

        empty_rows = (normalized == "").loc[lambda s: s].index.tolist()
        if empty_rows and _generic_null_required(col):
            _add_issue(columns_info, col, _issue("null", [i + 1 for i in empty_rows], "warning", total))

        if _identifier(col):
            non_empty = lowered != ""
            dup_mask = non_empty & lowered.duplicated(keep=False)
            dup_rows = dup_mask.loc[dup_mask].index.tolist()
            if dup_rows:
                _add_issue(columns_info, col, _issue("duplicate", [i + 1 for i in dup_rows], "warning", total))

        if pd.api.types.is_string_dtype(series) or col_info["type"] in {"VARCHAR", "TEXT", "STRING", "CHAR"}:
            groups: dict[str, set[str]] = defaultdict(set)
            for value in normalized.tolist():
                if value:
                    groups[value.lower()].add(value)
            mismatch_keys = {key for key, values in groups.items() if len(values) > 1}
            mismatch_rows = [idx + 1 for idx, key in enumerate(lowered.tolist()) if key in mismatch_keys and key]
            if mismatch_rows:
                _add_issue(columns_info, col, _issue("case_mismatch", mismatch_rows, "error", total))

        if _date_like(col):
            parsed = pd.to_datetime(series, errors="coerce")
            invalid_rows = [
                idx + 1
                for idx, (value, parsed_value) in enumerate(zip(normalized.tolist(), parsed.tolist()))
                if value and pd.isna(parsed_value)
            ]
            if invalid_rows:
                _add_issue(columns_info, col, _issue("invalid_date", invalid_rows, "error", total))

        if _numeric_semantic(col):
            parsed_num = pd.to_numeric(series, errors="coerce")
            invalid_num_rows = [
                idx + 1
                for idx, (value, parsed_value) in enumerate(zip(normalized.tolist(), parsed_num.tolist()))
                if value and pd.isna(parsed_value)
            ]
            if invalid_num_rows:
                _add_issue(columns_info, col, _issue("non_numeric", invalid_num_rows, "warning", total))

    if "Дата выдачи" in df.columns and "Дата окончания" in df.columns:
        issued = pd.to_datetime(df["Дата выдачи"], errors="coerce")
        expires = pd.to_datetime(df["Дата окончания"], errors="coerce")
        bad_rows = [idx + 1 for idx, (a, b) in enumerate(zip(issued.tolist(), expires.tolist())) if pd.notna(a) and pd.notna(b) and b < a]
        if bad_rows:
            _add_issue(columns_info, "Дата окончания", _issue("date_order", bad_rows, "error", total))

    if "Тип доступа" in df.columns and "Согласование СЭБ" in df.columns:
        required_types = _required_seb_types(conn, table_name)
        if required_types:
            access_values = df["Тип доступа"].map(_normalized_text)
            approval_values = df["Согласование СЭБ"].map(_normalized_text)
            bad_rows = [
                idx + 1
                for idx, (access_type, approval) in enumerate(zip(access_values.tolist(), approval_values.tolist()))
                if access_type in required_types and not approval
            ]
            if bad_rows:
                _add_issue(columns_info, "Согласование СЭБ", _issue("missing_required_approval", bad_rows, "error", total))

    if "Организация" in df.columns:
        aliases = _registry_aliases(conn, table_name)
        if aliases:
            org_values = df["Организация"].map(_normalized_text)
            bad_rows = [
                idx + 1
                for idx, value in enumerate(org_values.tolist())
                if value and _normalized_key(value) not in aliases
            ]
            if bad_rows:
                _add_issue(columns_info, "Организация", _issue("reference_mismatch", bad_rows, "warning", total))

    columns = list(columns_info.values())
    errors = sum(1 for c in columns if c["severity"] == "error")
    warnings = sum(1 for c in columns if c["severity"] == "warning")
    return {
        "total_rows": total,
        "columns": columns,
        "summary": {"errors": errors, "warnings": warnings},
    }
