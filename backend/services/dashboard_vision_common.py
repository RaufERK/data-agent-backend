"""Shared helpers for dashboard vision analyzers."""
from __future__ import annotations

from .dashboard_vision_tables import _DashboardVisionTablesMixin

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .vision_xml_stub import _XmlPlanParser
from ..utils.chart_utils import normalize_chart_type as _normalize_chart_type

logger = logging.getLogger(__name__)


class DashboardVisionCommonMixin(_DashboardVisionTablesMixin):
    """Common infrastructure used by screenshot and drawing vision analyzers."""

    MAX_XML_BLUEPRINT_ITEMS = 12
    MAX_TABLE_ROWS = 30
    KEEP_EMPTY_TABLE_PLACEHOLDERS = False
    USE_XML_CHART_POSITION = True
    TABLE_MERGE_MATCH_ONCE = False
    TABLE_MERGE_STRICT_NON_TABLE_SCORE: Optional[float] = None
    TABLE_MERGE_COPY_BLOCK_ID = False

    @staticmethod
    def _image_mime(path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix in {".jpg", ".jpeg"}:
            return "image/jpeg"
        if suffix in {".png"}:
            return "image/png"
        if suffix in {".gif"}:
            return "image/gif"
        if suffix in {".bmp"}:
            return "image/bmp"
        return "image/png"

    @staticmethod
    def _allowed_chart_types(
        upload_dir: Optional[Path],
        xml_path: Optional[Path] = None,
        xml_blueprint: Optional[List[Dict[str, Any]]] = None,
    ) -> tuple[List[str], Optional[str]]:
        base_types = {
            "bar",
            "bar_horizontal",
            "combo",
            "line",
            "area",
            "pie",
            "donut",
            "scatter",
            "table",
            "pivot_table",
            "big_number",
            "gauge",
            "sankey",
            "gantt",
            "sunburst",
            "treemap",
            "funnel",
            "radar",
            "country_map",
            "image",
            "candlestick",
        }
        core_types = {
            "bar",
            "bar_horizontal",
            "line",
            "area",
            "pie",
            "donut",
            "scatter",
            "table",
            "big_number",
            "candlestick",
        }
        xml_name = None
        xml_types: set[str] = set()
        if xml_blueprint:
            for chart in xml_blueprint:
                if not isinstance(chart, dict):
                    continue
                superset_type = _normalize_chart_type(chart.get("superset_type"))
                if superset_type:
                    xml_types.add(superset_type)
        if xml_types:
            allowed = sorted((xml_types | core_types) & base_types)
            if allowed:
                return allowed, xml_name
        return sorted(base_types), xml_name

    @staticmethod
    def _coerce_chart_type(raw_type: Any, allowed: set[str]) -> str:
        normalized = _normalize_chart_type(raw_type)
        if normalized in allowed:
            return normalized
        text = str(raw_type or "").strip().lower()
        if not text:
            return "table"
        if any(token in text for token in ("sunkey", "sankey", "санке")):
            return "sankey" if "sankey" in allowed else "table"
        if any(token in text for token in ("gant", "gantt", "гант", "timeline")):
            return "gantt" if "gantt" in allowed else "table"
        if any(token in text for token in ("combo", "mixed", "смеш")):
            return "combo" if "combo" in allowed else ("bar" if "bar" in allowed else "table")
        if any(token in text for token in ("donut", "doughnut", "кольц")):
            return "donut" if "donut" in allowed else ("pie" if "pie" in allowed else "table")
        if any(token in text for token in ("pie", "круг")):
            return "pie" if "pie" in allowed else "table"
        if any(token in text for token in ("area", "площад")):
            return "area" if "area" in allowed else ("line" if "line" in allowed else "table")
        if any(token in text for token in ("line", "trend", "линия")):
            return "line" if "line" in allowed else "table"
        if any(token in text for token in ("bar", "hist", "столб", "гист")):
            if "гориз" in text or "horizontal" in text:
                return "bar_horizontal" if "bar_horizontal" in allowed else ("bar" if "bar" in allowed else "table")
            return "bar" if "bar" in allowed else "table"
        if any(token in text for token in ("pivot", "свод")):
            return "pivot_table" if "pivot_table" in allowed else ("table" if "table" in allowed else "table")
        if any(token in text for token in ("table", "таблиц")):
            return "table"
        if any(token in text for token in ("map", "карта")):
            return "country_map" if "country_map" in allowed else "table"
        if any(token in text for token in ("scatter", "точк", "bubble", "quadrant")):
            return "scatter" if "scatter" in allowed else "table"
        if "radar" in text or "радар" in text:
            return "radar" if "radar" in allowed else "table"
        if "funnel" in text or "воронк" in text:
            return "funnel" if "funnel" in allowed else "table"
        if "sunburst" in text or "солн" in text:
            return "sunburst" if "sunburst" in allowed else "table"
        if "tree" in text or "treemap" in text:
            return "treemap" if "treemap" in allowed else "table"
        if any(token in text for token in ("race", "race_chart")):
            return "bar_horizontal" if "bar_horizontal" in allowed else ("bar" if "bar" in allowed else "table")
        if any(token in text for token in ("gauge", "progress", "прогресс", "thermometer", "pictorial")):
            return "big_number" if "big_number" in allowed else "table"
        if any(token in text for token in ("candlestick", "свечн", "ohlc")):
            return "candlestick" if "candlestick" in allowed else ("bar" if "bar" in allowed else "table")
        if any(token in text for token in ("waterfall", "водопад", "факторн")):
            return "bar" if "bar" in allowed else "table"
        return "table"

    @staticmethod
    def _xml_candidates(upload_dir: Optional[Path], xml_path: Optional[Path]) -> List[Path]:
        candidates: List[Path] = []
        if xml_path and xml_path.exists() and xml_path.is_file():
            candidates.append(xml_path)
        if upload_dir and upload_dir.exists():
            latest = _XmlPlanParser.latest_plan(upload_dir)
            if latest:
                candidates.append(latest)
        unique: List[Path] = []
        seen: set[str] = set()
        for candidate in candidates:
            key = str(candidate.resolve()) if candidate.exists() else str(candidate)
            if key in seen:
                continue
            seen.add(key)
            unique.append(candidate)
        return unique

    @classmethod
    def _load_xml_blueprint(
        cls,
        upload_dir: Optional[Path],
        xml_path: Optional[Path],
    ) -> tuple[List[Dict[str, Any]], Optional[str]]:
        for candidate in cls._xml_candidates(upload_dir, xml_path):
            try:
                charts = _XmlPlanParser.parse_plan(candidate)
            except Exception:
                continue
            if charts:
                return charts, candidate.name
        return [], None

    @classmethod
    def _load_xml_by_screen(
        cls,
        upload_dir: Optional[Path],
        xml_path: Optional[Path],
    ) -> tuple[Dict[str, list], Optional[str]]:
        """Load XML blueprint grouped by screen_id."""
        for candidate in cls._xml_candidates(upload_dir, xml_path):
            try:
                by_screen = _XmlPlanParser.parse_plan_by_screen(candidate)
            except Exception:
                continue
            if by_screen:
                return by_screen, candidate.name
        return {}, None

    @staticmethod
    def _default_categories_for_chart(chart_type: str) -> List[str]:
        if chart_type in {"line", "area", "combo"}:
            return ["Янв", "Фев", "Мар", "Апр", "Май", "Июн"]
        if chart_type in {"bar", "bar_horizontal"}:
            return ["Категория A", "Категория B", "Категория C", "Категория D"]
        if chart_type in {"pie", "donut", "funnel", "treemap", "sunburst", "radar"}:
            return ["Сегмент 1", "Сегмент 2", "Сегмент 3", "Сегмент 4"]
        if chart_type == "country_map":
            return ["Москва", "Санкт-Петербург", "Казань", "Екатеринбург"]
        if chart_type == "gantt":
            return ["Этап 1", "Этап 2", "Этап 3", "Этап 4"]
        if chart_type == "sankey":
            return ["Источник", "Промежуточный", "Цель"]
        return ["Показатель 1", "Показатель 2", "Показатель 3"]

    @staticmethod
    def _grid_position(index: int) -> Dict[str, float]:
        cols = 2
        width = 0.46
        height = 0.28
        gap_x = 0.03
        gap_y = 0.04
        row = index // cols
        col = index % cols
        left = 0.03 + col * (width + gap_x)
        top = 0.24 + row * (height + gap_y)
        return {
            "left": min(0.95, round(left, 4)),
            "top": min(0.95, round(top, 4)),
            "width": round(width, 4),
            "height": round(height, 4),
        }

    @staticmethod
    def _title_tokens(text: Any) -> set[str]:
        source = str(text or "").lower()
        tokens = re.findall(r"[0-9a-zа-яё]+", source)
        return {token for token in tokens if len(token) > 1}

    @classmethod
    def _title_score(cls, left: Any, right: Any) -> float:
        left_tokens = cls._title_tokens(left)
        right_tokens = cls._title_tokens(right)
        if not left_tokens or not right_tokens:
            return 0.0
        intersection = len(left_tokens & right_tokens)
        union = len(left_tokens | right_tokens)
        if union == 0:
            return 0.0
        return intersection / union

    @staticmethod
    def _position_iou(left: Any, right: Any) -> float:
        if not isinstance(left, dict) or not isinstance(right, dict):
            return 0.0
        try:
            lx = float(left.get("left", 0.0))
            ly = float(left.get("top", 0.0))
            lw = float(left.get("width", 0.0))
            lh = float(left.get("height", 0.0))
            rx = float(right.get("left", 0.0))
            ry = float(right.get("top", 0.0))
            rw = float(right.get("width", 0.0))
            rh = float(right.get("height", 0.0))
        except (TypeError, ValueError):
            return 0.0
        if lw <= 0 or lh <= 0 or rw <= 0 or rh <= 0:
            return 0.0
        left_x2 = lx + lw
        left_y2 = ly + lh
        right_x2 = rx + rw
        right_y2 = ry + rh
        inter_w = max(0.0, min(left_x2, right_x2) - max(lx, rx))
        inter_h = max(0.0, min(left_y2, right_y2) - max(ly, ry))
        intersection = inter_w * inter_h
        if intersection <= 0:
            return 0.0
        left_area = lw * lh
        right_area = rw * rh
        union = left_area + right_area - intersection
        if union <= 0:
            return 0.0
        return intersection / union

    @classmethod
    def _normalize_position(cls, raw: Any, fallback_index: int) -> Dict[str, float]:
        if not isinstance(raw, dict):
            return cls._grid_position(fallback_index)
        base = cls._grid_position(fallback_index)
        position: Dict[str, float] = {}
        for key in ("left", "top", "width", "height"):
            value = raw.get(key)
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                numeric = float(base.get(key, 0.0))
            position[key] = max(0.0, min(1.0, numeric))
        return position

    @classmethod
    def _table_chart_from_payload(
        cls,
        raw_entry: Dict[str, Any],
        index: int,
        allowed_set: set[str],
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(raw_entry, dict):
            return None
        chart_type = cls._coerce_chart_type(
            raw_entry.get("chart_type") or raw_entry.get("type") or "table",
            allowed_set,
        )
        if chart_type not in {"table", "pivot_table"}:
            chart_type = "table"
        title = str(raw_entry.get("title") or raw_entry.get("name") or "").strip() or f"Таблица {index + 1}"

        columns_raw = raw_entry.get("columns") or raw_entry.get("headers") or []
        columns = [str(col).strip() for col in columns_raw if str(col).strip()] if isinstance(columns_raw, list) else []
        rows_raw = raw_entry.get("rows")
        rows: List[Dict[str, Any]] = []
        if isinstance(rows_raw, list):
            for row in rows_raw[: cls.MAX_TABLE_ROWS]:
                if isinstance(row, dict):
                    clean = {str(k): v for k, v in row.items() if str(k).strip()}
                    if clean:
                        rows.append(clean)
                    continue
                if isinstance(row, list):
                    if not columns:
                        columns = [f"col_{idx + 1}" for idx in range(len(row))]
                    as_dict: Dict[str, Any] = {}
                    for idx, value in enumerate(row):
                        if idx >= len(columns):
                            break
                        as_dict[columns[idx]] = value
                    if as_dict:
                        rows.append(as_dict)

        if not rows and isinstance(raw_entry.get("series"), list) and columns:
            for series in raw_entry.get("series")[: cls.MAX_TABLE_ROWS]:
                if not isinstance(series, dict):
                    continue
                data = series.get("data")
                if not isinstance(data, list):
                    continue
                as_dict: Dict[str, Any] = {}
                for idx, value in enumerate(data):
                    if idx >= len(columns):
                        break
                    as_dict[columns[idx]] = value
                if as_dict:
                    rows.append(as_dict)

        if rows and not columns:
            columns = list(rows[0].keys())
        position = cls._normalize_position(raw_entry.get("position"), index)
        keep_empty = bool(getattr(cls, "KEEP_EMPTY_TABLE_PLACEHOLDERS", False))
        if not rows and (not columns or not keep_empty):
            return None
        if not rows and columns:
            logger.info("Keeping table placeholder without rows: %s", title)

        first_column = columns[0] if columns else "name"
        series_payload: List[Dict[str, Any]] = []
        for idx, row in enumerate(rows[: cls.MAX_TABLE_ROWS]):
            row_data = [row.get(col) for col in columns] if columns else list(row.values())
            row_name = str(row.get(first_column) or f"row_{idx + 1}").strip()
            series_payload.append({"name": row_name, "data": row_data})

        result = {
            "title": title,
            "chart_type": chart_type,
            "x_axis": str(raw_entry.get("x_axis") or "").strip(),
            "y_axis": str(raw_entry.get("y_axis") or "").strip(),
            "categories": columns,
            "series": series_payload,
            "rows": rows,
            "table_hint": str(raw_entry.get("table_hint") or title).strip(),
            "position": position,
        }
        if not rows:
            result["_table_placeholder"] = True
        return result

    @classmethod
    def _fill_placeholder_data(cls, parsed: Dict[str, Any]) -> Dict[str, Any]:
        """Fill empty series/categories with placeholder values so charts can render."""
        import random
        random.seed(42)

        charts = parsed.get("charts")
        if not isinstance(charts, list):
            return parsed

        PALETTE = [
            "#6366F1", "#22D3EE", "#F59E0B", "#10B981", "#EF4444",
            "#8B5CF6", "#F97316", "#3B82F6", "#EC4899", "#14B8A6",
        ]

        for chart in charts:
            if not isinstance(chart, dict):
                continue
            chart_type = chart.get("chart_type", "table")
            if chart_type in ("table", "pivot_table", "image", "big_number"):
                continue

            categories = chart.get("categories") or []
            series = chart.get("series") or []
            legend_items = chart.get("legend_items") or []

            # For pie/donut/funnel: build categories+series from legend_items if missing
            if chart_type in ("pie", "donut", "funnel", "treemap", "sunburst", "radar"):
                if legend_items and not categories:
                    if isinstance(legend_items[0], dict):
                        categories = [li.get("label") or li.get("name") or str(li) for li in legend_items]
                    else:
                        categories = [str(li) for li in legend_items]
                    chart["categories"] = categories

                if not categories:
                    categories = cls._default_categories_for_chart(chart_type)
                    chart["categories"] = categories

                # Build series from categories if series is empty
                all_empty = not series or all(
                    not (s.get("data") or s.get("value")) for s in series if isinstance(s, dict)
                )
                if all_empty:
                    n = len(categories)
                    series = [
                        {
                            "name": cat,
                            "value": round(random.uniform(10, 100), 1),
                            "data": [round(random.uniform(10, 100), 1)],
                            "hex_code": PALETTE[i % len(PALETTE)],
                        }
                        for i, cat in enumerate(categories)
                    ]
                    chart["series"] = series
                else:
                    # Fill missing hex_codes
                    for i, s in enumerate(series):
                        if isinstance(s, dict) and not s.get("hex_code"):
                            s["hex_code"] = PALETTE[i % len(PALETTE)]
                continue

            # For line/bar/area/combo/scatter etc.
            if not categories:
                categories = cls._default_categories_for_chart(chart_type)
                chart["categories"] = categories
            num_cats = len(categories)

            all_empty = not series or all(
                not (isinstance(s.get("data"), list) and s["data"]) for s in series if isinstance(s, dict)
            )
            if not all_empty:
                # Just fill missing hex_codes
                for i, s in enumerate(series):
                    if isinstance(s, dict) and not s.get("hex_code"):
                        s["hex_code"] = PALETTE[i % len(PALETTE)]
                continue

            if chart_type == "scatter":
                if not series:
                    series = [{"name": "Данные", "data": [], "hex_code": PALETTE[0]}]
                    chart["series"] = series
                for s in series:
                    if isinstance(s, dict) and not s.get("data"):
                        s["data"] = [
                            {"x": random.randint(10, 90), "y": random.randint(10, 90)}
                            for _ in range(min(num_cats, 8) or 8)
                        ]
                continue

            if not series:
                series = [{"name": "Значение", "data": [], "hex_code": PALETTE[0]}]
                chart["series"] = series

            for i, s in enumerate(series):
                if not isinstance(s, dict):
                    continue
                if not s.get("hex_code"):
                    s["hex_code"] = PALETTE[i % len(PALETTE)]
                data = s.get("data")
                if isinstance(data, list) and len(data) > 0:
                    continue
                base_val = s.get("value")
                if base_val:
                    try:
                        base = float(base_val)
                        s["data"] = [round(base * random.uniform(0.3, 1.2), 1) for _ in range(num_cats)]
                    except (TypeError, ValueError):
                        s["data"] = [round(random.uniform(10, 100), 1) for _ in range(num_cats)]
                else:
                    s["data"] = [round(random.uniform(10, 100), 1) for _ in range(num_cats)]

        return parsed

