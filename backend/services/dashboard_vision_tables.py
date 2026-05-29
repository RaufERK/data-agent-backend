"""XML blueprint and table extraction helpers for DashboardVisionService."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..utils.chart_utils import normalize_chart_type as _normalize_chart_type
from ..utils.json_utils import safe_json_loads as _safe_json_loads

logger = logging.getLogger(__name__)


class _DashboardVisionTablesMixin:
    @staticmethod
    def _table_row_count(chart: Dict[str, Any]) -> int:
        rows = chart.get("rows")
        return len(rows) if isinstance(rows, list) else 0

    @staticmethod
    def _chart_block_id(chart: Dict[str, Any]) -> str:
        return str(chart.get("block_id") or "").strip()

    @staticmethod
    def _position_area(position: Any) -> float:
        if not isinstance(position, dict):
            return 0.0
        try:
            width = float(position.get("width") or 0.0)
            height = float(position.get("height") or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, width) * max(0.0, height)

    @classmethod
    def _table_inventory_lookup(
        cls,
        inventory: Optional[Dict[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        if not isinstance(inventory, dict):
            return {}
        blocks = inventory.get("blocks")
        if not isinstance(blocks, list):
            return {}
        lookup: Dict[str, Dict[str, Any]] = {}
        for block in blocks:
            if not isinstance(block, dict):
                continue
            block_id = str(block.get("block_id") or "").strip()
            if block_id:
                lookup[block_id] = block
        return lookup

    @classmethod
    def _inventory_block_is_table(
        cls,
        block_id: str,
        inventory_lookup: Dict[str, Dict[str, Any]],
    ) -> bool:
        if not block_id:
            return False
        block = inventory_lookup.get(block_id)
        if not isinstance(block, dict):
            return False
        block_kind = cls._block_kind_from_type(block.get("block_kind") or block.get("chart_type"))
        return block_kind == "table"

    @classmethod
    def _is_table_target_candidate(
        cls,
        chart: Dict[str, Any],
        inventory_lookup: Dict[str, Dict[str, Any]],
    ) -> bool:
        chart_type = _normalize_chart_type(chart.get("chart_type") or chart.get("type"))
        if chart_type in {"table", "pivot_table"}:
            return True
        if chart.get("_table_placeholder"):
            return True
        return cls._inventory_block_is_table(cls._chart_block_id(chart), inventory_lookup)

    @classmethod
    def _filter_extracted_table_charts_by_inventory(
        cls,
        table_charts: List[Dict[str, Any]],
        inventory: Optional[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        if not isinstance(inventory, dict):
            return table_charts
        blocks = [block for block in (inventory.get("blocks") or []) if isinstance(block, dict)]
        if not blocks:
            return table_charts
        inventory_lookup = cls._table_inventory_lookup(inventory)
        filtered: List[Dict[str, Any]] = []
        for chart in table_charts:
            if not isinstance(chart, dict):
                continue
            block_id = cls._chart_block_id(chart)
            matched_block_id = block_id or cls._match_inventory_block(chart, blocks, "title")
            if matched_block_id:
                if not cls._inventory_block_is_table(matched_block_id, inventory_lookup):
                    continue
                prepared_chart = dict(chart)
                prepared_chart["block_id"] = matched_block_id
                filtered.append(prepared_chart)
                continue
            if cls._table_row_count(chart) < 5 and cls._position_area(chart.get("position")) < 0.08:
                continue
            filtered.append(chart)
        return filtered

    @classmethod
    def _prepare_xml_chart_blueprint(
        cls,
        xml_blueprint: List[Dict[str, Any]],
        allowed_set: set[str],
    ) -> List[Dict[str, Any]]:
        prepared: List[Dict[str, Any]] = []
        for entry in xml_blueprint or []:
            if not isinstance(entry, dict):
                continue
            mapped_type = cls._coerce_chart_type(entry.get("superset_type"), allowed_set)
            prepared_entry = dict(entry)
            prepared_entry["superset_type"] = mapped_type
            prepared.append(prepared_entry)
        if not prepared:
            return []
        # Preserve table widgets from XML blueprint; only drop decorative image widgets first.
        selected = [entry for entry in prepared if entry.get("superset_type") != "image"]
        if not selected:
            selected = prepared
        return selected[: cls.MAX_XML_BLUEPRINT_ITEMS]

    @classmethod
    def _chart_from_xml_entry(
        cls,
        xml_entry: Dict[str, Any],
        index: int,
        allowed_set: set[str],
    ) -> Dict[str, Any]:
        chart_type = cls._coerce_chart_type(xml_entry.get("superset_type"), allowed_set)
        title = str(xml_entry.get("title") or f"График {index + 1}").strip()
        position = (
            cls._normalize_position(xml_entry.get("position"), index)
            if cls.USE_XML_CHART_POSITION
            else cls._grid_position(index)
        )
        chart: Dict[str, Any] = {
            "title": title,
            "chart_type": chart_type,
            "x_axis": xml_entry.get("x_field") or "",
            "y_axis": xml_entry.get("y_field") or "",
            "categories": cls._default_categories_for_chart(chart_type),
            "series": xml_entry.get("series") if isinstance(xml_entry.get("series"), list) else [],
            "table_hint": title,
            "position": position,
        }
        if xml_entry.get("stacked"):
            chart["stacked"] = True
        if xml_entry.get("is_horizontal"):
            chart["is_horizontal"] = True
        if xml_entry.get("is_combo") or chart_type == "combo":
            chart["is_combo"] = True
        return chart

    @classmethod
    def _table_merge_score_matches(
        cls,
        score: float,
        target_chart: Dict[str, Any],
        *,
        exact_block_match: bool = False,
        title_score: float = 0.0,
        pos_score: float = 0.0,
        inventory_lookup: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> bool:
        if exact_block_match:
            return True
        inventory_lookup = inventory_lookup or {}
        if cls._is_table_target_candidate(target_chart, inventory_lookup):
            return score >= 0.45 and (title_score >= 0.12 or pos_score >= 0.28)
        strict_non_table_score = cls.TABLE_MERGE_STRICT_NON_TABLE_SCORE
        threshold = strict_non_table_score if strict_non_table_score is not None else 0.72
        return score >= threshold and title_score >= 0.45 and pos_score >= 0.35

    @classmethod
    def _merge_table_charts(
        cls,
        parsed: Dict[str, Any],
        table_charts: List[Dict[str, Any]],
        inventory: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not isinstance(parsed, dict) or not table_charts:
            return parsed
        charts = parsed.get("charts")
        if not isinstance(charts, list):
            charts = []
            parsed["charts"] = charts
        matched_existing: set[int] = set()
        inventory_lookup = cls._table_inventory_lookup(
            inventory if isinstance(inventory, dict) else parsed.get("vision_inventory")
        )

        for table_chart in table_charts:
            table_title = table_chart.get("title")
            table_pos = table_chart.get("position")
            incoming_block_id = cls._chart_block_id(table_chart)
            incoming_is_placeholder = bool(table_chart.get("_table_placeholder"))
            best_idx = -1
            best_score = 0.0
            best_title_score = 0.0
            best_pos_score = 0.0
            best_exact_block_match = False
            for idx, chart in enumerate(charts):
                if not isinstance(chart, dict):
                    continue
                if cls.TABLE_MERGE_MATCH_ONCE and idx in matched_existing:
                    continue
                target_block_id = cls._chart_block_id(chart)
                exact_block_match = bool(incoming_block_id and target_block_id and incoming_block_id == target_block_id)
                if incoming_block_id and target_block_id and not exact_block_match:
                    continue
                table_target_candidate = cls._is_table_target_candidate(chart, inventory_lookup)
                existing_empty = table_target_candidate and cls._table_row_count(chart) == 0
                title_score = cls._title_score(table_title, chart.get("title") or chart.get("chart_name") or chart.get("name"))
                pos_score = cls._position_iou(table_pos, chart.get("position"))
                type_bonus = 0.3 if table_target_candidate else 0.0
                # Empty table placeholders get an extra bonus so they absorb extracted data
                # even when the normalization LLM used a different title
                empty_placeholder_bonus = 0.25 if existing_empty else 0.0
                score = (2.0 if exact_block_match else 0.0) + (0.65 * title_score) + (0.35 * pos_score) + type_bonus + empty_placeholder_bonus
                if score > best_score:
                    best_score = score
                    best_idx = idx
                    best_title_score = title_score
                    best_pos_score = pos_score
                    best_exact_block_match = exact_block_match

            if best_idx >= 0 and cls._table_merge_score_matches(
                best_score,
                charts[best_idx],
                exact_block_match=best_exact_block_match,
                title_score=best_title_score,
                pos_score=best_pos_score,
                inventory_lookup=inventory_lookup,
            ):
                target = charts[best_idx]
                if cls.TABLE_MERGE_MATCH_ONCE:
                    matched_existing.add(best_idx)
                existing_rows_count = cls._table_row_count(target)
                incoming_rows_count = cls._table_row_count(table_chart)
                target_has_payload = existing_rows_count > 0 or bool(target.get("categories") or target.get("series"))
                if incoming_is_placeholder:
                    should_update = (
                        best_exact_block_match
                        and cls._is_table_target_candidate(target, inventory_lookup)
                        and not target_has_payload
                    )
                else:
                    should_update = incoming_rows_count > 0 and (
                        best_exact_block_match or incoming_rows_count >= existing_rows_count or not target_has_payload
                    )
                if should_update:
                    target["chart_type"] = table_chart.get("chart_type", "table")
                    if not incoming_is_placeholder:
                        target["categories"] = table_chart.get("categories") or target.get("categories") or []
                        target["series"] = table_chart.get("series") or target.get("series") or []
                        target["rows"] = table_chart.get("rows") or target.get("rows") or []
                    target["table_hint"] = table_chart.get("table_hint") or target.get("table_hint") or ""
                    if cls.TABLE_MERGE_COPY_BLOCK_ID and incoming_block_id and not target.get("block_id"):
                        target["block_id"] = incoming_block_id
                    if not target.get("title") and table_chart.get("title"):
                        target["title"] = table_chart.get("title")
                    if not isinstance(target.get("position"), dict):
                        target["position"] = table_chart.get("position")
            else:
                charts.append(table_chart)
        return parsed

    @classmethod
    def _build_table_extraction_prompt(cls, inventory: Optional[Dict[str, Any]] = None) -> str:
        return (
            "Ты анализируешь только ТАБЛИЧНЫЕ блоки на скриншоте BI-дашборда.\n"
            "Найди все большие таблицы (включая иерархические/групповые), даже если рядом есть KPI и графики.\n"
            "Верни только JSON вида:\n"
            "{\n"
            "  \"tables\": [\n"
            "    {\n"
            "      \"title\": \"...\",\n"
            "      \"chart_type\": \"table|pivot_table\",\n"
            "      \"columns\": [\"col1\", \"col2\", \"...\"],\n"
            "      \"rows\": [\n"
            "        [\"v11\", \"v12\"],\n"
            "        [\"v21\", \"v22\"]\n"
            "      ],\n"
            "      \"position\": {\"left\":0.0,\"top\":0.0,\"width\":1.0,\"height\":1.0}\n"
            "    }\n"
            "  ]\n"
            "}\n"
            "Требования:\n"
            "- Для каждой таблицы верни минимум 8 строк, если они читаются.\n"
            "- Сохраняй оригинальные заголовки колонок.\n"
            "- Не добавляй KPI и не табличные графики.\n"
            "- Если таблиц нет, верни {\"tables\":[]}.\n"
        )

    @classmethod
    def _postprocess_extracted_table_charts(
        cls,
        table_charts: List[Dict[str, Any]],
        inventory: Optional[Dict[str, Any]],
        allowed_set: set[str],
    ) -> List[Dict[str, Any]]:
        return table_charts

    @classmethod
    def _extract_table_charts(
        cls,
        client: Any,
        model: str,
        image_data_url: str,
        allowed_set: set[str],
        inventory: Optional[Dict[str, Any]] = None,
    ) -> tuple[List[Dict[str, Any]], str]:
        table_prompt = cls._build_table_extraction_prompt(inventory)
        raw = ""
        try:
            response = client.chat.completions.create(
                model=model,
                max_tokens=3600,
                temperature=0.1,
                top_p=0.9,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": table_prompt},
                            {"type": "image_url", "image_url": {"url": image_data_url}},
                        ],
                    }
                ],
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content.strip()
        except Exception as exc:
            logger.warning("Vision table extraction error: %s", exc)
            return [], raw

        parsed = _safe_json_loads(raw) or {}
        tables_raw = parsed.get("tables")
        if not isinstance(tables_raw, list):
            return [], raw
        result: List[Dict[str, Any]] = []
        for idx, table in enumerate(tables_raw):
            chart = cls._table_chart_from_payload(table, idx, allowed_set)
            if chart:
                result.append(chart)
        result = cls._postprocess_extracted_table_charts(result, inventory, allowed_set)
        return result, raw
