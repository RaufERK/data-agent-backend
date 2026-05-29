"""Dashboard vision analysis service (standalone)."""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .cloudru_client import get_vision_client, get_vision_model
from .vision_config import get_config
from .dashboard_vision_common import DashboardVisionCommonMixin
from ..utils.chart_utils import normalize_chart_type as _normalize_chart_type
from ..utils.json_utils import safe_json_loads as _safe_json_loads

logger = logging.getLogger(__name__)




class DashboardVisionServiceDrawing(DashboardVisionCommonMixin):
    """Extracts dashboard structure from a hand-drawn image using Vision LLM.

    This is the drawing-optimised analyser preserved from commit 0f4a8fd.
    It works better for hand-drawn / sketched dashboards.
    """

    USE_XML_CHART_POSITION = False

    @classmethod
    def _pick_best_screen(
        cls,
        by_screen: Dict[str, list],
        vision_charts: list,
        vision_kpis: list,
    ) -> List[Dict[str, Any]]:
        """Pick XML widgets that individually match Vision-detected charts/KPIs.

        Instead of returning an entire screen (which may be a 55-widget demo
        library), we collect *all* XML widgets across all screens and return
        only those that title-match a Vision-detected element.
        """
        if not by_screen or (not vision_charts and not vision_kpis):
            return []

        # Gather Vision titles
        vision_items: list[str] = []
        for chart in vision_charts:
            if isinstance(chart, dict):
                title = chart.get("title") or chart.get("chart_name") or chart.get("name") or ""
                if title:
                    vision_items.append(title)
        for kpi in vision_kpis:
            if isinstance(kpi, dict):
                name = kpi.get("name") or ""
                if name:
                    vision_items.append(name)

        if not vision_items:
            return []

        # Flatten all XML widgets from all screens
        all_xml_widgets: list[dict] = []
        for widgets in by_screen.values():
            all_xml_widgets.extend(widgets)

        # For each Vision item, find the best-matching XML widget
        matched_indices: set[int] = set()
        for vision_title in vision_items:
            best_idx = None
            best_score = 0.0
            for xml_idx, xml_w in enumerate(all_xml_widgets):
                if xml_idx in matched_indices:
                    continue
                score = cls._title_score(vision_title, xml_w.get("title") or "")
                if score > best_score:
                    best_score = score
                    best_idx = xml_idx
            if best_idx is not None and best_score >= 0.15:
                matched_indices.add(best_idx)

        # Return only the matched XML widgets (in original order)
        return [all_xml_widgets[i] for i in sorted(matched_indices)]

    @classmethod
    def _fill_placeholder_data(cls, parsed: Dict[str, Any]) -> Dict[str, Any]:
        """Fill empty series/data with placeholder values so charts can render."""
        import random
        random.seed(42)

        charts = parsed.get("charts")
        if not isinstance(charts, list):
            return parsed

        for chart in charts:
            if not isinstance(chart, dict):
                continue
            chart_type = chart.get("chart_type", "table")

            # Skip types that don't need series data
            if chart_type in ("table", "pivot_table", "image", "big_number"):
                continue

            categories = chart.get("categories") or []
            series = chart.get("series") or []

            # Ensure categories exist
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
                val = s.get("value")
                if val and val != 0:
                    # Has a value but no data array — expand from value
                    pass

            if not all_empty:
                continue

            # Generate placeholder data based on chart type
            if chart_type in ("scatter",):
                # Scatter needs x,y pairs
                if not series:
                    series = [{"name": "Данные", "data": [], "hex_code": "#6366F1"}]
                    chart["series"] = series
                for s in series:
                    if not isinstance(s, dict):
                        continue
                    if not s.get("data") or len(s["data"]) == 0:
                        s["data"] = [
                            {"x": random.randint(10, 90), "y": random.randint(10, 90)}
                            for _ in range(min(num_cats, 12) or 8)
                        ]
            elif chart_type in ("sankey",):
                # Sankey needs from/to/flow — leave as is, handled by frontend
                pass
            elif chart_type in ("gantt",):
                # Gantt needs start/end dates — leave as is
                pass
            else:
                # Bar, line, area, pie, donut, funnel, treemap, radar, etc.
                if not series:
                    series = [{"name": "Значение", "data": [], "hex_code": "#6366F1"}]
                    chart["series"] = series

                for s in series:
                    if not isinstance(s, dict):
                        continue
                    data = s.get("data")
                    if isinstance(data, list) and len(data) > 0:
                        continue
                    val = s.get("value")
                    if val and val != 0:
                        # Use value as basis to generate proportional data
                        base = float(val)
                        s["data"] = [
                            round(base * random.uniform(0.3, 1.2), 1)
                            for _ in range(num_cats)
                        ]
                    else:
                        s["data"] = [
                            round(random.uniform(10, 100), 1)
                            for _ in range(num_cats)
                        ]

        return parsed

    @classmethod
    def _apply_xml_blueprint(
        cls,
        parsed: Dict[str, Any],
        xml_chart_blueprint: List[Dict[str, Any]],
        allowed_set: set[str],
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
                if chart["chart_type"] == "bar" and "bar_horizontal" in allowed_set:
                    chart["chart_type"] = "bar_horizontal"
            if xml_entry.get("is_combo") or chart.get("chart_type") == "combo":
                chart["is_combo"] = True
            if not chart.get("title") and xml_entry.get("title"):
                chart["title"] = xml_entry.get("title")
            if not chart.get("categories"):
                chart["categories"] = cls._default_categories_for_chart(chart.get("chart_type"))
            if not isinstance(chart.get("position"), dict):
                chart["position"] = cls._grid_position(chart_idx)

        parsed["xml_blueprint_chart_count"] = len(xml_chart_blueprint)
        parsed["xml_blueprint_applied_count"] = len(matches)
        return parsed

    @classmethod
    def analyze_dashboard(
        cls,
        image_path: Path,
        table_names: Optional[List[str]] = None,
        xml_plan_path: Optional[Path] = None,
        ocr_dir: Optional[Path] = None,
        prefer_enriched_fallback: bool = True,
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
        allowed_hint = ", ".join(allowed_types)
        xml_hint = f" (из XML {xml_name})" if xml_name else ""
        allowed_set = set(allowed_types)
        # Defer xml_chart_blueprint selection until after Vision analysis (for screen matching)
        xml_chart_blueprint: List[Dict[str, Any]] = []
        xml_kpi_count = 0

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
            parsed = cls._apply_xml_blueprint(parsed, xml_chart_blueprint, allowed_set)
            parsed["allowed_chart_types"] = allowed_types
            parsed["xml_source"] = xml_name
            parsed["prompt"] = "VISION_DISABLED_STUB"
            parsed["raw_response"] = json.dumps(parsed, ensure_ascii=False)
            return parsed

        # Build prompt for Vision LLM
        prompt = (
            "Ты — аналитик BI. Проанализируй скриншот дашборда и верни JSON с описанием.\n\n"
            "Формат результата (строго JSON-объект):\n"
            "{\n"
            "  \"dashboard_title\": \"...\",\n"
            "  \"kpis\": [\n"
            "    {\"name\": \"...\", \"value\": \"...\", \"unit\": \"...\", \"note\": \"...\", "
            "\"position\": {\"left\":0.1,\"top\":0.1,\"width\":0.2,\"height\":0.1}}\n"
            "  ],\n"
            "  \"charts\": [\n"
            "    {\"title\":\"...\",\"chart_type\":\"bar|bar_horizontal|line|area|combo|pie|donut|table|pivot_table|big_number|scatter|sankey|gantt|sunburst|treemap|funnel|radar|country_map|image\",\n"
            "     (bar — вертикальные столбцы, категории по оси X; bar_horizontal — горизонтальные полосы, категории по оси Y)\n"
            "     \"x_axis\":\"...\",\"y_axis\":\"...\",\"categories\":[...],\n"
            "     \"series\":[{\"name\":\"...\",\"data\":[...],\"value\":...,\"hex_code\":\"#RRGGBB\"}],\n"
            "     \"table_hint\":\"...\",\n"
            "     \"position\":{\"left\":0.2,\"top\":0.3,\"width\":0.4,\"height\":0.2}}\n"
            "  ]\n"
            "}\n\n"
            "ВАЖНО ПО ДЕТАЛИЗАЦИИ:\n"
            "- Верни ОТДЕЛЬНЫЙ объект для каждого видимого блока графика/таблицы.\n"
            "- Не объединяй несколько графиков в один элемент.\n"
            "- Не упрощай сложные визуализации до bar/table без явной причины.\n"
            "- Если линейный график имеет закрашенную область под линией (заливка) — это area, не line.\n"
            "- Если несколько кривых или заливок разных цветов — это area с несколькими series.\n\n"
            "ТАБЛИЧНЫЕ БЛОКИ:\n"
            "- Если видишь большую таблицу (много строк/колонок), верни отдельный chart_type=table или pivot_table.\n"
            "- Для таблиц обязательно заполняй categories (заголовки колонок) и series (строки).\n"
            "- Не заменяй табличный блок KPI-карточками.\n\n"
            "КАК ОПРЕДЕЛЯТЬ KPI:\n"
            "- Если на изображении есть текстовая надпись (название) рядом с крупным числом — это KPI.\n"
            "- Обычно KPI выглядят как рамка/карточка с заголовком и большим значением (например: «Показатели 3 555 000», «Клики 250 000»).\n"
            "- KPI могут быть БЕЗ графика — просто название и число. Их НЕ нужно путать с графиками.\n"
            "- КАЖДЫЙ KPI — это ОТДЕЛЬНЫЙ объект в массиве kpis. НЕ группируй несколько KPI в один элемент!\n"
            "  Пример: если на дашборде «Показатели 3 555 000» и «Клики 250 000» — это ДВА отдельных элемента:\n"
            "  [{\"name\":\"Показатели\",\"value\":3555000}, {\"name\":\"Клики\",\"value\":250000}]\n"
            "- Поле value — всегда одно число (не массив).\n\n"
            "ВАЖНО: Для каждого элемента series укажи hex_code — цвет элемента на графике в формате #RRGGBB.\n"
            "Извлекай цвета визуально с изображения. Особенно важно для pie, donut, funnel, bar, line, area.\n\n"
            "PIE/DONUT/FUNNEL — ОСОБЫЕ ПРАВИЛА:\n"
            "- Для pie, donut: categories — это ВСЕ подписи сегментов легенды. series — список сегментов с name=подпись, value=число.\n"
            "  Пример donut с 5 сегментами: categories:[\"Бюджет\",\"Показатели\",\"Клики\",\"Посещения\",\"Ecommerce\"], "
            "series:[{\"name\":\"Бюджет\",\"value\":72},{\"name\":\"Показатели\",\"value\":60},...]\n"
            "- Для funnel: categories — СОХРАНЯЙ ИСХОДНЫЙ ПОРЯДОК этапов воронки (сверху вниз). НЕ сортируй.\n"
            "  series — список этапов с name=этап, value=число в оригинальном порядке.\n\n"
            "ДЕДУПЛИКАЦИЯ KPI:\n"
            "- Каждый KPI добавляй ТОЛЬКО ОДИН РАЗ. НЕ добавляй суффиксы типа «(2)» к уже упомянутым KPI.\n\n"
            f"Допустимые типы графиков{xml_hint}: {allowed_hint}.\n"
            "Координаты позиционирования укажи в долях (0..1).\n"
        )
        if xml_chart_blueprint:
            xml_lines: List[str] = []
            for idx, chart in enumerate(xml_chart_blueprint, start=1):
                title = str(chart.get("title") or f"Элемент {idx}").strip()
                chart_type = str(chart.get("superset_type") or "table").strip()
                flags: List[str] = []
                if chart.get("stacked"):
                    flags.append("stacked")
                if chart.get("is_horizontal"):
                    flags.append("horizontal")
                if chart.get("is_combo") or chart_type == "combo":
                    flags.append("combo")
                suffix = f" [{', '.join(flags)}]" if flags else ""
                xml_lines.append(f"{idx}. {title} -> {chart_type}{suffix}")
            prompt += (
                "\nXML-ЭТАЛОН ДЛЯ МАТЧИНГА (ориентир по сложности и типам):\n"
                f"- Ожидаемое число KPI: {xml_kpi_count}.\n"
                f"- Ожидаемое число графиков: {len(xml_chart_blueprint)}.\n"
                "- Старайся держать порядок и типы графиков как в XML:\n"
                + "\n".join(xml_lines)
                + "\n- Если тип визуально неоднозначен, выбирай тип из XML.\n"
            )
        if table_hint:
            prompt += "\n" + table_hint

        client = get_vision_client()
        model = get_vision_model()
        from .image_compress import compress_image_to_base64
        image_b64, mime = compress_image_to_base64(image_path)

        try:
            response = client.chat.completions.create(
                model=model,
                max_tokens=2000,
                temperature=0.2,
                top_p=0.9,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime};base64,{image_b64}"},
                            },
                        ],
                    }
                ],
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content.strip()
        except Exception as exc:
            logger.warning("Vision API error: %s", exc)
            raw = ""

        parsed = _safe_json_loads(raw) or {}
        charts = parsed.get("charts")
        if isinstance(charts, list):
            for chart in charts:
                if not isinstance(chart, dict):
                    continue
                chart["chart_type"] = cls._coerce_chart_type(chart.get("chart_type"), allowed_set)
        image_data_url = f"data:{mime};base64,{image_b64}"
        table_charts, table_raw = cls._extract_table_charts(client, model, image_data_url, allowed_set)
        parsed = cls._merge_table_charts(parsed, table_charts)

        # Now pick the best XML screen based on Vision results
        if xml_by_screen:
            best_screen_widgets = cls._pick_best_screen(
                xml_by_screen,
                parsed.get("charts") or [],
                parsed.get("kpis") or [],
            )
            if best_screen_widgets:
                xml_chart_blueprint = cls._prepare_xml_chart_blueprint(best_screen_widgets, allowed_set)
                xml_kpi_count = sum(
                    1 for entry in best_screen_widgets
                    if _normalize_chart_type(entry.get("superset_type")) == "big_number"
                )
        elif xml_blueprint:
            xml_chart_blueprint = cls._prepare_xml_chart_blueprint(xml_blueprint, allowed_set)
            xml_kpi_count = sum(
                1 for entry in xml_blueprint
                if _normalize_chart_type(entry.get("superset_type")) == "big_number"
            )

        parsed = cls._apply_xml_blueprint(parsed, xml_chart_blueprint, allowed_set)
        parsed = cls._fill_placeholder_data(parsed)
        parsed["allowed_chart_types"] = allowed_types
        parsed["xml_source"] = xml_name
        parsed["prompt"] = prompt
        parsed["table_prompt_used"] = bool(table_charts)
        parsed["table_raw_response"] = table_raw
        parsed["raw_response"] = raw
        return parsed
