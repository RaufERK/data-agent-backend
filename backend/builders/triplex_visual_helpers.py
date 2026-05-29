"""Visual/xparams helpers mixin for _TriplexExportBuilder."""
from __future__ import annotations

from .triplex_visual_xparams import _VisualXparamsMixin

import re
import hashlib
from typing import Any, Dict, List, Optional

from backend.utils.color_utils import normalize_hex_color as _normalize_hex_color


class _VisualHelpersMixin(_VisualXparamsMixin):
    """Data normalisation, axis inference, diagram/xparams building."""

    def _normalize_wide_format(
        self,
        chart_type: str,
        columns: List[str],
        rows: List[dict],
        x_field: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """Keep wide-format datasets wide for Navigator.

        AI-platform can render both wide data (category + several numeric
        series columns) and long data (category, series, value).  Navigator's
        diagram xparams is more reliable with one numeric field per series.
        Older code unpivoted wide data to long format and then emitted only one
        value field, which made series disappear or become "Нет данных" after
        import.  Long-format inputs are still pivoted by
        _normalize_long_format_to_wide(); already-wide inputs should pass
        through unchanged.
        """
        return None

    def _normalize_long_format_to_wide(
        self,
        chart_type: str,
        columns: List[str],
        rows: List[dict],
        x_field: Optional[str],
        y_field: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """Detect long-format (x, series_name, value) and pivot to wide-format.

        Navigator expects one numeric column per series; it cannot group rows
        by a string 'series' column automatically.  When we detect the classic
        long-format layout we pivot so each unique series value becomes its own
        numeric column.
        Returns None if data is not in recognisable long format.
        """
        if chart_type not in ("bar", "bar_horizontal", "line", "area", "combo", "table", "pivot_table"):
            return None
        if not x_field or not y_field or not columns:
            return None
        # Need exactly: x_field (string/number), a string "series" column, y_field (number)
        series_col = None
        for c in columns:
            if c == x_field or c == y_field:
                continue
            sample = [r for r in rows if isinstance(r, dict)][:20]
            if self._column_kind([r.get(c) for r in sample if c in r]) == "string":
                cl = c.lower().strip()
                if cl in {"series", "group", "category_group", "легенда", "серия", "группа", "источник"}:
                    series_col = c
                    break
        if not series_col:
            return None

        # Collect unique series values (preserve order)
        series_values: List[str] = list(dict.fromkeys(
            str(r.get(series_col, "")) for r in rows if isinstance(r, dict) and r.get(series_col) is not None
        ))
        if len(series_values) < 2:
            return None

        # Pivot: group by x_field, create one column per series value
        from collections import defaultdict
        wide: Dict[Any, Dict[str, Any]] = defaultdict(dict)
        x_order: List[Any] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            x_val = row.get(x_field)
            s_val = str(row.get(series_col, ""))
            y_val = row.get(y_field)
            if x_val not in wide:
                x_order.append(x_val)
            wide[x_val][s_val] = y_val

        wide_rows = []
        for x_val in x_order:
            r = {x_field: x_val}
            for s in series_values:
                r[s] = wide[x_val].get(s)
            wide_rows.append(r)

        wide_columns = [x_field] + series_values
        return {
            "columns": wide_columns,
            "rows": wide_rows,
            "x_field": x_field,
            "y_field": series_values[0],
        }

    def _series_value_count(
        self,
        columns: List[str],
        rows: List[dict],
        x_field: Optional[str],
        y_field: Optional[str],
    ) -> int:
        if not x_field or not y_field:
            return 0
        series_col = None
        sample = [r for r in rows if isinstance(r, dict)][:20]
        for col in columns:
            if col in {x_field, y_field}:
                continue
            if self._column_kind([r.get(col) for r in sample if col in r]) != "string":
                continue
            if col.lower().strip() in {"series", "group", "category_group", "легенда", "серия", "группа", "источник"}:
                series_col = col
                break
        if not series_col:
            return 0
        return len({
            str(row.get(series_col))
            for row in rows
            if isinstance(row, dict) and not self._is_empty_cell(row.get(series_col))
        })

    def _should_force_table_visual(
        self,
        chart_type: str,
        columns: List[str],
        rows: List[dict],
        x_field: Optional[str],
        y_field: Optional[str],
    ) -> bool:
        # country_map is not supported by Navigator at all — always use table.
        if chart_type == "country_map":
            return True

        series_count = self._series_value_count(columns, rows, x_field, y_field)

        # Wide-format bars with many series: Navigator renders them as a "plot"
        # (multi-series histogram) when there are multiple numeric Y columns.
        # Only fall back to table when there are too many series (>8) which makes
        # the chart unreadable, or when there is no x_field at all.
        if chart_type in {"bar", "bar_horizontal"}:
            sample = [r for r in rows if isinstance(r, dict)][:20]
            extra_numeric = [
                c for c in columns
                if c != x_field
                and self._column_kind([r.get(c) for r in sample if c in r]) == "number"
            ]
            if not x_field:
                return True
            if len(extra_numeric) > 8:
                return True

        # Large multi-line charts are fragile; preserve data as table.
        if chart_type in {"line", "area"} and series_count > 4:
            return True
        return False

    def _should_preserve_empty_table_columns(self, columns: List[str], rows: List[dict]) -> bool:
        if len(columns or []) < 4 or not rows:
            return False

        sample = [row for row in rows if isinstance(row, dict)][:30]
        if len(sample) < 3:
            return False

        total = 0
        empty = 0
        text = 0
        numeric = 0
        for row in sample:
            for col in columns:
                value = row.get(col)
                total += 1
                if self._is_empty_cell(value):
                    empty += 1
                elif self._column_kind([value]) == "number":
                    numeric += 1
                else:
                    text += 1

        if total == 0:
            return False

        sparse = empty / total >= 0.35
        label_heavy = text >= numeric
        return sparse or label_heavy

    def _compact_wide_table_text(self, columns: List[str], rows: List[dict]) -> List[dict]:
        if len(columns or []) < 5 or not rows:
            return rows

        compacted: List[dict] = []
        text_limit = 64
        for row in rows:
            if not isinstance(row, dict):
                compacted.append(row)
                continue
            next_row = dict(row)
            for col in columns[:2]:
                value = next_row.get(col)
                if not isinstance(value, str):
                    continue
                text = value.strip()
                if len(text) > text_limit:
                    next_row[col] = text[: text_limit - 1].rstrip() + "..."
            compacted.append(next_row)
        return compacted

    # Characters that Navigator's XML/Java identifier parser cannot handle
    _UNSAFE_COL_CHARS_RE = re.compile(r"[^\w\u0400-\u04FF]", re.UNICODE)

    @classmethod
    def _safe_col_name(cls, col: str) -> str:
        """Return a Navigator-safe column identifier.

        Strips or replaces characters that break Navigator's field parser
        (₽, commas, percent signs, parentheses, etc.) while keeping
        Cyrillic letters, Latin letters, digits, and underscores.
        Original names are preserved separately as ``sName`` display labels.

        Pure ASCII identifiers are lowercased so that Navigator's SQL generator
        (which emits unquoted column names) resolves them correctly in
        PostgreSQL. Cyrillic identifiers are kept as-is because PostgreSQL
        preserves their case when they are quoted in the CREATE VIEW statement
        and Navigator treats them as opaque tokens.
        """
        safe = cls._UNSAFE_COL_CHARS_RE.sub("_", col)
        # Collapse consecutive underscores and strip leading/trailing
        safe = re.sub(r"_+", "_", safe).strip("_")
        if safe and safe[0].isdigit():
            safe = f"col_{safe}"
        safe = safe or "col"
        # Lowercase pure-ASCII identifiers to avoid case-folding issues
        # when Navigator emits unquoted column references in generated SQL.
        if safe.isascii():
            safe = safe.lower()
        return cls._fit_pg_identifier(safe)

    @staticmethod
    def _fit_pg_identifier(identifier: str, suffix: str = "") -> str:
        """Keep an identifier unique before PostgreSQL's 63-byte truncation.

        Navigator imports sources into PostgreSQL, so quoted identifiers longer
        than 63 bytes are still truncated by the database.  Long Cyrillic labels
        can therefore collapse into the same physical column name unless we add
        a hash before trimming.
        """
        max_bytes = 63
        if suffix:
            base = identifier
            tag = suffix
        elif len(identifier.encode("utf-8")) <= max_bytes:
            return identifier
        else:
            base = identifier
            tag = "_" + hashlib.md5(identifier.encode("utf-8")).hexdigest()[:8]

        tag_bytes = len(tag.encode("utf-8"))
        available = max(1, max_bytes - tag_bytes)
        while len(base.encode("utf-8")) > available:
            base = base[:-1]
        return f"{base.rstrip('_')}{tag}"

    def _sanitize_columns(
        self,
        columns: List[str],
        rows: List[dict],
    ) -> tuple:
        """Sanitize column names and corresponding row keys.

        Returns ``(safe_columns, display_names, safe_rows)`` where:
        - ``safe_columns`` — list of Navigator-safe identifiers
        - ``display_names`` — dict mapping safe_name → original name
        - ``safe_rows`` — rows with keys renamed to safe identifiers
        """
        if not columns:
            return columns, {}, rows

        rename_map: Dict[str, str] = {}  # orig → safe
        display_names: Dict[str, str] = {}  # safe → orig
        safe_columns: List[str] = []
        seen_safe: Dict[str, int] = {}

        for orig in columns:
            safe = self._safe_col_name(orig)
            # Deduplicate safe names by appending a counter
            if safe in seen_safe:
                seen_safe[safe] += 1
                safe = self._fit_pg_identifier(safe, f"_{seen_safe[safe]}")
            else:
                seen_safe[safe] = 0
            rename_map[orig] = safe
            display_names[safe] = orig
            safe_columns.append(safe)

        # Check if any renaming was actually needed
        if all(rename_map[o] == o for o in columns):
            return columns, {c: c for c in columns}, rows

        safe_rows = [
            {rename_map.get(k, k): v for k, v in row.items()}
            if isinstance(row, dict) else row
            for row in rows
        ]
        return safe_columns, display_names, safe_rows

    def _infer_axes(self, chart_type: str, columns: List[str], rows: List[dict], meta: Dict[str, Any]) -> Dict[str, Optional[str]]:
        if not columns:
            return {"x": None, "y": None}
        x_field = meta.get("x_field") if isinstance(meta, dict) else None
        y_field = meta.get("y_field") if isinstance(meta, dict) else None

        sample_rows = [r for r in rows if isinstance(r, dict)][:20]
        col_kinds: Dict[str, str] = {}
        for col in columns:
            values = [r.get(col) for r in sample_rows if col in r]
            col_kinds[col] = self._column_kind(values)

        numeric_cols = [c for c in columns if col_kinds.get(c) == "number"]
        datetime_cols = [c for c in columns if col_kinds.get(c) == "datetime"]
        text_cols = [c for c in columns if col_kinds.get(c) == "string"]

        # Detect well-known column conventions (e.g. category/series/value).
        col_lower = {c: c.lower().strip() for c in columns}
        if not y_field:
            for c in columns:
                if col_lower[c] in self._VALUE_COL_NAMES and col_kinds.get(c) == "number":
                    y_field = c
                    break
        if not x_field:
            for c in columns:
                if c == y_field:
                    continue
                if col_lower[c] in self._CATEGORY_COL_NAMES:
                    x_field = c
                    break

        if not x_field:
            if chart_type in ("line", "area") and datetime_cols:
                x_field = datetime_cols[0]
            else:
                x_field = text_cols[0] if text_cols else columns[0]
        if not y_field:
            # Pick the first numeric column that is NOT the x_field.
            for c in numeric_cols:
                if c != x_field:
                    y_field = c
                    break
            if not y_field:
                y_field = numeric_cols[0] if numeric_cols else (columns[1] if len(columns) > 1 else columns[0])
        if x_field == y_field and len(columns) > 1:
            y_field = columns[1] if columns[0] == x_field else columns[0]
        return {"x": x_field, "y": y_field}

    def _collect_palette_colors(self, series_colors: List[tuple[str, str]]) -> None:
        for label, hex_code in series_colors:
            normalized = _normalize_hex_color(hex_code)
            if normalized and normalized not in self._palette_color_set:
                self._palette_color_set.add(normalized)
                self._palette_colors.append((label, normalized))

    @staticmethod
    def _dataset_stable(ds_name: str) -> str:
        """Compute Navigator src-schema view name from dataset name.

        PostgreSQL limits identifier length to 63 bytes.  When truncation
        is needed we embed a short hash of the *full* original name so
        that two datasets whose names differ only in the truncated part
        still receive distinct stable identifiers.
        """
        stable = re.sub(r"[^\w]", "_", ds_name, flags=re.UNICODE).lower()
        stable = re.sub(r"_+", "_", stable).strip("_")

        max_bytes = 63

        if len(stable.encode("utf-8")) <= max_bytes:
            return stable

        # 8-char hex hash of the full name guarantees uniqueness after
        # truncation.  "_" + 8 hex chars = 9 ASCII bytes.
        h = hashlib.md5(stable.encode("utf-8")).hexdigest()[:8]
        tag = f"_{h}"  # 9 bytes

        # Try to preserve the timestamp suffix produced by _with_suffix.
        last_us = stable.rfind("_")
        suffix = ""
        prefix = stable
        if last_us > 0 and stable[last_us + 1:].isdigit():
            suffix = stable[last_us:]       # e.g. "_2603111928309836"
            prefix = stable[:last_us]

        # Budget: prefix + tag + suffix <= 63 bytes
        suffix_bytes = len(suffix.encode("utf-8"))
        tag_bytes = len(tag.encode("utf-8"))
        available_for_prefix = max_bytes - suffix_bytes - tag_bytes

        if available_for_prefix < 4:
            # Extreme case – drop the timestamp suffix entirely.
            suffix = ""
            available_for_prefix = max_bytes - tag_bytes
            prefix = stable

        while len(prefix.encode("utf-8")) > available_for_prefix:
            prefix = prefix[:-1]
        prefix = prefix.rstrip("_")
        return f"{prefix}{tag}{suffix}"
