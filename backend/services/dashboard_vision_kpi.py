"""KPI conversion and deduplication helpers for dashboard chart typing."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ..utils.chart_utils import normalize_chart_type as _normalize_chart_type

_KPI_TITLE_RE = re.compile(
    r"(итог|всего|kpi|показател|выручк|доход|расход|прибыл|план|факт|конверс|ctr|cr|arpu|arppu|маu|dau|ltv|nps|roi|romi|cpc|cpm|клики|лиды|продаж|заказ|балл|скор|metric|остатк|счет|баланс|бюджет)",
    re.IGNORECASE,
)
# Title contains an embedded numeric value (drawn dashboards often put value in title)
_TITLE_HAS_NUMBER_RE = re.compile(r"\d[\d\s]{2,}", re.IGNORECASE)
_GAUGE_TITLE_RE = re.compile(
    r"(gauge|progress|thermometer|speedometer|radial|pictorial|спидометр|термометр|прогресс|шкала|индикатор)",
    re.IGNORECASE,
)
_KPI_GROUP_TITLE_RE = re.compile(
    # Matches titles that are section headers / group containers, NOT real KPI names.
    # Deliberately excludes short ambiguous words like "виды" that appear in real KPI names
    # (e.g. "Виды услуг" is a valid KPI; pruning it by word match is a false positive).
    r"(структур[аы]?|состав|характеристик|показател[ья]\s|профил[ьи]|сводк[аи]|обзор|итог[ио]|сравнен)",
    re.IGNORECASE,
)


class _ChartKpiMixin:
    @classmethod
    def _chart_to_kpi(cls, chart: Dict[str, Any], fallback_index: int) -> Optional[Dict[str, Any]]:
        title = cls._chart_title(chart)
        if not title:
            return None
        value = cls._extract_chart_value(chart)
        kpi_name = title
        # If title has embedded number (e.g. "Остатки денег на счетах 253 400"), split it out
        if value is None:
            m = _TITLE_HAS_NUMBER_RE.search(title)
            if m:
                raw_num = re.sub(r"\s+", "", m.group(0))
                try:
                    value = int(raw_num)
                    kpi_name = title[:m.start()].strip()
                except ValueError:
                    pass
        kpi = {
            "name": kpi_name,
            "value": value,
            "unit": chart.get("unit") or chart.get("value_unit") or "",
            "note": chart.get("note") or chart.get("notes") or "",
            "position": cls._normalize_position(chart.get("position"), fallback_index),
        }
        if chart.get("block_id"):
            kpi["block_id"] = chart.get("block_id")
        return kpi

    @classmethod
    def _is_kpi_candidate(cls, chart: Dict[str, Any]) -> bool:
        chart_type = _normalize_chart_type(chart.get("chart_type") or chart.get("type"))
        title = cls._chart_title(chart)
        widget_family = str(chart.get("widget_family") or "").lower()
        block_kind = cls._block_kind_from_type(chart.get("block_kind") or chart_type)
        position = chart.get("position") if isinstance(chart.get("position"), dict) else {}
        width = float(position.get("width", 0.0) or 0.0)
        height = float(position.get("height", 0.0) or 0.0)
        point_count = cls._chart_point_count(chart)
        categories = chart.get("categories") if isinstance(chart.get("categories"), list) else []
        if block_kind in {"gauge", "map", "legend", "text"}:
            return False
        if _GAUGE_TITLE_RE.search(title):
            return False
        if any(token in widget_family for token in ("progress", "gauge", "thermometer", "pictorial", "nav:70", "nav:92", "nav:119")):
            return False
        if bool(chart.get("is_kpi")):
            # Even if marked as KPI, if the title clearly indicates a chart type, prefer chart
            title_hint = cls._type_from_title(title)
            if title_hint and title_hint not in {"big_number", "big_number_total"}:
                return False
            return True
        if block_kind == "kpi":
            # If title strongly suggests a chart type (line, bar, pie etc.), it's a chart, not KPI
            title_hint = cls._type_from_title(title)
            if title_hint and title_hint not in {"big_number", "big_number_total"}:
                return False
            return True
        if chart_type == "big_number":
            return point_count <= 2 and len(categories) <= 1
        # Don't reclassify charts with specific types (bar, line, pie, etc.) as KPI
        # UNLESS the title has an embedded number — drawn dashboards often show "Title 12 345"
        if chart_type in {"bar", "line", "area", "pie", "donut", "scatter", "funnel",
                          "radar", "combo", "bar_horizontal", "country_map"}:
            # Allow promotion only if title contains an embedded large number and block is small
            if _TITLE_HAS_NUMBER_RE.search(title) and width < 0.55 and height <= 0.35:
                return True
            return False
        if point_count <= 1 and width < 0.4 and height <= 0.25 and _KPI_TITLE_RE.search(title):
            return True
        # Small block with a single value and no series data — likely KPI
        value = cls._extract_chart_value(chart)
        if value is not None and point_count <= 1 and len(categories) <= 1 and width < 0.4 and height <= 0.3:
            return True
        return False

    @classmethod
    def _dedupe_kpis(cls, kpis: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        # Same-title KPI cards often appear multiple times in one dashboard
        # (e.g. a total card and a related percent card).  Deduplicate by
        # title + coarse position, not by title alone.
        title_has_real_value: set[str] = set()
        for kpi in kpis:
            if not isinstance(kpi, dict):
                continue
            name = str(kpi.get("name") or kpi.get("title") or "").strip()
            if not name:
                continue
            if kpi.get("value") is not None and not kpi.get("_recovered_from_inventory"):
                title_has_real_value.add(re.sub(r"[^a-z0-9а-яё]+", "", name.lower()))

        best: dict[tuple, Dict[str, Any]] = {}
        for idx, kpi in enumerate(kpis):
            if not isinstance(kpi, dict):
                continue
            name = str(kpi.get("name") or kpi.get("title") or "").strip()
            if not name:
                continue
            key = re.sub(r"[^a-z0-9а-яё]+", "", name.lower())
            if (
                key in title_has_real_value
                and kpi.get("value") is None
                and kpi.get("_recovered_from_inventory")
            ):
                continue
            position = kpi.get("position") if isinstance(kpi.get("position"), dict) else {}
            left_bucket = round(float(position.get("left", 0.0) or 0.0) * 10)
            top_bucket = round(float(position.get("top", 0.0) or 0.0) * 10)
            has_position = isinstance(kpi.get("position"), dict)
            dedup_key = (key, left_bucket, top_bucket) if has_position else (key, -1, idx)
            existing = best.get(dedup_key)
            if existing is None:
                best[dedup_key] = kpi
                continue

            existing_val = existing.get("value")
            new_val = kpi.get("value")
            existing_score = 0
            new_score = 0
            if existing_val is not None:
                existing_score += 4
            if new_val is not None:
                new_score += 4
            if not existing.get("_recovered_from_inventory"):
                existing_score += 2
            if not kpi.get("_recovered_from_inventory"):
                new_score += 2
            if isinstance(existing.get("sparkline"), list) and len(existing.get("sparkline") or []) >= 2:
                existing_score += 1
            if isinstance(kpi.get("sparkline"), list) and len(kpi.get("sparkline") or []) >= 2:
                new_score += 1
            if new_score > existing_score:
                best[dedup_key] = kpi

        result: List[Dict[str, Any]] = []
        for idx, kpi in enumerate(best.values()):
            name = str(kpi.get("name") or kpi.get("title") or "").strip()
            normalized = dict(kpi)
            normalized["name"] = name
            normalized["position"] = cls._normalize_position(normalized.get("position"), idx)
            result.append(normalized)
        return result

    @classmethod
    def _prune_group_container_kpis(cls, kpis: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if len(kpis) < 3:
            return kpis
        result: List[Dict[str, Any]] = []
        for idx, kpi in enumerate(kpis):
            if not isinstance(kpi, dict):
                continue
            title = str(kpi.get("name") or kpi.get("title") or "").strip()
            if not title or not _KPI_GROUP_TITLE_RE.search(title):
                result.append(kpi)
                continue
            # A KPI that was directly extracted by the detail LLM (has a real value) is never
            # a section-header container — only recovered-from-inventory stubs without values are.
            has_real_value = kpi.get("value") is not None and not kpi.get("_recovered_from_inventory")
            if has_real_value:
                result.append(kpi)
                continue
            position = kpi.get("position") if isinstance(kpi.get("position"), dict) else {}
            top = float(position.get("top", 0.0) or 0.0)
            height = float(position.get("height", 0.0) or 0.0)
            left = float(position.get("left", 0.0) or 0.0)
            width = float(position.get("width", 0.0) or 0.0)
            same_band_neighbors = 0
            same_column_followers = 0
            is_empty_recovered = bool(kpi.get("_recovered_from_inventory")) and kpi.get("value") is None
            for other_idx, other in enumerate(kpis):
                if other_idx == idx or not isinstance(other, dict):
                    continue
                other_title = str(other.get("name") or other.get("title") or "").strip()
                if not other_title:
                    continue
                other_pos = other.get("position") if isinstance(other.get("position"), dict) else {}
                other_top = float(other_pos.get("top", 0.0) or 0.0)
                other_height = float(other_pos.get("height", 0.0) or 0.0)
                other_left = float(other_pos.get("left", 0.0) or 0.0)
                other_width = float(other_pos.get("width", 0.0) or 0.0)
                if abs(other_top - top) > max(0.05, min(0.12, max(height, other_height))):
                    if is_empty_recovered:
                        same_left = abs(other_left - left) <= max(0.06, width * 0.25)
                        similar_width = abs(other_width - width) <= max(0.08, width * 0.35)
                        below_container = other_top >= (top + min(height * 0.6, 0.14))
                        close_enough = other_top <= (top + max(height * 3.2, 0.42))
                        if same_left and similar_width and below_container and close_enough:
                            same_column_followers += 1
                    continue
                if abs(other_left - left) > max(0.36, width * 2.2):
                    continue
                same_band_neighbors += 1
            if same_band_neighbors >= 2:
                continue
            if is_empty_recovered and same_column_followers >= 2:
                continue
            result.append(kpi)
        return result

    @classmethod
    def _prune_duplicate_tables(cls, charts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if len(charts) < 2:
            return charts

        def _area(chart: Dict[str, Any]) -> float:
            position = chart.get("position") if isinstance(chart.get("position"), dict) else {}
            try:
                width = float(position.get("width", 0.0) or 0.0)
                height = float(position.get("height", 0.0) or 0.0)
            except (TypeError, ValueError):
                return 0.0
            return max(0.0, width) * max(0.0, height)

        grouped: Dict[str, List[Dict[str, Any]]] = {}
        passthrough: List[Dict[str, Any]] = []
        for chart in charts:
            if not isinstance(chart, dict):
                continue
            chart_type = _normalize_chart_type(chart.get("chart_type") or chart.get("type"))
            title = str(chart.get("title") or chart.get("chart_name") or chart.get("name") or "").strip()
            if chart_type not in {"table", "pivot_table"} or not title:
                passthrough.append(chart)
                continue
            grouped.setdefault(cls._inventory_title_key(title), []).append(chart)

        result = list(passthrough)
        for _, group in grouped.items():
            if len(group) == 1:
                result.extend(group)
                continue
            best = max(group, key=_area)
            result.append(best)
        return cls._sort_by_position(result)

    @classmethod
    def _dedupe_charts(cls, charts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if len(charts) < 2:
            return charts

        result: List[Dict[str, Any]] = []
        seen_exact: set[tuple] = set()
        for idx, chart in enumerate(charts):
            if not isinstance(chart, dict):
                continue
            chart_type = _normalize_chart_type(chart.get("chart_type") or chart.get("type"))
            title = str(chart.get("title") or chart.get("chart_name") or chart.get("name") or "").strip()
            title_key = cls._inventory_title_key(title)
            categories = tuple(
                str(item).strip().lower()
                for item in (chart.get("categories") or [])
                if str(item).strip()
            )
            series = chart.get("series") or []
            if isinstance(series, list):
                series_key = tuple(
                    str((item or {}).get("name") if isinstance(item, dict) else item).strip().lower()
                    for item in series
                    if str((item or {}).get("name") if isinstance(item, dict) else item).strip()
                )
            else:
                series_key = ()
            position = chart.get("position") if isinstance(chart.get("position"), dict) else {}
            pos_key = (
                round(float(position.get("left", 0.0) or 0.0), 2),
                round(float(position.get("top", 0.0) or 0.0), 2),
                round(float(position.get("width", 0.0) or 0.0), 2),
                round(float(position.get("height", 0.0) or 0.0), 2),
            )
            exact_key = (chart_type, title_key, categories, series_key, pos_key)
            if exact_key in seen_exact:
                continue

            duplicate = False
            for existing in result:
                existing_type = _normalize_chart_type(existing.get("chart_type") or existing.get("type"))
                existing_title = str(existing.get("title") or existing.get("chart_name") or existing.get("name") or "").strip()
                if chart_type != existing_type:
                    continue
                if title_key and title_key == cls._inventory_title_key(existing_title):
                    pos_iou = cls._position_iou(chart.get("position"), existing.get("position"))
                    if pos_iou >= 0.55:
                        duplicate = True
                        break
                    existing_categories = tuple(
                        str(item).strip().lower()
                        for item in (existing.get("categories") or [])
                        if str(item).strip()
                    )
                    if categories and categories == existing_categories:
                        duplicate = True
                        break
            if duplicate:
                continue
            seen_exact.add(exact_key)
            normalized = dict(chart)
            normalized["position"] = cls._normalize_position(normalized.get("position"), idx)
            result.append(normalized)
        return cls._sort_by_position(result)

    @classmethod
    def _sort_by_position(cls, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return sorted(
            items,
            key=lambda item: (
                round(float((item.get("position") or {}).get("top", 0.0) or 0.0), 3),
                round(float((item.get("position") or {}).get("left", 0.0) or 0.0), 3),
            ),
        )
