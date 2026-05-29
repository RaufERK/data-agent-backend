"""Widget catalog and LLM prompt helpers for DashboardVisionService."""
from __future__ import annotations

from .dashboard_vision_catalog_data import _DashboardVisionCatalogDataMixin

import logging
from typing import Any, Dict, List

from .cloudru_client import get_text_client, get_text_model
from ..utils.chart_utils import normalize_chart_type as _normalize_chart_type

logger = logging.getLogger(__name__)


class _DashboardVisionCatalogMixin(_DashboardVisionCatalogDataMixin):
    @staticmethod
    def _widget_family_key(entry: Dict[str, Any]) -> str:
        if not isinstance(entry, dict):
            return "unknown"
        widget_type = str(entry.get("widget_type") or "").strip()
        visualization_type = str(entry.get("visualization_type") or "").strip()
        if widget_type:
            key = f"nav:{widget_type}"
            if visualization_type:
                key += f":{visualization_type}"
            return key
        superset_type = _normalize_chart_type(entry.get("superset_type"))
        return superset_type or "unknown"

    @classmethod
    def _build_widget_catalog(cls, xml_blueprint: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        families: Dict[str, Dict[str, Any]] = {}
        for entry in xml_blueprint or []:
            if not isinstance(entry, dict):
                continue
            family_key = cls._widget_family_key(entry)
            superset_type = _normalize_chart_type(entry.get("superset_type"))
            item = families.setdefault(
                family_key,
                {
                    "family_key": family_key,
                    "superset_type": superset_type or "table",
                    "widget_type": str(entry.get("widget_type") or "").strip(),
                    "visualization_type": str(entry.get("visualization_type") or "").strip(),
                    "count": 0,
                    "title_examples": [],
                    "flags": [],
                    "field_examples": [],
                },
            )
            item["count"] += 1
            title = str(entry.get("title") or "").strip()
            if title and title not in item["title_examples"] and len(item["title_examples"]) < 4:
                item["title_examples"].append(title)
            for flag in ("stacked", "is_horizontal", "is_combo", "is_compressed"):
                if entry.get(flag) and flag not in item["flags"]:
                    item["flags"].append(flag)
            for field_name in ("x_field", "y_field", "group_field"):
                field_value = str(entry.get(field_name) or "").strip()
                if field_value and field_value not in item["field_examples"] and len(item["field_examples"]) < 5:
                    item["field_examples"].append(field_value)
        catalog = sorted(
            families.values(),
            key=lambda item: (-int(item.get("count") or 0), str(item.get("superset_type") or ""), str(item.get("family_key") or "")),
        )
        return catalog[: cls.MAX_WIDGET_CATALOG_ITEMS]

    @staticmethod
    def _format_widget_catalog(catalog: List[Dict[str, Any]]) -> str:
        if not catalog:
            return "Каталог виджетов XML недоступен."
        lines: List[str] = []
        for idx, entry in enumerate(catalog, start=1):
            title_examples = ", ".join(entry.get("title_examples") or [])
            flags = ", ".join(entry.get("flags") or [])
            fields = ", ".join(entry.get("field_examples") or [])
            suffix_parts = []
            if title_examples:
                suffix_parts.append(f"titles: {title_examples}")
            if fields:
                suffix_parts.append(f"fields: {fields}")
            if flags:
                suffix_parts.append(f"flags: {flags}")
            suffix = f" ({'; '.join(suffix_parts)})" if suffix_parts else ""
            lines.append(
                f"{idx}. family_key={entry.get('family_key')} -> superset_type={entry.get('superset_type')}{suffix}"
            )
        return "\n".join(lines)

    @classmethod
    def _block_kind_from_type(cls, block_type: Any) -> str:
        normalized = str(block_type or "").strip().lower()
        if normalized in {"kpi", "metric", "big_number", "number"}:
            return "kpi"
        if normalized in {"table", "pivot_table"}:
            return "table"
        if normalized in {"country_map", "map", "mosaic_map"}:
            return "map"
        if normalized in {"gauge", "progress", "thermometer", "pictorial"}:
            return "gauge"
        if normalized in {"filter", "dropdown", "date_picker", "selector"}:
            return "filter"
        if normalized in {"legend"}:
            return "legend"
        if normalized in {"text"}:
            return "text"
        return "chart"

    @classmethod
    def _catalog_score_for_block(cls, block: Dict[str, Any], catalog_entry: Dict[str, Any]) -> float:
        title = str(block.get("title") or block.get("name") or "").strip()
        block_kind = cls._block_kind_from_type(block.get("block_kind") or block.get("chart_type"))
        chart_type = _normalize_chart_type(block.get("chart_type") or block.get("type"))
        family_type = _normalize_chart_type(catalog_entry.get("superset_type"))
        score = 0.0

        if block_kind == "kpi" and family_type == "big_number":
            score += 0.55
        elif block_kind == "table" and family_type in {"table", "pivot_table"}:
            score += 0.55
        elif block_kind == "map" and family_type == "country_map":
            score += 0.6
        elif block_kind == "gauge" and family_type == "big_number":
            score += 0.45

        if chart_type and family_type and chart_type == family_type:
            score += 0.35
        elif chart_type in {"pie", "donut"} and family_type in {"pie", "donut"}:
            score += 0.25
        elif chart_type in {"table", "pivot_table"} and family_type in {"table", "pivot_table"}:
            score += 0.25

        title_examples = catalog_entry.get("title_examples") or []
        if title and title_examples:
            score += max((0.3 * cls._title_score(title, example) for example in title_examples), default=0.0)

        title_hint = cls._type_from_title(title)
        if title_hint and family_type and title_hint == family_type:
            score += 0.15

        family_key = str(catalog_entry.get("family_key") or "").lower()
        if block_kind == "gauge" and any(token in family_key for token in ("92", "70", "119", "progress", "gauge")):
            score += 0.15
        if block_kind == "map" and any(token in family_key for token in ("83", "90", "97", "map", "мозаик")):
            score += 0.15
        return round(min(score, 1.0), 4)

    @classmethod
    def _top_widget_candidates_for_block(
        cls,
        block: Dict[str, Any],
        catalog: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        scored: List[Dict[str, Any]] = []
        for entry in catalog:
            score = cls._catalog_score_for_block(block, entry)
            if score <= 0:
                continue
            scored.append(
                {
                    "family_key": entry.get("family_key"),
                    "superset_type": entry.get("superset_type"),
                    "score": score,
                    "title_examples": entry.get("title_examples") or [],
                    "flags": entry.get("flags") or [],
                }
            )
        scored.sort(key=lambda item: (-float(item.get("score") or 0.0), str(item.get("family_key") or "")))
        return scored[: cls.MAX_WIDGET_CANDIDATES_PER_BLOCK]

    @classmethod
    def _attach_widget_candidates(
        cls,
        inventory: Dict[str, Any],
        catalog: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        if not isinstance(inventory, dict):
            return inventory
        blocks = inventory.get("blocks")
        if not isinstance(blocks, list):
            return inventory
        enriched_blocks: List[Dict[str, Any]] = []
        for idx, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            normalized = dict(block)
            normalized["block_id"] = str(block.get("block_id") or f"b{idx + 1}")
            normalized["block_kind"] = cls._block_kind_from_type(block.get("block_kind") or block.get("chart_type"))
            normalized["position"] = cls._normalize_position(block.get("position"), idx)
            normalized["widget_candidates"] = cls._top_widget_candidates_for_block(normalized, catalog)
            enriched_blocks.append(normalized)
        inventory["blocks"] = enriched_blocks
        return inventory

    @staticmethod
    def _extract_response_content(response: Any) -> str:
        try:
            return (response.choices[0].message.content or "").strip()
        except Exception:
            return ""

    @classmethod
    def _run_vision_json_prompt(
        cls,
        client: Any,
        model: str,
        prompt: str,
        image_data_url: str,
        max_tokens: int = 2200,
        temperature: float = 0.1,
    ) -> str:
        response = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=0.9,
            timeout=45,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_data_url}},
                    ],
                }
            ],
            response_format={"type": "json_object"},
        )
        return cls._extract_response_content(response)

    @classmethod
    def _run_text_json_prompt(
        cls,
        prompt: str,
        max_tokens: int = 2200,
        temperature: float = 0.1,
    ) -> str:
        client = get_text_client()
        model = get_text_model()
        response = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=90,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        return cls._extract_response_content(response)

    @classmethod
    def _pick_best_screen(
        cls,
        by_screen: Dict[str, list],
        vision_charts: list,
        vision_kpis: list,
    ) -> Dict[str, Any]:
        if not by_screen or (not vision_charts and not vision_kpis):
            return {}

        vision_titles: List[str] = []
        vision_types: List[str] = []
        vision_positions: List[Dict[str, float]] = []

        for chart in vision_charts:
            if not isinstance(chart, dict):
                continue
            title = chart.get("title") or chart.get("chart_name") or chart.get("name") or ""
            if title:
                vision_titles.append(str(title))
            chart_type = _normalize_chart_type(chart.get("chart_type") or chart.get("type"))
            if chart_type:
                vision_types.append(chart_type)
            position = chart.get("position")
            if isinstance(position, dict):
                vision_positions.append(cls._normalize_position(position, len(vision_positions)))

        for kpi in vision_kpis:
            if not isinstance(kpi, dict):
                continue
            name = kpi.get("name") or ""
            if name:
                vision_titles.append(str(name))
            vision_types.append("big_number")
            position = kpi.get("position")
            if isinstance(position, dict):
                vision_positions.append(cls._normalize_position(position, len(vision_positions)))

        if not vision_titles and not vision_types:
            return {}

        candidates: List[Dict[str, Any]] = []
        vision_block_count = max(1, len(vision_charts) + len(vision_kpis))

        for screen_id, widgets in by_screen.items():
            if not isinstance(widgets, list) or not widgets:
                continue
            visible_widgets = [
                widget for widget in widgets
                if isinstance(widget, dict) and _normalize_chart_type(widget.get("superset_type")) != "image"
            ]
            if not visible_widgets:
                continue

            titled_widgets = [
                widget for widget in visible_widgets
                if str(widget.get("title") or "").strip()
            ]
            xml_titles = [str(widget.get("title") or "").strip() for widget in titled_widgets]
            xml_types = [
                _normalize_chart_type(widget.get("superset_type"))
                for widget in visible_widgets
                if _normalize_chart_type(widget.get("superset_type"))
            ]
            xml_kpi_count = sum(1 for chart_type in xml_types if chart_type == "big_number")
            xml_positions = [
                widget.get("position")
                for widget in visible_widgets
                if isinstance(widget.get("position"), dict)
            ]

            title_scores: List[float] = []
            matched_widget_indices: set[int] = set()
            for vision_title in vision_titles:
                best_title_score = 0.0
                best_title_idx = None
                for xml_idx, xml_title in enumerate(xml_titles):
                    current_score = cls._title_score(vision_title, xml_title)
                    if current_score > best_title_score:
                        best_title_score = current_score
                        best_title_idx = xml_idx
                title_scores.append(best_title_score)
                if best_title_idx is not None and best_title_score >= 0.15:
                    matched_widget_indices.add(best_title_idx)
            title_score = sum(title_scores) / len(title_scores) if title_scores else 0.0

            matched_widgets = [titled_widgets[idx] for idx in sorted(matched_widget_indices)]
            if not matched_widgets and visible_widgets:
                matched_widgets = visible_widgets[: min(len(visible_widgets), max(1, vision_block_count))]

            type_hits = 0
            xml_type_pool = list(xml_types)
            for vision_type in vision_types:
                if vision_type in xml_type_pool:
                    type_hits += 1
                    xml_type_pool.remove(vision_type)
            type_score = type_hits / max(1, len(vision_types))

            xml_block_count = len(visible_widgets)
            count_score = max(
                0.0,
                1.0 - (abs(vision_block_count - xml_block_count) / max(vision_block_count, xml_block_count, 1)),
            )
            expected_kpis = max(len(vision_kpis), xml_kpi_count, 1)
            kpi_score = max(0.0, 1.0 - (abs(len(vision_kpis) - xml_kpi_count) / expected_kpis))

            layout_score = 0.0
            if vision_positions and xml_positions:
                ordered_vision = sorted(
                    vision_positions,
                    key=lambda pos: (round(pos.get("top", 0.0), 3), round(pos.get("left", 0.0), 3)),
                )
                ordered_xml = sorted(
                    xml_positions,
                    key=lambda pos: (round(pos.get("top", 0.0), 3), round(pos.get("left", 0.0), 3)),
                )
                pair_count = min(len(ordered_vision), len(ordered_xml), 6)
                if pair_count > 0:
                    layout_score = sum(
                        cls._position_iou(ordered_vision[idx], ordered_xml[idx])
                        for idx in range(pair_count)
                    ) / pair_count

            score = (
                0.42 * title_score
                + 0.24 * type_score
                + 0.18 * count_score
                + 0.08 * kpi_score
                + 0.08 * layout_score
            )
            candidates.append({
                "screen_id": screen_id,
                "score": round(score, 4),
                "title_score": round(title_score, 4),
                "type_score": round(type_score, 4),
                "count_score": round(count_score, 4),
                "kpi_score": round(kpi_score, 4),
                "layout_score": round(layout_score, 4),
                "widgets": matched_widgets,
                "xml_widget_count": xml_block_count,
            })

        if not candidates:
            return {}
        candidates.sort(key=lambda item: item["score"], reverse=True)
        best = dict(candidates[0])
        best["top_candidates"] = [
            {
                "screen_id": item["screen_id"],
                "score": item["score"],
                "xml_widget_count": item["xml_widget_count"],
            }
            for item in candidates[:3]
        ]
        return best
