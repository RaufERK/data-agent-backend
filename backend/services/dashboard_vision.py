"""Dashboard vision analysis service (standalone).

The large DashboardVisionService class has been split into mixins:
  - dashboard_vision_chart_type.py  — _ChartTypeMixin  (chart-type detection, KPI heuristics)
  - dashboard_vision_layout.py      — _LayoutMixin     (inventory recovery, grid layout, sparklines)
  - dashboard_vision_colors.py      — _ColorsMixin     (image color extraction)
  - dashboard_vision_prompts.py     — _PromptsMixin    (LLM prompt builders, inventory normalisation)
  - dashboard_vision_common.py      — DashboardVisionCommonMixin (shared base helpers)

This file contains the orchestrator: widget catalog, LLM calls, staged vision pipeline,
XML blueprint matching, and the public analyze_dashboard() entry point.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .cloudru_client import get_vision_client, get_vision_model
from .vision_config import get_config
from .dashboard_vision_common import DashboardVisionCommonMixin
from .dashboard_vision_chart_type import _ChartTypeMixin
from .dashboard_vision_layout import _LayoutMixin
from .dashboard_vision_colors import _ColorsMixin
from .dashboard_vision_prompts import _PromptsMixin
from .dashboard_vision_catalog import _DashboardVisionCatalogMixin
from .dashboard_vision_fallback import build_local_dashboard_fallback
from ..utils.json_utils import safe_json_loads as _safe_json_loads

logger = logging.getLogger(__name__)

class DashboardVisionService(
    _DashboardVisionCatalogMixin,
    _ChartTypeMixin,
    _LayoutMixin,
    _ColorsMixin,
    _PromptsMixin,
    DashboardVisionCommonMixin,
):
    """Extracts dashboard structure from an image using Vision LLM."""

    KEEP_EMPTY_TABLE_PLACEHOLDERS = True
    TABLE_MERGE_MATCH_ONCE = True
    TABLE_MERGE_STRICT_NON_TABLE_SCORE = 0.72
    TABLE_MERGE_COPY_BLOCK_ID = True
    MAX_WIDGET_CATALOG_ITEMS = 24
    MAX_WIDGET_CANDIDATES_PER_BLOCK = 5
    XML_SCREEN_HINT_THRESHOLD = 0.48
    XML_LAYOUT_HINT_THRESHOLD = 0.52

    @classmethod
    def _run_staged_vision(
            cls,
            image_data_url: str,
            allowed_types: List[str],
            xml_blueprint: List[Dict[str, Any]],
            on_progress: Optional[Callable[[str, int], None]] = None,
        ) -> Dict[str, Any]:
            def _emit(stage: str, pct: int) -> None:
                if on_progress:
                    try:
                        on_progress(stage, pct)
                    except Exception:
                        pass

            allowed_hint = ", ".join(allowed_types)
            widget_catalog = cls._build_widget_catalog(xml_blueprint)
            widget_catalog_text = cls._format_widget_catalog(widget_catalog)
            vision_client = get_vision_client()
            vision_model = get_vision_model()

            _emit("ocr", 10)
            inventory_prompt = cls._build_inventory_prompt(allowed_hint, widget_catalog_text)
            inventory_raw = ""
            try:
                inventory_raw = cls._run_vision_json_prompt(
                    vision_client,
                    vision_model,
                    inventory_prompt,
                    image_data_url,
                    max_tokens=1600,
                    temperature=0.05,
                )
            except Exception as exc:
                logger.warning("Vision inventory extraction error: %s", exc)
            inventory = cls._normalize_inventory_payload(_safe_json_loads(inventory_raw) or {})
            inventory = cls._attach_widget_candidates(inventory, widget_catalog)
            _emit("ocr_done", 30)

            # Retry inventory once if it returned 0 blocks (vision model sometimes fails)
            if not inventory.get("blocks"):
                logger.warning("Vision inventory returned 0 blocks, retrying with higher temperature")
                try:
                    retry_raw = cls._run_vision_json_prompt(
                        vision_client,
                        vision_model,
                        inventory_prompt,
                        image_data_url,
                        max_tokens=2000,
                        temperature=0.2,
                    )
                    retry_inventory = cls._normalize_inventory_payload(_safe_json_loads(retry_raw) or {})
                    if retry_inventory.get("blocks"):
                        inventory = cls._attach_widget_candidates(retry_inventory, widget_catalog)
                        inventory_raw = retry_raw
                        logger.info("Inventory retry succeeded: %d blocks", len(inventory.get("blocks") or []))
                except Exception as exc:
                    logger.warning("Inventory retry also failed: %s", exc)

            if not inventory.get("blocks"):
                logger.warning("Vision inventory returned 0 blocks after retry; skipping expensive detail stages")
                return {
                    "charts": [],
                    "kpis": [],
                    "vision_inventory": inventory,
                    "vision_inventory_raw": inventory_raw,
                    "vision_detail_raw": "",
                    "vision_normalization_raw": "",
                    "widget_catalog": widget_catalog,
                    "widget_catalog_size": len(widget_catalog),
                    "table_prompt_used": False,
                    "table_raw_response": "",
                    "stage_diagnostics": {
                        "inventory_block_count": 0,
                        "detail_kpi_count": 0,
                        "detail_chart_count": 0,
                        "normalized_kpi_count": 0,
                        "normalized_chart_count": 0,
                        "widget_catalog_size": len(widget_catalog),
                        "short_circuited_empty_inventory": True,
                    },
                    "prompt": {
                        "inventory": inventory_prompt,
                        "detail": "",
                        "normalization": "",
                    },
                }

            _emit("detail", 40)
            detail_prompt = cls._build_detail_prompt(allowed_hint, widget_catalog_text, inventory)
            detail_raw = ""
            try:
                detail_raw = cls._run_vision_json_prompt(
                    vision_client,
                    vision_model,
                    detail_prompt,
                    image_data_url,
                    max_tokens=2600,
                    temperature=0.1,
                )
            except Exception as exc:
                logger.warning("Vision detail extraction error: %s", exc)
            detail = _safe_json_loads(detail_raw) or {}
            # Retry detail once if the response parsed to empty (LLM occasionally returns nothing).
            _detail_empty = not detail.get("kpis") and not detail.get("charts")
            if _detail_empty and inventory.get("blocks"):
                logger.warning("Vision detail returned 0 widgets, retrying with higher temperature")
                try:
                    detail_raw_retry = cls._run_vision_json_prompt(
                        vision_client,
                        vision_model,
                        detail_prompt,
                        image_data_url,
                        max_tokens=2800,
                        temperature=0.3,
                    )
                    detail_retry = _safe_json_loads(detail_raw_retry) or {}
                    if detail_retry.get("kpis") or detail_retry.get("charts"):
                        detail = detail_retry
                        detail_raw = detail_raw_retry
                        logger.info(
                            "Detail retry succeeded: kpis=%d charts=%d",
                            len(detail.get("kpis") or []),
                            len(detail.get("charts") or []),
                        )
                    else:
                        logger.warning("Detail retry also returned 0 widgets; falling back to inventory-only recovery")
                except Exception as exc:
                    logger.warning("Detail retry failed: %s", exc)
            detail = cls._apply_inventory_positions(detail, inventory)
            _emit("detail_done", 60)

            _emit("tables", 65)
            table_charts, table_raw = cls._extract_table_charts(
                vision_client,
                vision_model,
                image_data_url,
                set(allowed_types),
                inventory=inventory,
            )
            _emit("tables_done", 75)

            _emit("normalize", 80)
            normalization_prompt = cls._build_normalization_prompt(
                allowed_hint,
                widget_catalog_text,
                inventory,
                detail,
                table_charts,
            )
            normalization_raw = ""
            try:
                normalization_raw = cls._run_text_json_prompt(
                    normalization_prompt,
                    max_tokens=2600,
                    temperature=0.1,
                )
            except Exception as exc:
                logger.warning("Qwen normalization error: %s", exc)
            normalized = _safe_json_loads(normalization_raw) or detail or {}
            normalized = cls._merge_table_charts(normalized, table_charts, inventory=inventory)
            normalized = cls._apply_inventory_positions(normalized, inventory)
            _emit("normalize_done", 90)
            normalized["vision_inventory"] = inventory
            normalized["vision_inventory_raw"] = inventory_raw
            normalized["vision_detail_raw"] = detail_raw
            normalized["vision_normalization_raw"] = normalization_raw
            normalized["widget_catalog"] = widget_catalog
            normalized["widget_catalog_size"] = len(widget_catalog)
            normalized["table_prompt_used"] = bool(table_charts)
            normalized["table_raw_response"] = table_raw
            normalized["stage_diagnostics"] = {
                "inventory_block_count": len(inventory.get("blocks") or []),
                "detail_kpi_count": len(detail.get("kpis") or []) if isinstance(detail.get("kpis"), list) else 0,
                "detail_chart_count": len(detail.get("charts") or []) if isinstance(detail.get("charts"), list) else 0,
                "normalized_kpi_count": len(normalized.get("kpis") or []) if isinstance(normalized.get("kpis"), list) else 0,
                "normalized_chart_count": len(normalized.get("charts") or []) if isinstance(normalized.get("charts"), list) else 0,
                "widget_catalog_size": len(widget_catalog),
            }
            normalized["prompt"] = {
                "inventory": inventory_prompt,
                "detail": detail_prompt,
                "normalization": normalization_prompt,
            }
            return normalized

    @classmethod
    def _apply_xml_blueprint(
        cls,
        parsed: Dict[str, Any],
        xml_chart_blueprint: List[Dict[str, Any]],
        allowed_set: set[str],
        xml_match_score: float = 0.0,
    ) -> Dict[str, Any]:
        if not isinstance(parsed, dict) or not xml_chart_blueprint:
            return parsed
        charts = parsed.get("charts")
        if not isinstance(charts, list):
            charts = []
            parsed["charts"] = charts

        unmatched_xml: set[int] = set(range(len(xml_chart_blueprint)))
        matches: Dict[int, int] = {}

        for chart_idx, chart in enumerate(charts):
            if not isinstance(chart, dict):
                continue
            chart_title = chart.get("title") or chart.get("chart_name") or chart.get("name")
            best_idx = None
            best_score = 0.0
            for xml_idx in unmatched_xml:
                xml_title = xml_chart_blueprint[xml_idx].get("title")
                score = cls._title_score(chart_title, xml_title)
                if score > best_score:
                    best_score = score
                    best_idx = xml_idx
            if best_idx is not None and best_score >= 0.2:
                matches[chart_idx] = best_idx

        for xml_idx in matches.values():
            unmatched_xml.discard(xml_idx)

        for chart_idx, xml_idx in matches.items():
            chart = charts[chart_idx]
            xml_entry = xml_chart_blueprint[xml_idx]
            chart["chart_type"] = cls._coerce_chart_type(xml_entry.get("superset_type"), allowed_set)
            if xml_entry.get("stacked"):
                chart["stacked"] = True
            if xml_entry.get("is_horizontal"):
                chart["is_horizontal"] = True
            if xml_entry.get("is_combo") or chart.get("chart_type") == "combo":
                chart["is_combo"] = True
            if not chart.get("title") and xml_entry.get("title"):
                chart["title"] = xml_entry.get("title")
            if not chart.get("categories"):
                chart["categories"] = cls._default_categories_for_chart(chart.get("chart_type"))
            xml_position = xml_entry.get("position")
            if isinstance(xml_position, dict) and xml_match_score >= cls.XML_LAYOUT_HINT_THRESHOLD:
                chart["position"] = cls._blend_positions(
                    chart.get("position"),
                    xml_position,
                    min(0.55, 0.2 + (xml_match_score * 0.5)),
                    chart_idx,
                )
            elif not isinstance(chart.get("position"), dict):
                chart["position"] = cls._normalize_position(xml_position, chart_idx)

        parsed["xml_blueprint_chart_count"] = len(xml_chart_blueprint)
        parsed["xml_blueprint_applied_count"] = len(matches)
        parsed["xml_blueprint_match_score"] = round(float(xml_match_score or 0.0), 4)
        return parsed

    @classmethod
    def analyze_dashboard(
        cls,
        image_path: Path,
        table_names: Optional[List[str]] = None,
        xml_plan_path: Optional[Path] = None,
        ocr_dir: Optional[Path] = None,
        prefer_enriched_fallback: bool = True,
        on_progress: Optional[Callable[[str, int], None]] = None,
    ) -> Dict[str, Any]:
        if not image_path.exists() or not image_path.is_file():
            raise FileNotFoundError(f"Файл {image_path} не найден")

        table_hint = ""
        if table_names:
            cleaned = [t for t in table_names if isinstance(t, str) and t.strip()]
            if cleaned:
                table_hint = "Доступные таблицы (если можешь, используй их как подсказку): " + ", ".join(cleaned)

        config = get_config()
        upload_dir = Path(config.get("TEXT_ANALYZER_UPLOAD_FOLDER") or "/tmp/ai_uploads")
        if xml_plan_path is None:
            xml_raw = os.getenv("TEXT_ANALYZER_XML_PLAN") or config.get("TEXT_ANALYZER_XML_PLAN")
            if isinstance(xml_raw, str) and xml_raw.strip():
                candidate = Path(xml_raw).expanduser()
                if candidate.exists() and candidate.is_file():
                    xml_plan_path = candidate
        # Try screen-aware XML loading first, fall back to flat loading
        xml_by_screen, xml_name = cls._load_xml_by_screen(upload_dir, xml_plan_path)
        if not xml_by_screen:
            xml_blueprint, xml_name = cls._load_xml_blueprint(upload_dir, xml_plan_path)
        else:
            # Flat list for allowed_chart_types calculation (union of all screens)
            xml_blueprint = []
            for widgets in xml_by_screen.values():
                xml_blueprint.extend(widgets)
        allowed_types, _ = cls._allowed_chart_types(upload_dir, xml_plan_path, xml_blueprint=xml_blueprint)
        allowed_set = set(allowed_types)

        # Parse VISION_IMAGE_JSON_MAP stub
        stub_map_raw = config.get("VISION_IMAGE_JSON_MAP")
        stub_map = {}
        if stub_map_raw:
            try:
                if isinstance(stub_map_raw, str):
                    stub_map = json.loads(stub_map_raw)
                elif isinstance(stub_map_raw, dict):
                    stub_map = stub_map_raw
            except json.JSONDecodeError as exc:
                logger.warning("Не удалось распарсить VISION_IMAGE_JSON_MAP: %s", exc)
                stub_map = {}

        stub_key_candidates = [image_path.name, str(image_path), str(image_path.resolve())]
        stub_payload = None
        for key in stub_key_candidates:
            if key in stub_map:
                stub_payload = stub_map[key]
                break
        if not stub_payload and image_path.name:
            base_name = re.sub(r"_\d+(?=\.[^.]+$)", "", image_path.name)
            if base_name != image_path.name and base_name in stub_map:
                stub_payload = stub_map[base_name]

        if config.get("VISION_DISABLED"):
            if not stub_payload:
                raise RuntimeError(
                    f"Vision отключен и нет JSON-распознавания для изображения '{image_path.name}'. "
                    "Добавьте его в VISION_IMAGE_JSON_MAP."
                )
            parsed = json.loads(json.dumps(stub_payload))
            charts = parsed.get("charts")
            if isinstance(charts, list):
                for chart in charts:
                    if not isinstance(chart, dict):
                        continue
                    chart["chart_type"] = cls._coerce_chart_type(chart.get("chart_type"), allowed_set)
            parsed = cls._postprocess_parsed_result(parsed, allowed_set)
            parsed["allowed_chart_types"] = allowed_types
            parsed["xml_source"] = xml_name
            parsed["prompt"] = "VISION_DISABLED_STUB"
            parsed["raw_response"] = json.dumps(parsed, ensure_ascii=False)
            parsed["xml_screen_candidate"] = {
                "screen_id": None,
                "score": None,
                "top_candidates": [],
                "applied": False,
                "mode": "catalog_only",
            }
            return parsed
        from .image_compress import compress_image_to_base64
        image_b64, mime = compress_image_to_base64(image_path)
        image_data_url = f"data:{mime};base64,{image_b64}"
        parsed = cls._run_staged_vision(image_data_url, allowed_types, xml_blueprint, on_progress=on_progress)
        if table_hint:
            parsed["table_name_hint"] = table_hint
        parsed = cls._postprocess_parsed_result(parsed, allowed_set)
        if not parsed.get("charts") and not parsed.get("kpis"):
            logger.warning("Vision produced no widgets; using local image fallback for %s", image_path.name)
            parsed = build_local_dashboard_fallback(image_path, reason="empty_vision_result")
            parsed = cls._postprocess_parsed_result(parsed, allowed_set)
        parsed = cls._apply_extracted_chart_colors(parsed, image_path)
        parsed = cls._fill_placeholder_data(parsed)
        parsed["allowed_chart_types"] = allowed_types
        parsed["xml_source"] = xml_name
        parsed["xml_screen_candidate"] = {
            "screen_id": None,
            "score": None,
            "top_candidates": [],
            "applied": False,
            "mode": "catalog_only",
        }
        parsed["raw_response"] = parsed.get("vision_detail_raw") or parsed.get("vision_normalization_raw") or ""
        return parsed
