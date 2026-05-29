"""Placeholder-data and XML position helpers for DashboardVisionService."""
from __future__ import annotations

import random
from typing import Any, Dict, List, Optional

from ..utils.color_utils import normalize_hex_color as _normalize_hex_color


class _DashboardVisionCatalogDataMixin:
    @classmethod
    def _fill_placeholder_data(cls, parsed: Dict[str, Any]) -> Dict[str, Any]:
        """Fill empty series/data with placeholder values so charts can render."""
        random.seed(42)

        charts = parsed.get("charts")
        if not isinstance(charts, list):
            return parsed

        for chart in charts:
            if not isinstance(chart, dict):
                continue
            chart_type = chart.get("chart_type", "table")
            confidence = chart.get("confidence")
            try:
                confidence_value = float(confidence)
            except (TypeError, ValueError):
                confidence_value = 0.0
            if confidence_value and confidence_value < 0.55:
                continue
            # Skip types that don't need series data
            if chart_type in ("table", "pivot_table", "image", "big_number"):
                continue

            PALETTE = [
                "#6366F1", "#22D3EE", "#F59E0B", "#10B981", "#EF4444",
                "#8B5CF6", "#F97316", "#3B82F6", "#EC4899", "#14B8A6",
            ]

            categories = chart.get("categories") or []
            series = chart.get("series") or []
            legend_items = chart.get("legend_items") or []

            # Normalize legend_items to list of strings
            legend_names = [
                str(item.get("name") if isinstance(item, dict) else item).strip()
                for item in legend_items
                if (isinstance(item, dict) and item.get("name")) or (isinstance(item, str) and item.strip())
            ]

            # For pie/donut/funnel/treemap/sunburst/radar: build from legend_items if categories empty
            if chart_type in ("pie", "donut", "funnel", "treemap", "sunburst", "radar"):
                if legend_names and not categories:
                    categories = legend_names
                    chart["categories"] = categories
                if not categories:
                    categories = cls._default_categories_for_chart(chart_type)
                    chart["categories"] = categories

                all_empty = not series or all(
                    not (isinstance(s.get("data"), list) and s["data"]) and not s.get("value")
                    for s in series if isinstance(s, dict)
                )
                if all_empty:
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
                    for i, s in enumerate(series):
                        if isinstance(s, dict) and not s.get("hex_code"):
                            s["hex_code"] = PALETTE[i % len(PALETTE)]
                continue

            # Ensure categories exist for other chart types
            if not categories:
                categories = cls._default_categories_for_chart(chart_type)
                chart["categories"] = categories

            num_cats = len(categories)

            # Check if all series have empty data
            all_empty = True
            for s in series:
                if not isinstance(s, dict):
                    continue
                data = s.get("data")
                if isinstance(data, list) and len(data) > 0:
                    all_empty = False
                    break

            if not all_empty:
                # Just fill missing hex_codes
                for i, s in enumerate(series):
                    if isinstance(s, dict) and not s.get("hex_code"):
                        s["hex_code"] = PALETTE[i % len(PALETTE)]
                continue

            # Generate placeholder data based on chart type
            if chart_type in ("scatter",):
                if not series:
                    series = [{"name": "Данные", "data": [], "hex_code": PALETTE[0]}]
                    chart["series"] = series
                for s in series:
                    if not isinstance(s, dict):
                        continue
                    if not s.get("data") or len(s["data"]) == 0:
                        s["data"] = [
                            {"x": random.randint(10, 90), "y": random.randint(10, 90)}
                            for _ in range(min(num_cats, 12) or 8)
                        ]
            elif chart_type in ("sankey", "gantt"):
                pass
            else:
                # Bar, line, area, combo, etc.
                if not series:
                    if len(legend_names) >= 2 and chart_type in ("line", "area", "combo", "bar", "bar_horizontal"):
                        legend_colors = []
                        for item in legend_items:
                            if isinstance(item, dict):
                                legend_colors.append(_normalize_hex_color(item.get("hex_code") or item.get("color")))
                        series = [
                            {
                                "name": name,
                                "data": [],
                                "hex_code": (legend_colors[i] if i < len(legend_colors) else None) or PALETTE[i % len(PALETTE)],
                            }
                            for i, name in enumerate(legend_names[:5])
                        ]
                    else:
                        extracted_palette = chart.get("extracted_palette") if isinstance(chart.get("extracted_palette"), list) else []
                        fallback_color = _normalize_hex_color(extracted_palette[0] if extracted_palette else None) or PALETTE[0]
                        series = [{"name": "Значение", "data": [], "hex_code": fallback_color}]
                    chart["series"] = series

                for i, s in enumerate(series):
                    if not isinstance(s, dict):
                        continue
                    if not s.get("hex_code"):
                        s["hex_code"] = PALETTE[i % len(PALETTE)]
                    data = s.get("data")
                    if isinstance(data, list) and len(data) > 0:
                        continue
                    val = s.get("value")
                    if val and val != 0:
                        try:
                            base = float(val)
                            s["data"] = [round(base * random.uniform(0.3, 1.2), 1) for _ in range(num_cats)]
                        except (TypeError, ValueError):
                            s["data"] = [round(random.uniform(10, 100), 1) for _ in range(num_cats)]
                    else:
                        s["data"] = [round(random.uniform(10, 100), 1) for _ in range(num_cats)]

        return parsed

    @classmethod
    def _blend_positions(
        cls,
        vision_position: Any,
        xml_position: Any,
        xml_weight: float,
        fallback_index: int,
    ) -> Dict[str, float]:
        normalized_vision = cls._normalize_position(vision_position, fallback_index)
        normalized_xml = cls._normalize_position(xml_position, fallback_index)
        weight = max(0.0, min(1.0, float(xml_weight)))
        return {
            "left": round(((1.0 - weight) * normalized_vision["left"]) + (weight * normalized_xml["left"]), 4),
            "top": round(((1.0 - weight) * normalized_vision["top"]) + (weight * normalized_xml["top"]), 4),
            "width": round(((1.0 - weight) * normalized_vision["width"]) + (weight * normalized_xml["width"]), 4),
            "height": round(((1.0 - weight) * normalized_vision["height"]) + (weight * normalized_xml["height"]), 4),
        }

    @classmethod
    def _should_apply_xml_candidate(cls, candidate: Dict[str, Any]) -> bool:
        if not isinstance(candidate, dict):
            return False
        score = float(candidate.get("score") or 0.0)
        title_score = float(candidate.get("title_score") or 0.0)
        count_score = float(candidate.get("count_score") or 0.0)
        type_score = float(candidate.get("type_score") or 0.0)
        layout_score = float(candidate.get("layout_score") or 0.0)
        widget_count = int(candidate.get("xml_widget_count") or 0)

        if score >= cls.XML_SCREEN_HINT_THRESHOLD and title_score >= 0.18:
            return True
        if (
            widget_count <= 2
            and score >= 0.5
            and type_score >= 0.9
            and count_score >= 0.8
            and layout_score >= 0.35
        ):
            return True
        return False

    @classmethod
    def _fill_table_placeholders_from_inventory(
        cls,
        tables: List[Dict[str, Any]],
        inventory: Optional[Dict[str, Any]],
        allowed_set: set[str],
    ) -> List[Dict[str, Any]]:
        if not isinstance(inventory, dict):
            return tables
        blocks = [block for block in (inventory.get("blocks") or []) if isinstance(block, dict)]
        if not blocks:
            return tables
        result = list(tables)
        matched_block_ids: set[str] = set()

        def _block_area(position: Any) -> float:
            if not isinstance(position, dict):
                return 0.0
            try:
                width = float(position.get("width") or 0.0)
                height = float(position.get("height") or 0.0)
            except (TypeError, ValueError):
                return 0.0
            return max(0.0, width) * max(0.0, height)

        for table in result:
            matched = cls._match_inventory_block(table, blocks, "title")
            rows = table.get("rows") if isinstance(table.get("rows"), list) else []
            table_area = _block_area(table.get("position"))
            if (not matched or table_area < 0.12) and len(rows) >= 5:
                fallback_candidates = []
                for block in blocks:
                    if cls._block_kind_from_type(block.get("block_kind") or block.get("chart_type")) != "table":
                        continue
                    block_id = str(block.get("block_id") or "").strip()
                    if block_id and block_id in matched_block_ids:
                        continue
                    area = _block_area(block.get("position"))
                    if area < 0.2:
                        continue
                    fallback_candidates.append((area, block))
                if fallback_candidates:
                    fallback_candidates.sort(key=lambda item: item[0], reverse=True)
                    matched = str((fallback_candidates[0][1] or {}).get("block_id") or "").strip() or matched
                    if matched:
                        block = fallback_candidates[0][1]
                        table["position"] = cls._normalize_position(block.get("position"), 0)
                        if not str(table.get("title") or "").strip() and str(block.get("title") or "").strip():
                            table["title"] = str(block.get("title") or "").strip()
            if matched:
                table["block_id"] = matched
                matched_block_ids.add(matched)
        for block in blocks:
            block_kind = cls._block_kind_from_type(block.get("block_kind") or block.get("chart_type"))
            if block_kind != "table":
                continue
            block_id = str(block.get("block_id") or "").strip()
            if block_id and block_id in matched_block_ids:
                continue
            title = str(block.get("title") or "").strip() or f"Таблица {len(result) + 1}"
            result.append(
                {
                    "block_id": block_id,
                    "title": title,
                    "chart_type": "table",
                    "x_axis": "",
                    "y_axis": "",
                    "categories": [],
                    "series": [],
                    "rows": [],
                    "table_hint": title,
                    "position": cls._normalize_position(block.get("position"), len(result)),
                    "_table_placeholder": True,
                    "_recovered_from_inventory": True,
                }
            )
            if block_id:
                matched_block_ids.add(block_id)
        return result
