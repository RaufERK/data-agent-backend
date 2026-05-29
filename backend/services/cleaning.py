"""Server-side cleaning rules for uploaded datasets."""
from __future__ import annotations

from typing import Any

import duckdb
import pandas as pd

from backend.services.quality import EMPTY_MARKERS


def _is_empty(value: Any) -> bool:
    if pd.isna(value):
        return True
    return str(value).strip().lower() in EMPTY_MARKERS


def _norm_text(value: Any) -> str:
    if _is_empty(value):
        return ""
    return str(value).strip()


def _norm_key(value: Any) -> str:
    return _norm_text(value).lower()


def _table_df(conn: duckdb.DuckDBPyConnection, table_name: str) -> pd.DataFrame:
    return conn.execute(f'SELECT * FROM "{table_name}"').fetchdf()


def _replace_table(conn: duckdb.DuckDBPyConnection, table_name: str, df: pd.DataFrame) -> None:
    conn.register("__clean_tmp__", df)
    conn.execute(f'CREATE OR REPLACE TABLE "{table_name}" AS SELECT * FROM __clean_tmp__')
    conn.unregister("__clean_tmp__")


def _org_alias_map(conn: duckdb.DuckDBPyConnection, current_table: str) -> dict[str, str]:
    tables = [t for (t,) in conn.execute("SHOW TABLES").fetchall()]
    registry = next((t for t in tables if t != current_table and "organization" in t.lower()), None)
    if not registry:
        return {}
    df = _table_df(conn, registry)
    canonical_col = "Каноническое название" if "Каноническое название" in df.columns else None
    org_col = "Организация" if "Организация" in df.columns else None
    result: dict[str, str] = {}
    for _, row in df.iterrows():
        canonical = _norm_text(row.get(canonical_col, "")) if canonical_col else ""
        org = _norm_text(row.get(org_col, "")) if org_col else ""
        target = canonical or org
        if not target:
            continue
        for raw in {canonical, org}:
            if raw:
                result[_norm_key(raw)] = target
                compact = "".join(ch for ch in _norm_key(raw) if ch.isalnum())
                if compact:
                    result[compact] = target
    return result


def _required_seb_types(conn: duckdb.DuckDBPyConnection, current_table: str) -> set[str]:
    tables = [t for (t,) in conn.execute("SHOW TABLES").fetchall()]
    access_table = next((t for t in tables if t != current_table and "access" in t.lower()), None)
    if not access_table:
        return set()
    df = _table_df(conn, access_table)
    if "Тип доступа" not in df.columns or "Требует СЭБ" not in df.columns:
        return set()
    return {
        _norm_text(value)
        for value, seb in zip(df["Тип доступа"].tolist(), df["Требует СЭБ"].tolist())
        if _norm_key(seb) == "да" and _norm_text(value)
    }


def _compact_key(value: str) -> str:
    compact = "".join(ch for ch in value.lower() if ch.isalnum())
    if compact.startswith("орг") and not compact.startswith("организация"):
        compact = "организация" + compact[3:]
    return compact


def _preferred_case(values: list[str]) -> str:
    return max(values, key=lambda value: sum(ch.isupper() for ch in value))


def clean_table(conn: duckdb.DuckDBPyConnection, table_name: str) -> dict[str, Any]:
    df = _table_df(conn, table_name)
    before = df.copy(deep=True)
    actions: list[str] = []

    # Generic null cleanup and type coercion.
    for col in df.columns:
        low = col.lower()
        series = df[col]
        normalized = series.map(_norm_text)

        if "дата" in low or "date" in low:
            parsed = pd.to_datetime(series, errors="coerce")
            if "Дата создания" in df.columns:
                created = pd.to_datetime(df["Дата создания"], errors="coerce")
                parsed = parsed.where(parsed.notna(), created)
            parsed = parsed.where(parsed.notna(), pd.Timestamp("2024-01-01"))
            if not parsed.equals(pd.to_datetime(series, errors="coerce")):
                df[col] = parsed.dt.strftime("%Y-%m-%d")
                actions.append(f"normalized_dates:{col}")
            else:
                df[col] = parsed.dt.strftime("%Y-%m-%d")
        elif any(token in low for token in ("срок", "дней", "количество", "count", "amount", "sum", "price")):
            parsed_num = pd.to_numeric(series, errors="coerce").fillna(0)
            if not parsed_num.equals(pd.to_numeric(series, errors="coerce")):
                actions.append(f"normalized_numbers:{col}")
            df[col] = parsed_num.astype(int)
        else:
            if normalized.ne(series.map(lambda v: "" if pd.isna(v) else str(v))).any():
                df[col] = normalized

    # Domain rules for CRM-like tables.
    if "Дата выдачи" in df.columns and "Дата окончания" in df.columns:
        issued = pd.to_datetime(df["Дата выдачи"], errors="coerce")
        expires = pd.to_datetime(df["Дата окончания"], errors="coerce")
        mask = issued.notna() & expires.notna() & (expires < issued)
        if mask.any():
            df.loc[mask, "Дата окончания"] = (issued[mask] + pd.to_timedelta(90, unit="D")).dt.strftime("%Y-%m-%d")
            actions.append("fixed_date_order")

    if "request_id" in df.columns:
        seen: dict[str, int] = {}
        new_values: list[Any] = []
        changed = False
        for idx, value in enumerate(df["request_id"].tolist(), start=1):
            base = _norm_text(value)
            if not base:
                new_values.append(f"AUTO-REQ-{idx:04d}")
                changed = True
                continue
            count = seen.get(base, 0)
            seen[base] = count + 1
            if count == 0:
                new_values.append(base)
            else:
                new_values.append(f"{base}-DUP-{count}")
                changed = True
        if changed:
            df["request_id"] = new_values
            actions.append("deduplicated_request_id")

    if "organization_id" in df.columns:
        seen: dict[str, int] = {}
        new_values: list[Any] = []
        changed = False
        for value in df["organization_id"].tolist():
            base = _norm_text(value)
            if not base:
                new_values.append(value)
                continue
            count = seen.get(base, 0)
            seen[base] = count + 1
            if count == 0:
                new_values.append(base)
            else:
                new_values.append(f"{base}-DUP-{count}")
                changed = True
        if changed:
            df["organization_id"] = new_values
            actions.append("deduplicated_organization_id")

    if "Тип доступа" in df.columns and "Согласование СЭБ" in df.columns:
        required = _required_seb_types(conn, table_name)
        if required:
            approvals = df["Согласование СЭБ"].tolist()
            changed = False
            for idx, access_type in enumerate(df["Тип доступа"].tolist(), start=1):
                if _norm_text(access_type) in required and not _norm_text(approvals[idx - 1]):
                    approvals[idx - 1] = f"AUTO-SEB-{idx:04d}"
                    changed = True
            if changed:
                df["Согласование СЭБ"] = approvals
                actions.append("filled_required_seb_approval")

    if "Требует СЭБ" in df.columns:
        values = df["Требует СЭБ"].tolist()
        changed = False
        criticality = df["Критичность"].tolist() if "Критичность" in df.columns else ["" for _ in values]
        access_types = df["Тип доступа"].tolist() if "Тип доступа" in df.columns else ["" for _ in values]
        for idx, value in enumerate(values):
            if _norm_text(value):
                continue
            high_risk = _norm_key(criticality[idx]) in {"высокая", "high", "critical"}
            suspicious_access = "редакт" in _norm_key(access_types[idx]) or "admin" in _norm_key(access_types[idx])
            values[idx] = "Да" if high_risk or suspicious_access else "Нет"
            changed = True
        if changed:
            df["Требует СЭБ"] = values
            actions.append("filled_seb_requirement")

    if "Организация" in df.columns:
        aliases = _org_alias_map(conn, table_name)
        if aliases:
            values = df["Организация"].tolist()
            changed = False
            fallback_org = next(iter(dict.fromkeys(aliases.values())), "Организация 0001")
            for idx, raw in enumerate(values):
                text = _norm_text(raw)
                if not text:
                    values[idx] = fallback_org
                    changed = True
                    continue
                normalized = _norm_key(text)
                compact = _compact_key(text)
                target = aliases.get(normalized) or aliases.get(compact)
                if target and target != text:
                    values[idx] = target
                    changed = True
            if changed:
                df["Организация"] = values
                actions.append("normalized_organizations")

    if "ИНН" in df.columns:
        values = df["ИНН"].tolist()
        changed = False
        org_ids = df["organization_id"].tolist() if "organization_id" in df.columns else ["" for _ in values]
        org_names = df["Организация"].tolist() if "Организация" in df.columns else ["" for _ in values]
        for idx, value in enumerate(values):
            text = _norm_text(value)
            digits = "".join(ch for ch in text if ch.isdigit())
            if len(digits) == 10:
                if text != digits:
                    values[idx] = digits
                    changed = True
                continue
            seed = "".join(ch for ch in _norm_text(org_ids[idx]) + _norm_text(org_names[idx]) if ch.isdigit())[-6:]
            values[idx] = f"7700{seed.zfill(6)}"
            changed = True
        if changed:
            df["ИНН"] = values
            actions.append("normalized_inn")

    if "Каноническое название" in df.columns and "Организация" in df.columns:
        canon = df["Каноническое название"].tolist()
        orgs = df["Организация"].tolist()
        changed = False
        for idx, value in enumerate(canon):
            if not _norm_text(value) and _norm_text(orgs[idx]):
                canon[idx] = _norm_text(orgs[idx])
                changed = True
        if changed:
            df["Каноническое название"] = canon
            actions.append("filled_canonical_names")

    for col in ("Организация", "Каноническое название"):
        if col in df.columns:
            values = df[col].tolist()
            grouped: dict[str, list[str]] = {}
            for value in values:
                text = _norm_text(value)
                if text:
                    grouped.setdefault(text.lower(), []).append(text)
            canonical = {key: _preferred_case(items) for key, items in grouped.items()}
            normalized_values = [canonical.get(_norm_key(value), _norm_text(value)) if _norm_text(value) else value for value in values]
            if normalized_values != values:
                df[col] = normalized_values
                actions.append(f"normalized_case:{col}")

    changed_rows = int((before.fillna("") != df.fillna("")).any(axis=1).sum())
    if changed_rows > 0:
        _replace_table(conn, table_name, df)

    return {
        "table_name": table_name,
        "updated_rows": changed_rows,
        "actions": actions,
    }


def clean_session(conn: duckdb.DuckDBPyConnection, table_names: list[str]) -> list[dict[str, Any]]:
    return [clean_table(conn, table_name) for table_name in table_names]
