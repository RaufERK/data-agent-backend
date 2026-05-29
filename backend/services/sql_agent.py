"""LLM → SQL → DuckDB pipeline."""
from __future__ import annotations
import re
import json
import logging
from difflib import get_close_matches
import duckdb
from backend.services.cloudru_client import chat_complete
from backend.services.semantic_manifest import build_semantic_manifest, format_semantic_context

logger = logging.getLogger("data_agent.sql_agent")


def _build_schema_str(conn: duckdb.DuckDBPyConnection) -> str:
    try:
        return format_semantic_context(build_semantic_manifest(conn))
    except Exception:
        logger.exception("Semantic manifest build failed, falling back to raw schema")

    tables = [t for (t,) in conn.execute("SHOW TABLES").fetchall()]
    parts = []
    for table in tables:
        desc = conn.execute(f'DESCRIBE "{table}"').fetchdf()
        cols = ", ".join(f'"{r["column_name"]}" {r["column_type"]}' for _, r in desc.iterrows())
        samples = []
        for _, row in desc.iterrows():
            if any(t in row["column_type"].upper() for t in ("VARCHAR", "TEXT")):
                col = row["column_name"]
                try:
                    vals = conn.execute(
                        f'SELECT DISTINCT "{col}" FROM "{table}" WHERE "{col}" IS NOT NULL LIMIT 3'
                    ).fetchdf()[col].tolist()
                    samples.append(f'  -- "{col}": {vals}')
                except Exception:
                    pass
        sample_str = "\n" + "\n".join(samples) if samples else ""
        parts.append(f'Table "{table}":\n  {cols}{sample_str}')
    return "\n\n".join(parts)


def _identifier_catalog(manifest: dict) -> dict[str, set[str]]:
    tables = {str(table["name"]) for table in manifest.get("tables", [])}
    columns: set[str] = set()
    by_table: dict[str, set[str]] = {}
    for table in manifest.get("tables", []):
        table_name = str(table["name"])
        table_cols = {str(col["name"]) for col in table.get("columns", [])}
        by_table[table_name] = table_cols
        columns.update(table_cols)
    return {"tables": tables, "columns": columns, "by_table": by_table}


def _diagnose_sql_error(error: Exception, manifest: dict) -> str:
    catalog = _identifier_catalog(manifest)
    table_names = ", ".join(sorted(catalog["tables"])[:12]) or "нет таблиц"
    column_names = ", ".join(sorted(catalog["columns"])[:40]) or "нет колонок"
    return (
        f"{type(error).__name__}: {error}\n"
        f"Доступные таблицы: {table_names}\n"
        f"Доступные колонки: {column_names}"
    )


def _closest_identifier(value: str, candidates: set[str]) -> str | None:
    if value in candidates:
        return value
    lowered = {candidate.lower(): candidate for candidate in candidates}
    if value.lower() in lowered:
        return lowered[value.lower()]
    matches = get_close_matches(value.lower(), list(lowered.keys()), n=1, cutoff=0.72)
    return lowered[matches[0]] if matches else None


def _repair_sql_identifiers(sql: str, manifest: dict) -> str | None:
    catalog = _identifier_catalog(manifest)
    candidates = catalog["tables"] | catalog["columns"]
    changed = False

    def repl(match: re.Match[str]) -> str:
        nonlocal changed
        ident = match.group(1).replace('""', '"')
        if ident in candidates:
            return match.group(0)
        replacement = _closest_identifier(ident, candidates)
        if not replacement or replacement == ident:
            return match.group(0)
        changed = True
        return '"' + replacement.replace('"', '""') + '"'

    repaired = re.sub(r'"([^"]+(?:""[^"]*)*)"', repl, sql)
    return repaired if changed and repaired != sql else None


def _dry_run_sql(conn: duckdb.DuckDBPyConnection, sql: str) -> None:
    conn.execute(f"EXPLAIN {sql}")


_SYSTEM_PROMPT = """Ты аналитик данных. Пользователь загрузил файлы в DuckDB и задаёт вопросы.

Схема таблиц:
{schema}

Сгенерируй SQL-запрос для ответа на вопрос пользователя.
Верни ТОЛЬКО SQL без объяснений, без markdown, без ```sql```.
Используй двойные кавычки для таблиц и колонок.
Добавляй LIMIT 1000 если не указано иное.
Не выполняй INSERT/UPDATE/DELETE/DROP/ALTER/CREATE. Только чтение данных.
"""

_ANSWER_PROMPT = """Пользователь спросил: {question}

Результат SQL-запроса ({row_count} строк):
{preview}

Дай краткий ответ на русском языке (1-2 предложения). Используй конкретные числа из результата. Не упоминай SQL."""


def _extract_sql(raw: str) -> str:
    """Extract SQL from LLM response regardless of format."""
    raw = raw.strip()

    # Try JSON format first
    try:
        cleaned = re.sub(r"^```(?:json)?", "", raw, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict) and parsed.get("sql"):
            return parsed["sql"].strip().rstrip(";")
    except (json.JSONDecodeError, AttributeError):
        pass

    # Strip markdown code blocks
    raw = re.sub(r"```(?:sql)?", "", raw, flags=re.IGNORECASE).strip()
    raw = re.sub(r"```$", "", raw).strip()

    # Find SELECT statement
    sql_match = re.search(r"((?:WITH|SELECT|SHOW|DESCRIBE)\b.*)", raw, re.IGNORECASE | re.DOTALL)
    if sql_match:
        sql = sql_match.group(1).strip().rstrip(";")
        # Cut off any trailing non-SQL text after last semicolon or newline+word
        lines = sql.splitlines()
        sql_lines = []
        for line in lines:
            if re.match(r"^\s*(SELECT|WITH|FROM|WHERE|GROUP|ORDER|HAVING|LIMIT|JOIN|LEFT|RIGHT|INNER|OUTER|ON|AND|OR|UNION|INSERT|UPDATE|DELETE|SHOW|DESCRIBE|\(|\")", line, re.IGNORECASE):
                sql_lines.append(line)
            elif sql_lines:  # continuation line
                sql_lines.append(line)
        return "\n".join(sql_lines).strip().rstrip(";") if sql_lines else sql

    return ""


def _guard_readonly_sql(sql: str) -> str:
    cleaned = sql.strip().rstrip(";")
    if not re.match(r"^(SELECT|WITH|SHOW|DESCRIBE)\b", cleaned, re.IGNORECASE):
        raise ValueError("Разрешены только read-only SQL-запросы.")
    if re.search(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|COPY|ATTACH|DETACH|INSTALL|LOAD)\b", cleaned, re.IGNORECASE):
        raise ValueError("SQL содержит изменяющую операцию, она запрещена.")
    if re.match(r"^(SELECT|WITH)\b", cleaned, re.IGNORECASE) and not re.search(r"\bLIMIT\s+\d+\b", cleaned, re.IGNORECASE):
        cleaned = f"{cleaned}\nLIMIT 1000"
    return cleaned


def generate_sql(conn: duckdb.DuckDBPyConnection, user_question: str) -> str:
    try:
        manifest = build_semantic_manifest(conn)
        schema = format_semantic_context(manifest)
    except Exception:
        logger.exception("Semantic manifest build failed in SQL generation")
        manifest = {"tables": [], "relationships": [], "recommended_questions": [], "instructions": []}
        schema = _build_schema_str(conn)
    system = _SYSTEM_PROMPT.format(schema=schema)

    last_error = None
    for attempt in range(3):
        sql = ""
        try:
            prompt = user_question
            if last_error:
                prompt += (
                    f"\n\nПредыдущая попытка дала ошибку и диагностику:\n{last_error}\n"
                    "Исправь SQL, используя только доступные таблицы и колонки из схемы."
                )

            raw = chat_complete(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=600,
                temperature=0,
            )
            logger.info("LLM raw (attempt %d): %.300s", attempt + 1, raw)

            sql = _extract_sql(raw)
            if not sql:
                last_error = "не удалось извлечь SQL из ответа"
                continue

            sql = _guard_readonly_sql(sql)
            try:
                _dry_run_sql(conn, sql)
            except Exception as dry_error:
                repaired = _repair_sql_identifiers(sql, manifest)
                if repaired:
                    repaired = _guard_readonly_sql(repaired)
                    _dry_run_sql(conn, repaired)
                    logger.info("SQL repaired deterministically: %s -> %s", sql, repaired)
                    return repaired
                raise dry_error
            return sql

        except Exception as e:
            diagnosis = _diagnose_sql_error(e, manifest)
            last_error = f"{diagnosis}\nSQL: {sql}" if sql else diagnosis
            logger.warning("Attempt %d failed: %s", attempt + 1, e)

    raise ValueError(f"Не удалось сгенерировать SQL после 3 попыток. Последняя ошибка: {last_error}")


def _build_answer(conn: duckdb.DuckDBPyConnection, question: str, sql: str, columns: list, data: list, row_count: int) -> str:
    if row_count == 0:
        return "По запросу ничего не найдено."
    if len(columns) == 1 and row_count == 1:
        val = list(data[0].values())[0]
        return f"**{val}**"

    preview_rows = data[:8]
    preview_text = "\n".join(
        "  " + ", ".join(f"{k}: {v}" for k, v in row.items())
        for row in preview_rows
    )
    suffix = f" (показаны первые 8 из {row_count})" if row_count > 8 else ""

    try:
        answer = chat_complete(
            messages=[{"role": "user", "content": _ANSWER_PROMPT.format(
                question=question,
                row_count=str(row_count) + suffix,
                preview=preview_text,
            )}],
            max_tokens=200,
            temperature=0.3,
        )
        return answer.strip()
    except Exception:
        return f"Найдено {row_count} строк."


def _df_to_records(df) -> list:
    safe = df.astype(object).where(df.notna(), None)
    for col in safe.columns:
        safe[col] = safe[col].apply(lambda v: str(v) if hasattr(v, 'isoformat') else v)
    return safe.to_dict(orient="records")


def execute_sql(conn: duckdb.DuckDBPyConnection, sql: str) -> dict:
    try:
        df = conn.execute(sql).fetchdf()
    except Exception as e:
        raise ValueError(f"Ошибка выполнения SQL: {e}")
    df = df.head(1000)
    return {
        "columns": list(df.columns),
        "data": _df_to_records(df),
        "row_count": len(df),
    }


_SCHEMA_QUESTION_RE = re.compile(
    r"(какие|что|покажи|список|таблиц|данные|схем|колонк|структур|загружен|есть\s+тут|тут\s+есть|что\s+загружен)",
    re.IGNORECASE,
)


def _describe_schema(conn: duckdb.DuckDBPyConnection) -> dict:
    """Return a human-readable schema summary without calling LLM."""
    tables = [t for (t,) in conn.execute("SHOW TABLES").fetchall()]
    if not tables:
        return {
            "sql": "SHOW TABLES",
            "columns": ["table_name"],
            "data": [],
            "row_count": 0,
            "answer": "В сессии нет загруженных таблиц.",
        }
    lines = []
    for table in tables:
        desc = conn.execute(f'DESCRIBE "{table}"').fetchdf()
        col_list = ", ".join(desc["column_name"].tolist())
        try:
            cnt = conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
        except Exception:
            cnt = "?"
        lines.append(f"**{table}** — {cnt} строк, колонки: {col_list}")
    answer = "Загруженные таблицы:\n" + "\n".join(f"- {l}" for l in lines)
    return {
        "sql": "SHOW TABLES",
        "columns": ["table_name"],
        "data": [{"table_name": t} for t in tables],
        "row_count": len(tables),
        "answer": answer,
    }


def answer_with_sql(conn: duckdb.DuckDBPyConnection, question: str) -> dict:
    if _SCHEMA_QUESTION_RE.search(question) and len(question) < 120:
        try:
            return _describe_schema(conn)
        except Exception:
            pass
    sql = generate_sql(conn, question)
    result = execute_sql(conn, sql)
    result["sql"] = sql
    result["answer"] = _build_answer(conn, question, sql, result["columns"], result["data"], result["row_count"])
    return result
