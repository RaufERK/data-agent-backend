"""Grid layout, inventory recovery, sparklines and postprocessing mixin for DashboardVisionService."""
from __future__ import annotations

from .dashboard_vision_layout_grid import _LayoutGridMixin

import re
from typing import Any, Dict, List, Optional

from ..utils.chart_utils import normalize_chart_type as _normalize_chart_type


class _LayoutMixin(_LayoutGridMixin):
    """Inventory block recovery, grid layout building, sparkline merging and postprocessing."""

    @staticmethod
    def _inventory_title_key(value: Any) -> str:
        return re.sub(r"[^a-z0-9а-яё]+", "", str(value or "").lower())

    @classmethod
    def _match_inventory_block(
        cls,
        item: Dict[str, Any],
        inventory_blocks: List[Dict[str, Any]],
        title_field: str,
    ) -> Optional[str]:
        title = str(item.get(title_field) or item.get("title") or item.get("name") or "").strip()
        title_key = cls._inventory_title_key(title)
        position = item.get("position")
        best_block_id = None
        best_score = 0.0
        for block in inventory_blocks:
            if not isinstance(block, dict):
                continue
            block_id = str(block.get("block_id") or "").strip()
            if not block_id:
                continue
            block_title = str(block.get("title") or "").strip()
            block_key = cls._inventory_title_key(block_title)
            title_score = 0.0
            if title_key and block_key:
                if title_key == block_key:
                    title_score = 1.0
                elif title_key in block_key or block_key in title_key:
                    title_score = 0.7
                else:
                    title_score = cls._title_score(title, block_title)
            pos_score = cls._position_iou(position, block.get("position"))
            score = max(title_score, pos_score)
            if title_score > 0 and pos_score > 0:
                score = max(score, (0.65 * title_score) + (0.35 * pos_score))
            if score > best_score:
                best_score = score
                best_block_id = block_id
        if best_score >= 0.3:
            return best_block_id
        return None

    @classmethod
    def _inventory_chart_type_hint(
        cls,
        block: Dict[str, Any],
        allowed_set: set[str],
    ) -> str:
        block_kind = cls._block_kind_from_type(block.get("block_kind") or block.get("chart_type"))
        raw_type = str(block.get("chart_type") or "").strip().lower()
        title = str(block.get("title") or "").strip()
        if block_kind == "table":
            return "table"
        if block_kind == "map":
            return "country_map" if "country_map" in allowed_set else "table"
        if block_kind == "gauge":
            return "big_number" if "big_number" in allowed_set else "table"
        title_hint = cls._type_from_title(title)
        if title_hint and title_hint in allowed_set:
            return title_hint
        if raw_type:
            coerced = cls._coerce_chart_type(raw_type, allowed_set)
            if coerced:
                return coerced
        widget_candidates = block.get("widget_candidates") if isinstance(block.get("widget_candidates"), list) else []
        for candidate in widget_candidates:
            candidate_type = cls._coerce_chart_type((candidate or {}).get("superset_type"), allowed_set)
            if candidate_type:
                return candidate_type
        return "table"

    @classmethod
    def _recover_inventory_blocks(
        cls,
        parsed: Dict[str, Any],
        charts: List[Dict[str, Any]],
        kpis: List[Dict[str, Any]],
        allowed_set: set[str],
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, int]]:
        inventory = parsed.get("vision_inventory")
        if not isinstance(inventory, dict):
            return charts, kpis, {"recovered_kpis": 0, "recovered_blocks": 0}
        inventory_blocks = [block for block in (inventory.get("blocks") or []) if isinstance(block, dict)]
        if not inventory_blocks:
            return charts, kpis, {"recovered_kpis": 0, "recovered_blocks": 0}
        omitted_block_ids = {
            str(block.get("block_id") or "").strip()
            for block in (parsed.get("omitted_blocks") or [])
            if isinstance(block, dict) and str(block.get("block_id") or "").strip()
        }

        matched_block_ids: set[str] = set()
        for chart in charts:
            block_id = str(chart.get("block_id") or "").strip() or cls._match_inventory_block(chart, inventory_blocks, "title")
            if block_id:
                chart["block_id"] = block_id
                matched_block_ids.add(block_id)
        for kpi in kpis:
            block_id = str(kpi.get("block_id") or "").strip() or cls._match_inventory_block(kpi, inventory_blocks, "name")
            if block_id:
                kpi["block_id"] = block_id
                matched_block_ids.add(block_id)

        # Track existing KPIs by (key, position_bucket) — same title at different positions = different widget
        existing_kpi_pos_keys: set[tuple] = set()
        for kpi in kpis:
            if not isinstance(kpi, dict):
                continue
            k = cls._inventory_title_key(kpi.get("name") or kpi.get("title") or "")
            pos = kpi.get("position") if isinstance(kpi.get("position"), dict) else {}
            lb = round(float(pos.get("left", 0.0) or 0.0) * 10)
            tb = round(float(pos.get("top", 0.0) or 0.0) * 10)
            existing_kpi_pos_keys.add((k, lb, tb))
        recovered_kpis = 0
        recovered_blocks = 0

        for block in inventory_blocks:
            block_id = str(block.get("block_id") or "").strip()
            if block_id and block_id in matched_block_ids:
                continue
            if block_id and block_id in omitted_block_ids:
                continue
            block_kind = cls._block_kind_from_type(block.get("block_kind") or block.get("chart_type"))
            title = str(block.get("title") or "").strip()
            position = cls._normalize_position(block.get("position"), len(charts) + len(kpis))
            area = float(position.get("width", 0.0) or 0.0) * float(position.get("height", 0.0) or 0.0)
            try:
                confidence = float(block.get("confidence") or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0
            if not title and block_kind in {"legend", "text"}:
                continue
            if not title and block_kind in {"chart", "table"} and (confidence < 0.75 or area < 0.12):
                continue
            if block_kind == "kpi":
                key = cls._inventory_title_key(title)
                lb = round(float(position.get("left", 0.0) or 0.0) * 10)
                tb = round(float(position.get("top", 0.0) or 0.0) * 10)
                if key and (key, lb, tb) in existing_kpi_pos_keys:
                    continue
                title_hint = cls._type_from_title(title)
                if title_hint and title_hint not in {"big_number", "big_number_total"}:
                    continue
                kpis.append({
                    "block_id": block_id,
                    "name": title or f"KPI {len(kpis) + 1}",
                    "value": None,
                    "unit": "",
                    "note": "",
                    "position": position,
                    "_recovered_from_inventory": True,
                })
                if key:
                    existing_kpi_pos_keys.add((key, lb, tb))
                recovered_kpis += 1
                if block_id:
                    matched_block_ids.add(block_id)
                continue
            if block_kind in {"legend", "text"}:
                continue
            # Filter widgets (dropdowns, date-pickers, selectors) are real dashboard widgets —
            # recover them as chart_type=filter so they appear in the final spec and XML.
            if block_kind == "filter":
                recovered_type = "filter"
            else:
                recovered_type = cls._inventory_chart_type_hint(block, allowed_set)
            widget_candidates = block.get("widget_candidates") if isinstance(block.get("widget_candidates"), list) else []
            recovered_chart = {
                "block_id": block_id,
                "title": title or f"Блок {len(charts) + 1}",
                "chart_type": recovered_type,
                "source_chart_type": str(block.get("chart_type") or "").strip().lower(),
                "x_axis": "",
                "y_axis": "",
                "categories": [],
                "series": [],
                "rows": [],
                "table_hint": title or "",
                "position": position,
                "widget_family": str((widget_candidates[0] or {}).get("family_key") or "") if widget_candidates else "",
                "_recovered_from_inventory": True,
            }
            charts.append(recovered_chart)
            recovered_blocks += 1
            if block_id:
                matched_block_ids.add(block_id)
        return charts, kpis, {"recovered_kpis": recovered_kpis, "recovered_blocks": recovered_blocks}

    @staticmethod
    def _sparkline_key(text: Any) -> str:
        return re.sub(r"[^a-z0-9а-яё]+", "", str(text or "").lower())

    @staticmethod
    def _sparkline_number(value: Any) -> Optional[float]:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        if value is None:
            return None
        text = str(value).strip().replace("\u00a0", " ").replace(" ", "").replace(",", ".")
        match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
        if not match:
            return None
        try:
            return float(match.group(0))
        except ValueError:
            return None

    @classmethod
    def _chart_sparkline_values(cls, chart: Dict[str, Any]) -> List[float]:
        values: List[float] = []
        series = chart.get("series")
        if isinstance(series, list):
            for entry in series:
                if not isinstance(entry, dict):
                    continue
                data = entry.get("data")
                if not isinstance(data, list):
                    continue
                for value in data:
                    numeric = cls._sparkline_number(value)
                    if numeric is not None:
                        values.append(float(numeric))
                if values:
                    break
        if not values:
            rows = chart.get("rows")
            if isinstance(rows, list):
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    numeric_values = [cls._sparkline_number(value) for value in row.values()]
                    numeric_values = [value for value in numeric_values if value is not None]
                    if numeric_values:
                        values.append(float(numeric_values[-1]))
        return values[:24]

    @classmethod
    def _merge_kpi_sparklines(
        cls,
        charts: List[Dict[str, Any]],
        kpis: List[Dict[str, Any]],
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], int]:
        if not charts or not kpis:
            return charts, kpis, 0

        merged = 0
        remaining_charts: List[Dict[str, Any]] = []
        kpi_items = [kpi for kpi in kpis if isinstance(kpi, dict)]

        for chart in charts:
            chart_type = _normalize_chart_type(chart.get("chart_type") or chart.get("type"))
            if chart_type not in {"line", "area", "bar"}:
                remaining_charts.append(chart)
                continue
            sparkline_values = cls._chart_sparkline_values(chart)
            if len(sparkline_values) < 2:
                remaining_charts.append(chart)
                continue

            pos = chart.get("position") if isinstance(chart.get("position"), dict) else {}
            chart_width = float(pos.get("width", 0.0) or 0.0)
            chart_height = float(pos.get("height", 0.0) or 0.0)
            if chart_width > 0.48 or chart_height > 0.28:
                remaining_charts.append(chart)
                continue

            chart_title_key = cls._sparkline_key(chart.get("title") or chart.get("name"))
            chart_center_y = float(pos.get("top", 0.0) or 0.0) + chart_height / 2.0
            best_kpi: Optional[Dict[str, Any]] = None
            best_score = -1.0

            for kpi in kpi_items:
                if isinstance(kpi.get("sparkline"), list) and len(kpi.get("sparkline") or []) >= 2:
                    continue
                kpi_pos = kpi.get("position") if isinstance(kpi.get("position"), dict) else {}
                kpi_height = float(kpi_pos.get("height", 0.0) or 0.0)
                kpi_width = float(kpi_pos.get("width", 0.0) or 0.0)
                if kpi_height <= 0 or kpi_width <= 0:
                    continue
                kpi_center_y = float(kpi_pos.get("top", 0.0) or 0.0) + kpi_height / 2.0
                vertical_distance = abs(chart_center_y - kpi_center_y)
                same_band = vertical_distance <= max(0.08, min(0.18, max(kpi_height, chart_height)))
                if not same_band:
                    continue

                kpi_title_key = cls._sparkline_key(kpi.get("name") or kpi.get("metric_name") or kpi.get("title"))
                title_match = bool(
                    chart_title_key
                    and kpi_title_key
                    and (chart_title_key in kpi_title_key or kpi_title_key in chart_title_key)
                )
                kpi_left = float(kpi_pos.get("left", 0.0) or 0.0)
                kpi_right = kpi_left + kpi_width
                chart_left = float(pos.get("left", 0.0) or 0.0)
                chart_right = chart_left + chart_width
                horizontal_overlap = max(0.0, min(kpi_right, chart_right) - max(kpi_left, chart_left))
                near_right = 0 <= (chart_left - kpi_right) <= 0.18
                near_inside = horizontal_overlap >= min(kpi_width, chart_width) * 0.25

                score = 0.0
                if title_match:
                    score += 3.0
                if near_inside:
                    score += 1.5
                if near_right:
                    score += 1.0
                score += max(0.0, 1.0 - vertical_distance * 8.0)

                if score > best_score and (title_match or near_inside or near_right):
                    best_score = score
                    best_kpi = kpi

            if best_kpi is None or best_score < 1.2:
                remaining_charts.append(chart)
                continue

            best_kpi["sparkline"] = sparkline_values
            best_kpi["sparkline_type"] = "bar" if chart_type == "bar" else "line"
            if not best_kpi.get("color") and chart.get("color"):
                best_kpi["color"] = chart.get("color")
            merged += 1

        return remaining_charts, kpis, merged

    @classmethod
    def _promote_small_line_cards_to_kpis(
        cls,
        charts: List[Dict[str, Any]],
        kpis: List[Dict[str, Any]],
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], int]:
        if not charts or not kpis:
            return charts, kpis, 0

        promoted = 0
        remaining_charts: List[Dict[str, Any]] = []
        kpi_positions = [
            kpi.get("position")
            for kpi in kpis
            if isinstance(kpi, dict) and isinstance(kpi.get("position"), dict)
        ]

        for chart in charts:
            chart_type = _normalize_chart_type(chart.get("chart_type") or chart.get("type"))
            if chart_type not in {"line", "area"}:
                remaining_charts.append(chart)
                continue
            pos = chart.get("position") if isinstance(chart.get("position"), dict) else {}
            width = float(pos.get("width", 0.0) or 0.0)
            height = float(pos.get("height", 0.0) or 0.0)
            if width > 0.45 or height > 0.24:
                remaining_charts.append(chart)
                continue

            sparkline_values = cls._chart_sparkline_values(chart)
            if len(sparkline_values) < 2:
                remaining_charts.append(chart)
                continue

            top = float(pos.get("top", 0.0) or 0.0)
            center_y = top + height / 2.0
            shares_kpi_band = False
            for kpi_pos in kpi_positions:
                if not isinstance(kpi_pos, dict):
                    continue
                kpi_top = float(kpi_pos.get("top", 0.0) or 0.0)
                kpi_height = float(kpi_pos.get("height", 0.0) or 0.0)
                kpi_center_y = kpi_top + kpi_height / 2.0
                if abs(center_y - kpi_center_y) <= max(0.08, min(0.16, max(height, kpi_height))):
                    shares_kpi_band = True
                    break
            if not shares_kpi_band:
                remaining_charts.append(chart)
                continue

            title = str(chart.get("title") or chart.get("name") or "").strip()
            if not title:
                remaining_charts.append(chart)
                continue
            value = (
                chart.get("value")
                if chart.get("value") is not None
                else chart.get("value_number")
                if chart.get("value_number") is not None
                else sparkline_values[-1]
            )
            kpis.append({
                "name": title,
                "value": value,
                "unit": chart.get("unit") or chart.get("value_unit") or "",
                "note": chart.get("note") or chart.get("notes") or "",
                "widget_family": chart.get("widget_family") or "nav:94",
                "sparkline": sparkline_values,
                "sparkline_type": "line",
                "confidence": chart.get("confidence"),
                "position": pos,
                "color": chart.get("color"),
            })
            promoted += 1

        return remaining_charts, kpis, promoted

    @classmethod
    def _postprocess_parsed_result(cls, parsed: Dict[str, Any], allowed_set: set[str]) -> Dict[str, Any]:
        if not isinstance(parsed, dict):
            return parsed
        charts_raw = parsed.get("charts") if isinstance(parsed.get("charts"), list) else []
        kpis_raw = parsed.get("kpis") if isinstance(parsed.get("kpis"), list) else []

        charts: List[Dict[str, Any]] = []
        kpis: List[Dict[str, Any]] = []
        for idx, kpi in enumerate(kpis_raw):
            if not isinstance(kpi, dict):
                continue
            normalized_kpi = dict(kpi)
            normalized_kpi["position"] = cls._normalize_position(normalized_kpi.get("position"), idx)
            kpis.append(normalized_kpi)
        for idx, chart in enumerate(charts_raw):
            if not isinstance(chart, dict):
                continue
            normalized = dict(chart)
            normalized["position"] = cls._normalize_position(normalized.get("position"), idx)
            normalized["chart_type"] = cls._refine_chart_type(normalized, allowed_set)
            if cls._is_kpi_candidate(normalized):
                maybe_kpi = cls._chart_to_kpi(normalized, idx)
                if maybe_kpi:
                    if normalized.get("widget_family"):
                        maybe_kpi["widget_family"] = normalized.get("widget_family")
                    if normalized.get("confidence") is not None:
                        maybe_kpi["confidence"] = normalized.get("confidence")
                    kpis.append(maybe_kpi)
                    continue
            charts.append(normalized)

        deduped_kpis = cls._dedupe_kpis(kpis)
        charts, deduped_kpis, recovery_stats = cls._recover_inventory_blocks(parsed, charts, deduped_kpis, allowed_set)
        deduped_kpis = cls._prune_group_container_kpis(cls._dedupe_kpis(deduped_kpis))
        charts, deduped_kpis, promoted_sparkline_kpi_count = cls._promote_small_line_cards_to_kpis(charts, deduped_kpis)
        charts, deduped_kpis, sparkline_merge_count = cls._merge_kpi_sparklines(charts, deduped_kpis)

        parsed["kpis"] = cls._sort_by_position(cls._dedupe_kpis(deduped_kpis))
        parsed["charts"] = cls._dedupe_charts(cls._prune_duplicate_tables(cls._sort_by_position(charts)))
        parsed["grid_layout"] = cls._build_grid_layout(parsed)
        stage_diag = parsed.get("stage_diagnostics") if isinstance(parsed.get("stage_diagnostics"), dict) else {}
        stage_diag["recovered_kpi_count"] = recovery_stats.get("recovered_kpis", 0)
        stage_diag["recovered_block_count"] = recovery_stats.get("recovered_blocks", 0)
        stage_diag["promoted_sparkline_kpi_count"] = promoted_sparkline_kpi_count
        stage_diag["merged_kpi_sparkline_count"] = sparkline_merge_count
        stage_diag["postprocess_kpi_count"] = len(parsed["kpis"])
        stage_diag["postprocess_chart_count"] = len(parsed["charts"])
        parsed["stage_diagnostics"] = stage_diag
        return parsed
