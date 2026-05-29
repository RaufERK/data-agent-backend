"""Navigator widget mappings and matching metrics."""

from __future__ import annotations

import re
from typing import Iterable, Optional

NAVIGATOR_WIDGET_MAP = {
    # 1 - Круговая диаграмма (legacy pie rendered as bar)
    "1": "bar",
    # 2 - Табличный виджет (Grid)
    "2": "table",
    # 3 - График (Chart)
    "3": "line",
    # 4 - Карточка с кругом (Card Round)
    "4": "big_number",
    # 5 - В фокусе (Focus view)
    "5": "big_number",
    # 6 - Водопадная диаграмма (Factor analysis)
    "6": "bar",
    # 7 - Нормированная полосовая диаграмма (Stacked Bar)
    "7": "bar",
    # 8 - Столбчатый табличный виджет (GridBar)
    "8": "table",
    # 9 - Рейтинг по приростам (RatingBar)
    "9": "bar",
    # 10 - Обычная карточка (Card Common)
    "10": "big_number",
    # 11 - Таблица старая (CustomGrid)
    "11": "table",
    # 12 - КПЭ (KPI)
    "12": "big_number",
    # 13 - Гистограмма горизонтальная (ClusteredBar)
    "13": "bar",
    # 14 - Карта (Map)
    "14": "country_map",
    # 15 - Гистограмма (Vertical Bar)
    "15": "bar",
    # 16 - Составная водопадная диаграмма (Composite waterfall)
    "16": "bar",
    # 17 - Составная гистограмма горизонтальная
    "17": "bar",
    # 18 - Вертикальные приросты (Vertical Growth)
    "18": "bar",
    # 19 - Факторный анализ (Factor analysis)
    "19": "bar",
    # 20 - Светофор (Light)
    "20": "big_number",
    # 21 - Легенда (Panel content)
    "21": "table",
    # 22 - Панель фильтров (Panel empty)
    "22": "table",
    # 23 - Текст (Text Box)
    "23": "table",
    # 24 - Диаграмма сравнения периодов
    "24": "bar",
    # 25 - Диаграмма для Опер. Отчета
    "25": "bar",
    # 26 - Дерево с динамикой (Tree Chart)
    "26": "sunburst",
    # 27 - Светофор с логотипом (Company List)
    "27": "big_number",
    # 28 - Светофор с легендой (Light with legend)
    "28": "big_number",
    # 29 - Таблица с барами (TableBar)
    "29": "table",
    # 30 - Графики и бары с кастомной осью X
    "30": "bar",
    # 31 - Гистограмма сегментированная (Vertical fake Bar)
    "31": "bar",
    # 32 - Глоссарий (Popup Widget)
    "32": "table",
    # 33 - Барабан с таблицей (Competition TB/GOSB)
    "33": "table",
    # 34 - Пузырьковая диаграмма (Bubbles chart)
    "34": "bar",
    # 44 - Диаграмма плоское дерево (TreeMap)
    "44": "treemap",
    # 45 - Водопадная диаграмма вертикальная (Waterfall)
    "45": "bar",
    # 51 - Карточка (Card Extended)
    "51": "table",
    # 52 - Календарь (Calendar)
    "52": "table",
    # 58 - Кнопка (Image Button)
    "58": "image",
    # 59 - Новости (News List)
    "59": "table",
    # 60 - Список документов (Financial Reports)
    "60": "table",
    # 62 - Генератор через группировку (Group of card extended)
    "62": "big_number",
    # 63 - Гистограмма иерархическая
    "63:1": "bar",
    "63:2": "pie",
    "63": "bar",
    # 64 - Бар со штриховкой (Hatching bar)
    "64": "bar",
    # 65 - Визитка (Contact Card)
    "65": "table",
    # 66 - Имитатор (Mimic)
    "66": "image",
    # 70 - Спидометр (Progress Indicator)
    "70": "big_number",
    # 71 - Панель с виджетами (Combine Widget)
    "71": "table",
    # 72 - Список (Block List)
    "72": "table",
    # 79 - Таблица старая витрина (Custom Table)
    "79": "table",
    # 80 - Таблица динамическая (DynamicAnalyticReport)
    "80": "table",
    # 81 - Таблица (Custom Table API for Java)
    "81": "table",
    # 82 - Sankey Diagram
    "82": "sankey",
    # 83 - Мозаика (Mosaic widget)
    "83": "country_map",
    # 84 - Круговая диаграмма отладка (Pie)
    "84": "pie",
    # 85 - Универсальный виджет (Universal)
    "85": "table",
    # 86 - Барабан (Sector diagram)
    "86": "pie",
    # 87 - Изображение (Image)
    "87": "image",
    # 88 - Sunburst Diagram
    "88": "sunburst",
    # 89 - Диаграмма (Diagram)
    "89": "bar",
    # 90 - 3D карта (3D Map)
    "90": "country_map",
    # 91 - Диаграмма Воронка (Funnel)
    "91": "funnel",
    # 92 - Диаграмма прогресс бар (Gauge chart)
    "92": "big_number",
    # 94 - Карточка (Card)
    "94": "big_number",
    # 95 - Bar chart race
    "95": "bar_horizontal",
    # 96 - Radar chart
    "96": "radar",
    # 97 - 2D карта (2D Map)
    "97": "country_map",
    # 98 - Круговая диаграмма (Pie Chart)
    "98": "pie",
    # 99 - Диаграмма Ганта (Gantt Chart)
    "99": "gantt",
    # 100 - Web View
    "100": "table",
    # 101 - Изображение (Image)
    "101": "image",
    # 102 - Таблица (Table)
    "102": "table",
    # 103 - Водопад (Waterfall / Факторный анализ)
    "103": "bar",
    # 104 - Разделитель (Separator)
    "104": "table",
    # 105 - Глоссарий (Descriptions)
    "105": "table",
    # 106 - Легенда (Legend)
    "106": "table",
    # 107 - Список документов (Document list)
    "107": "table",
    # 108 - Документ (Document)
    "108": "table",
    # 109 - Сектограмма (Drum Chart)
    "109": "pie",
    # 110 - Sankey Diagram
    "110": "sankey",
    # 111 - Структура холдинга (Structure graph)
    "111": "sunburst",
    # 112 - Визитка (Contact Card)
    "112": "table",
    # 113 - Светофор с логотипом (Company List)
    "113": "big_number",
    # 114 - Светофор (Light)
    "114": "big_number",
    # 115 - Мозаика (Mosaic widget)
    "115": "country_map",
    # 116 - Календарь (Calendar)
    "116": "table",
    # 118 - Векторный виджет (Vector)
    "118": "image",
    # 119 - Круговой прогресс (Round progress)
    "119": "big_number",
    # 121 - Структура холдинга (Structure Graph)
    "121": "sunburst",
    # 122 - Кнопка (Button)
    "122": "image",
    # 123 - Конструктор (Constructor) — JS-виджет, тип определяется по title
    "123": "table",
    # 124 - Кнопка v2 (Button v2)
    "124": "image",
    # 125 - Сводная таблица (Pivot Table)
    "125": "pivot_table",
    # 126 - Плагин (Plugin)
    "126": "table",
}

NAVIGATOR_TITLE_MAP = [
    # --- Highest priority: "таблица" prefix must win over partial matches ---
    {"needles": ("таблица ",), "superset_type": "table"},
    # --- Specific exotic types (must be before generic matches) ---
    {"needles": ("candlestick", "свечн"), "superset_type": "bar"},
    {"needles": ("quadrant", "квадрант"), "superset_type": "scatter"},
    {"needles": ("bubble chart", "bubble", "пузырьк"), "superset_type": "scatter"},
    {"needles": ("radial bubble",), "superset_type": "scatter"},
    {"needles": ("pictorial", "пиктограмм"), "superset_type": "big_number"},
    {"needles": ("thermometer", "термометр"), "superset_type": "big_number"},
    {"needles": ("mosaic map", "мозаик"), "superset_type": "country_map"},
    {"needles": ("race chart",), "superset_type": "bar_horizontal"},
    {"needles": ("timeline", "таймлайн", "time line"), "superset_type": "gantt"},
    # --- Standard chart types ---
    {"needles": ("sunburst", "санберст"), "superset_type": "sunburst"},
    {"needles": ("treemap", "tree map", "плоское дерево"), "superset_type": "treemap"},
    {"needles": ("воронк", "funnel"), "superset_type": "funnel"},
    {"needles": ("радар", "radar"), "superset_type": "radar"},
    {"needles": ("санки", "sankey"), "superset_type": "sankey"},
    {"needles": ("гант", "gantt"), "superset_type": "gantt"},
    {"needles": ("2d карта", "карта региона", "карта рф", "country_map"), "superset_type": "country_map"},
    {"needles": ("сект", "pie", "donut"), "superset_type": "pie"},
    {"needles": ("круг",), "superset_type": "pie", "exclude": ("прогресс",)},
    {"needles": ("гист", "hist", "бар"), "superset_type": "bar"},
    {"needles": ("таблиц", "table", "автотаблиц"), "superset_type": "table"},
    {"needles": ("прогресс", "progress", "gauge", "изогнутый бар", "прямой бар"), "superset_type": "big_number"},
    {"needles": ("race",), "superset_type": "bar_horizontal"},
    # --- Fallback generic matches (lowest priority) ---
    {"needles": ("карта", "map"), "superset_type": "country_map"},
    {"needles": ("bar",), "superset_type": "bar"},
    {"needles": ("иерарх",), "superset_type": "sunburst"},
]

TITLE_KEYS = ("title", "slice_name", "name", "label")


def _normalize_title(value: Optional[str]) -> str:
    if not value:
        return ""
    cleaned = re.sub(r"\s+", " ", str(value).strip().lower())
    return cleaned


def chart_type_by_title(title: Optional[str]) -> Optional[str]:
    title_norm = _normalize_title(title)
    if not title_norm:
        return None
    for entry in NAVIGATOR_TITLE_MAP:
        needles = entry.get("needles") or ()
        excludes = entry.get("exclude") or ()
        if any(needle in title_norm for needle in needles):
            if excludes and any(exc in title_norm for exc in excludes):
                continue
            return entry.get("superset_type")
    return None


def parse_navigator_descriptor(raw_value: Optional[str]) -> tuple[Optional[str], Optional[str], str]:
    raw = str(raw_value or "").strip()
    if not raw:
        return None, None, ""
    lower = raw.lower()
    if not (lower.startswith("nav:") or lower.startswith("navigator:")):
        return None, None, ""
    parts = raw.split(":")
    widget_type = parts[1] if len(parts) > 1 else None
    visualization_type = None
    title_parts = []
    if len(parts) >= 4:
        visualization_type = parts[2]
        title_parts = parts[3:]
    elif len(parts) >= 3:
        if parts[2].isdigit():
            visualization_type = parts[2]
        else:
            title_parts = parts[2:]
    title = ":".join(title_parts).strip()
    return widget_type, visualization_type, title


def map_navigator_chart_type(
    widget_type: Optional[str],
    visualization_type: Optional[str],
    title: Optional[str] = None,
) -> str:
    widget_type = (widget_type or "").strip()
    visualization_type = (visualization_type or "").strip()
    if widget_type and visualization_type:
        key = f"{widget_type}:{visualization_type}"
        if key in NAVIGATOR_WIDGET_MAP:
            return NAVIGATOR_WIDGET_MAP[key]
    title_hint = chart_type_by_title(title)
    if title_hint:
        return title_hint
    if widget_type and widget_type in NAVIGATOR_WIDGET_MAP:
        return NAVIGATOR_WIDGET_MAP[widget_type]
    return "auto"


def chart_match_rate(widgets: Iterable[dict]) -> float:
    total = 0
    matched = 0
    for widget in widgets or []:
        if not isinstance(widget, dict):
            continue
        total += 1
        mapped = map_navigator_chart_type(
            widget.get("widget_type"),
            widget.get("visualization_type"),
            widget.get("title"),
        )
        if mapped != "auto":
            matched += 1
    if total == 0:
        return 0.0
    return matched / total


def _entry_title(entry: dict, title_key: Optional[str] = None) -> str:
    if title_key:
        value = entry.get(title_key)
        if value:
            return _normalize_title(value)
    for key in TITLE_KEYS:
        value = entry.get(key)
        if value:
            return _normalize_title(value)
    return ""


def _bbox(entry: dict) -> Optional[tuple[float, float, float, float]]:
    left = entry.get("left", entry.get("x", 0))
    top = entry.get("top", entry.get("y", 0))
    width = entry.get("width", entry.get("w", 0))
    height = entry.get("height", entry.get("h", 0))
    try:
        left = float(left)
        top = float(top)
        width = float(width)
        height = float(height)
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return left, top, left + width, top + height


def _bbox_iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    inter_w = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    inter_h = max(0.0, min(ay2, by2) - max(ay1, by1))
    inter_area = inter_w * inter_h
    if inter_area <= 0:
        return 0.0
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union_area = area_a + area_b - inter_area
    if union_area <= 0:
        return 0.0
    return inter_area / union_area


def layout_fidelity(
    expected: Iterable[dict],
    actual: Iterable[dict],
    title_key: Optional[str] = None,
) -> float:
    expected_list = [entry for entry in (expected or []) if isinstance(entry, dict)]
    actual_list = [entry for entry in (actual or []) if isinstance(entry, dict)]
    if not expected_list:
        return 0.0
    actual_map = {
        _entry_title(entry, title_key): entry
        for entry in actual_list
        if _entry_title(entry, title_key)
    }
    scores = []
    for entry in expected_list:
        title = _entry_title(entry, title_key)
        if not title:
            scores.append(0.0)
            continue
        expected_box = _bbox(entry)
        actual_entry = actual_map.get(title)
        if not actual_entry:
            scores.append(0.0)
            continue
        actual_box = _bbox(actual_entry)
        if not expected_box or not actual_box:
            scores.append(0.0)
            continue
        scores.append(_bbox_iou(expected_box, actual_box))
    return sum(scores) / len(scores)
