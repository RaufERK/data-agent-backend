"""LLM prompt builders and inventory normalization mixin for DashboardVisionService."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


class _PromptsMixin:
    """Methods that build LLM prompts and normalize/apply inventory payloads."""

    def _build_table_extraction_prompt(cls, inventory: Optional[Dict[str, Any]] = None) -> str:
        inventory_blocks = [block for block in ((inventory or {}).get("blocks") or []) if isinstance(block, dict)]
        inventory_tables = [
            block
            for block in inventory_blocks
            if cls._block_kind_from_type(block.get("block_kind") or block.get("chart_type")) == "table"
        ]
        inventory_tables_hint = json.dumps(
            [
                {
                    "block_id": block.get("block_id"),
                    "title": block.get("title"),
                    "position": block.get("position"),
                }
                for block in inventory_tables
            ],
            ensure_ascii=False,
        )[:4000]
        return (
            "Ты анализируешь только ТАБЛИЧНЫЕ блоки на скриншоте BI-дашборда.\n"
            "Найди все большие таблицы (включая иерархические/групповые), даже если рядом есть KPI и графики.\n"
            "Если строки не читаются полностью, все равно верни сам табличный блок с title/position и пустыми rows.\n"
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
            "- Для каждой таблицы верни КАК МОЖНО БОЛЬШЕ строк — минимум 8, максимум все видимые.\n"
            "- Для иерархических таблиц (с группами/подгруппами): включай строки-заголовки групп и все подстроки.\n"
            "  Обозначай уровень вложенности пробелами или префиксом '  ' в первой колонке.\n"
            "- Сохраняй оригинальные заголовки колонок.\n"
            "- Для таблиц с МНОГОУРОВНЕВЫМИ ЗАГОЛОВКАМИ (когда одна строка заголовка объединяет несколько колонок): "
            "создавай одну колонку на каждую конечную ячейку, объединяя родительский и дочерний заголовок через ' / '. "
            "Пример: если '2026 г.' охватывает 'Факт', 'План', 'Δ' — создавай колонки '2026 г. / Факт', '2026 г. / План', '2026 г. / Δ'. "
            "НЕ создавай одну колонку '2026 г.' со значениями 'Факт', '0,69' — это неправильно.\n"
            "- Читай числовые значения точно — включая знак (минус/плюс) и единицы измерения.\n"
            "- Если в ячейках таблицы видны цветные маркеры, кружки, точки, галочки, крестики, heatmap/status-индикаторы "
            "или progress bars, НЕ оставляй такие ячейки пустыми. Записывай их как текстовые значения: "
            "\"🟢\", \"🟠\", \"🔴\", \"✓\", \"✕\", \"91%\" или \"91% ███░\". "
            "Если цвет виден, сохраняй его смысл: green/зелёный=🟢, orange/yellow/оранжевый/жёлтый=🟠, red/красный=🔴.\n"
            "- Для статусных матриц, где большинство числовых ячеек пустые, но есть цветные точки/маркеры, "
            "всё равно возвращай все видимые столбцы и строки; маркеры являются данными.\n"
            "- Не добавляй KPI и не табличные графики.\n"
            "- Если таблиц нет, верни {\"tables\":[]}.\n"
            + (
                f"\nInventory table blocks (hint, may be incomplete):\n{inventory_tables_hint}\n"
                if inventory_tables else ""
            )
        )

    @classmethod
    def _postprocess_extracted_table_charts(
        cls,
        table_charts: List[Dict[str, Any]],
        inventory: Optional[Dict[str, Any]],
        allowed_set: set[str],
    ) -> List[Dict[str, Any]]:
        filtered_tables = cls._filter_extracted_table_charts_by_inventory(table_charts, inventory)
        return cls._fill_table_placeholders_from_inventory(filtered_tables, inventory, allowed_set)

    @classmethod
    def _build_inventory_prompt(
        cls,
        allowed_hint: str,
        widget_catalog_text: str,
    ) -> str:
        return (
            "Ты анализируешь только структуру BI-дашборда на изображении.\n"
            "Твоя задача: перечислить ВСЕ видимые блоки и НЕ выдумывать данные.\n"
            "Верни только JSON:\n"
            "{\n"
            '  "dashboard_title": "...",\n'
            '  "blocks": [\n'
            "    {\n"
            '      "block_id": "b1",\n'
            '      "title": "...",\n'
            '      "block_kind": "kpi|chart|table|map|gauge|filter|legend|text",\n'
            f'      "chart_type": "one_of({allowed_hint}, gauge, progress, filter, mosaic_map, legend, text, unknown)",\n'
            '      "position": {"left":0.0,"top":0.0,"width":1.0,"height":1.0},\n'
            '      "confidence": 0.0\n'
            "    }\n"
            "  ]\n"
            "}\n"
            "Правила:\n"
            "- Никаких series/data/rows/value на этом шаге.\n"
            "- Не объединяй соседние блоки.\n"
            "- КРИТИЧЕСКИ ВАЖНО: Если видно крупное число (выручка, сумма, количество, баллы, процент и т.п.) "
            "с подписью — это KPI. Ставь block_kind=kpi, chart_type=big_number.\n"
            "- Если KPI-карточки расположены в ряд (горизонтальная полоса с числами), "
            "каждую карточку выдели как ОТДЕЛЬНЫЙ блок с block_kind=kpi.\n"
            "- Если один контейнер содержит общий заголовок и НЕСКОЛЬКО отдельных чисел с подписями "
            "(например Москва/РФ, Прием/Выдача/Отказы/Консультации), верни ОТДЕЛЬНЫЙ block_kind=kpi "
            "для каждого числа. Общий заголовок контейнера НЕ является отдельным KPI-блоком.\n"
            "- ВАЖНО: если внутри KPI-карточки видна мини-гистограмма (sparkline bar) или мини-график (sparkline line), "
            "это НЕ отдельный chart-блок. Это часть KPI. Оставляй один блок block_kind=kpi. "
            "НЕ создавай отдельный chart-блок для sparkline внутри KPI.\n"
            "- Если внутри панели только текстовые подписи и 2-4 значения без осей и без табличной сетки, "
            "это набор KPI, а не text/table.\n"
            "- Фильтры (выпадающие списки, date picker, сегмент-кнопки, чекбоксы выбора периода/категории) — "
            "ВАЖНЫЕ виджеты дашборда. Возвращай их как block_kind=filter, chart_type=filter. "
            "Для каждого отдельного фильтра — отдельный блок с title = подпись фильтра (например 'Период', 'Регион', 'Тип').\n"
            "- НЕ возвращай как blocks: иконки, меню, toolbar, breadcrumb, заголовки разделов без данных.\n"
            "- Возвращай все крупные содержательные виджеты дашборда, включая фильтры.\n"
            "- Если видно стадию/этап воронки с числом и процентом — это тоже KPI.\n"
            "- Если виден прогресс/спидометр/дуга/термометр, верни block_kind=gauge.\n"
            "- Если виден свечной/биржевой график (вертикальные прямоугольники с усами — open/high/low/close), верни chart_type=candlestick.\n"
            "- Если есть отдельная легенда, верни block_kind=legend.\n"
            "- Если не уверен в типе, оставь chart_type=unknown.\n"
            "- Лучше перечислить больше блоков, чем пропустить существующие.\n"
            "\nКаталог допустимых widget families целевой BI:\n"
            f"{widget_catalog_text}\n"
        )

    @classmethod
    def _build_detail_prompt(
        cls,
        allowed_hint: str,
        widget_catalog_text: str,
        inventory: Dict[str, Any],
    ) -> str:
        return (
            "Ты анализируешь BI-дашборд по изображению и инвентаризации блоков.\n"
            "Нужно извлечь KPI и графики. Используй inventory как список блоков, но при необходимости исправь только тип внутри блока.\n"
            "Верни строго JSON:\n"
            "{\n"
            '  "dashboard_title": "...",\n'
            '  "kpis": [{"name":"...","value":"...","unit":"...","note":"...","breakdown":[{"label":"...","value":"..."}],"widget_family":"...","sparkline":[],"sparkline_type":"bar|line","confidence":0.0,"position":{"left":0.0,"top":0.0,"width":1.0,"height":1.0}}],\n'
            '  "charts": [{"title":"...","chart_type":"...","x_axis":"...","y_axis":"...","categories":[],"series":[],"table_hint":"","widget_family":"...","legend_items":[],"small_labels":[],"confidence":0.0,"position":{"left":0.0,"top":0.0,"width":1.0,"height":1.0}}],\n'
            '  "omitted_blocks": [{"block_id":"...","reason":"..."}]\n'
            "}\n"
            "Правила:\n"
            "- Не выдумывай скрытые данные.\n"
            "- ВАЖНО: title графика — это текст-заголовок над графиком или рядом с ним. "
            "Если над графиком есть текст вроде 'Отработано в среднем часов' — это title. "
            "НЕ путай title с y_axis. y_axis — это подпись шкалы Y (обычно вертикальная). "
            "title — это крупный заголовок виджета.\n"
            "- ВАЖНО: Для line/bar/area графиков ОБЯЗАТЕЛЬНО извлекай series и categories.\n"
            "  categories — это подписи оси X (месяцы, даты, названия, регионы и т.п.). "
            "ВСЕГДА заполняй categories[], даже если подписи не читаются — придумай разумные на основе контекста (например месяцы, регионы).\n"
            "  series — массив именованных рядов с цветами: [{\"name\":\"план CVM\",\"data\":[100,200,300],\"hex_code\":\"#8b5cf6\"},{\"name\":\"факт CVM\",\"data\":[90,180,310],\"hex_code\":\"#06b6d4\"}].\n"
            "  КРИТИЧЕСКИ ВАЖНО: даже если точные числа не читаются, оценивай высоту столбцов / положение точек на графике ВИЗУАЛЬНО "
            "и возвращай приблизительные значения. Никогда не оставляй data=[] если на графике есть видимые столбцы или линии.\n"
            "  Если видишь горизонтальные составные полосы (stacked) со статусами в легенде:\n"
            "    chart_type='bar_horizontal', stacked=true\n"
            "    categories — ВСЕ подписи строк слева (каждая строка — отдельный элемент)\n"
            "    series — КАЖДЫЙ статус из легенды как отдельный элемент с hex_code и data по каждой строке\n"
            "    Пример для 3 статусов и 5 строк:\n"
            "    series=[\n"
            "      {\"name\":\"Высокие баллы\",\"hex_code\":\"#22c55e\",\"data\":[2,15,3,5,10]},\n"
            "      {\"name\":\"Средние баллы\",\"hex_code\":\"#f59e0b\",\"data\":[5,3,4,2,8]},\n"
            "      {\"name\":\"Низкие баллы\",\"hex_code\":\"#ef4444\",\"data\":[1,2,1,3,2]}\n"
            "    ]\n"
            "    ВАЖНО: если точные числа не читаются — оцени относительные доли сегментов визуально и верни приблизительные числа. "
            "Если совсем невозможно оценить — верни data=[] (пустой массив), но НЕ пропускай серию совсем.\n"
            "    НЕ возвращай stacked bar как таблицу.\n"
            "  Если видишь простые горизонтальные полосы по категориям (не stacked), chart_type='bar_horizontal', "
            "categories — подписи строк слева, series — один показатель с data по каждой строке. "
            "НЕ возвращай такой визуальный блок как table.\n"
            "  Если внутри блока круговая (pie) или кольцевая (donut) диаграмма:\n"
            "    chart_type='pie' или 'donut' по видимой геометрии (donut = есть отверстие в центре)\n"
            "    series — КАЖДЫЙ сегмент как отдельный элемент с name, value (процент или абсолютное), hex_code\n"
            "    legend_items — то же самое из легенды\n"
            "    Пример: series=[{\"name\":\"Новый\",\"value\":39,\"hex_code\":\"#3b82f6\"},"
            "{\"name\":\"На согласовании\",\"value\":22,\"hex_code\":\"#f59e0b\"},"
            "{\"name\":\"Подписан\",\"value\":15,\"hex_code\":\"#22c55e\"}]\n"
            "    ВАЖНО: если подписи с числами есть — используй их как value.\n"
            "    Если подписей нет — оцени размер каждого сегмента визуально и верни приблизительный процент (сумма ≈ 100).\n"
            "    Только если сегмент совсем не виден — ставь value=null.\n"
            "  Если на графике несколько линий разных цветов — это РАЗНЫЕ серии, каждая со своими данными.\n"
            "    НЕ копируй одни и те же data во все серии — у каждой линии своя траектория.\n"
            "    Читай значения каждой линии независимо по шкале Y.\n"
            "  Если есть легенда (например '◇ план CVM  ○ факт CVM'), используй имена из легенды как name серий.\n"
            "  Для каждой series обязательно укажи hex_code — это цвет САМОЙ ЛИНИИ или СТОЛБЦА, не фона.\n"
            "    hex_code должен быть читаемым на тёмном фоне — не тёмнее #404040.\n"
            "    Если линия синяя — например #3b82f6, оранжевая — #f59e0b, зелёная — #22c55e.\n"
            "  ВАЖНО: если на столбцах/линиях видны числовые подписи (data labels) — это ТОЧНЫЕ значения. "
            "Используй их как data в series. Например, если на трёх столбцах написано 5,9 / 6,0 / 5,7, "
            "то series=[{\"name\":\"Значение\",\"data\":[5.9,6.0,5.7]}]. Запятая в числах — это десятичный разделитель.\n"
            "  Если числовых подписей нет, считывай значения по шкале оси Y приблизительно — допускается погрешность 20-30%.\n"
            "  Предпочитай приблизительные значения пустым data=[]. Пустой data=[] допустим только если "
            "столбцы/линии совсем не видны или виджет обрезан.\n"
            "  Даже если точные data не читаются, ОБЯЗАТЕЛЬНО заполняй legend_items именами серий из легенды.\n"
            "- Если значения совсем не читаются, оставляй data=[] в series, но имена серий и legend_items и categories ОБЯЗАТЕЛЬНО заполняй.\n"
            "- КРИТИЧЕСКИ ВАЖНО: каждый блок с block_kind=kpi из inventory ДОЛЖЕН стать элементом kpis[].\n"
            "  Прочитай крупное число и единицу измерения. Если число не читается, поставь value=null, но блок НЕ пропускай.\n"
            "- Если inventory указывает на группу KPI внутри одного контейнера, верни отдельный kpi для каждого label+value. "
            "Общий заголовок контейнера (например 'Виды услуг', 'Ноябрь 2025 года') не должен становиться отдельным KPI, "
            "если внутри уже есть дочерние показатели.\n"
            "- Если в панели 2-4 текстовых значения сравнивают сущности/регионы без осей, "
            "используй подписи рядом с числами как name KPI и верни каждую пару как отдельный kpi.\n"
            "- Если внутри KPI-карточки есть мини-график (sparkline): маленькая гистограмма или линия под числом — "
            "извлеки значения в поле sparkline (массив чисел) и укажи sparkline_type ('bar' или 'line'). "
            "НЕ создавай для sparkline отдельный chart. Пример: {\"name\":\"Доход\",\"value\":752800,\"unit\":\"₽\","
            "\"note\":\"2,23%\",\"sparkline\":[600,650,700,752],\"sparkline_type\":\"bar\"}.\n"
            "- Если внутри KPI-карточки есть дополнительные пары label+value (например 'Квартиры 67,32 млн ₽', "
            "'Коммерч. помещения 135,06 млн ₽'), сохрани их в kpi.breakdown в визуальном порядке.\n"
            "- Если в inventory есть блок с block_kind=kpi, но ты видишь на картинке что это на самом деле "
            "график или таблица — перемести его в charts[], но НИКОГДА не удаляй и не пропускай.\n"
            "- KPI возвращай только как отдельные kpis.\n"
            "- Не превращай gauge/progress в KPI, если видна дуга, шкала или сегменты.\n"
            "- Для gauge/progress: обязательно прочитай числовое значение (%, число или метку) и помести его в "
            "series=[{\"name\":\"Значение\",\"data\":[X]}]. Например, если видно 84% — data=[84]. Не оставляй series=[].\n"
            "- Для pie/donut/funnel/radar/treemap/sunburst возвращай legend_items, если легенда читается; "
            "series должен содержать элементы сегментов с name/value/hex_code в визуальном порядке.\n"
            "- Для sankey: categories — список всех узлов (labels потоков), series — список рёбер в формате "
            "[{\"name\":\"Источник → Цель\",\"data\":[вес]}]. Если подписи видны — используй их.\n"
            "- Для candlestick/свечного графика: chart_type=\"candlestick\", categories — временны́е метки (метки оси X), "
            "series — ровно 4 элемента: [{\"name\":\"open\",\"data\":[...]},{\"name\":\"high\",\"data\":[...]},{\"name\":\"low\",\"data\":[...]},{\"name\":\"close\",\"data\":[...]}]. "
            "Прочитай хотя бы 5–10 свечей; если значение не читается, оставь null. Не сворачивай в одну серию.\n"
            "- Для line/bar/area/combo возвращай x_axis/y_axis и visible small_labels, если они читаются.\n"
            "  small_labels — подписи значений на столбцах/точках. Если они есть, они должны совпадать с data в series.\n"
            "- chart_type выбирай только из допустимых target-типов. widget_family может быть точнее и ссылаться на каталог BI.\n"
            "- Количество элементов в kpis[] + charts[] должно быть >= количеству блоков в inventory.\n"
            f"\nДопустимые target chart types: {allowed_hint}.\n"
            "Inventory blocks:\n"
            f"{json.dumps(inventory, ensure_ascii=False)[:12000]}\n"
            "\nКаталог widget families BI:\n"
            f"{widget_catalog_text}\n"
        )

    @classmethod
    def _build_normalization_prompt(
        cls,
        allowed_hint: str,
        widget_catalog_text: str,
        inventory: Dict[str, Any],
        detail: Dict[str, Any],
        table_charts: List[Dict[str, Any]],
    ) -> str:
        return (
            "Ты нормализуешь JSON описания BI-дашборда под целевую BI-систему.\n"
            "Это текстовый шаг. На входе inventory блоков, detail extraction и table extraction.\n"
            "XML используется как каталог widget families, а не как экран-шаблон.\n"
            "Верни только JSON:\n"
            "{\n"
            '  "dashboard_title": "...",\n'
            '  "kpis": [{"name":"...","value":"...","unit":"...","note":"...","breakdown":[{"label":"...","value":"..."}],"widget_family":"...","sparkline":[],"sparkline_type":"bar|line","confidence":0.0,"position":{"left":0.0,"top":0.0,"width":1.0,"height":1.0}}],\n'
            '  "charts": [{"title":"...","chart_type":"...","x_axis":"...","y_axis":"...","categories":[],"series":[],"rows":[],"table_hint":"","widget_family":"...","legend_items":[],"small_labels":[],"confidence":0.0,"position":{"left":0.0,"top":0.0,"width":1.0,"height":1.0}}],\n'
            '  "normalization_diagnostics": {"warnings":[],"block_count_expected":0,"block_count_output":0,"unmapped_blocks":[]}\n'
            "}\n"
            "Правила:\n"
            f"- chart_type только из: {allowed_hint}.\n"
            "- widget_family должен быть выбран из каталога BI, если есть разумное совпадение; иначе unknown.\n"
            "- Не выдумывай числа и строки, которых нет в detail/table extraction.\n"
            "- Сохраняй количество крупных блоков и их позиции максимально близко к inventory.\n"
            "- КРИТИЧЕСКИ ВАЖНО: все KPI из detail extraction ДОЛЖНЫ быть в kpis[]. НЕ удаляй, НЕ объединяй, НЕ превращай KPI в таблицы.\n"
            "- Если в detail есть kpi, но ты не уверен в значении — сохрани с value=null, но НЕ пропускай.\n"
            "- Если блок похож на progress/gauge/mosaic/exotic widget, сохраняй это в widget_family, даже если chart_type приходится упростить.\n"
            "- Для таблиц предпочитай rows из table extraction.\n"
            "- КРИТИЧЕСКИ ВАЖНО: для каждого chart из detail extraction ОБЯЗАТЕЛЬНО перенеси поля categories[] и series[] ПОЛНОСТЬЮ.\n"
            "  categories[] — это массив строк (подписи оси X или строк). Если в detail categories=[...], скопируй ВЕСЬ массив.\n"
            "  series[] — это массив объектов {name, data, hex_code}. Если в detail series имеет data=[...числа...], "
            "скопируй data БЕЗ ИЗМЕНЕНИЙ. НИКОГДА не заменяй data=[1,2,3] на data=[].\n"
            "  Если в detail series[i].data содержит числа — это ТОЧНЫЕ ЗНАЧЕНИЯ с изображения. Перенеси их без изменений.\n"
            "  Если в detail series[i].data=[] (пустой) — оставь data=[], НО name и hex_code сохрани.\n"
            "- ВАЖНО: сохраняй series[].hex_code, legend_items[].hex_code и color из detail extraction без замены на дефолтную палитру.\n"
            "- Для составных горизонтальных полос сохраняй chart_type='bar_horizontal', stacked=true, categories и все серии из легенды.\n"
            "- Если inventory содержит chart_type=candlestick — ОБЯЗАТЕЛЬНО сохрани chart_type='candlestick'. НЕ меняй на bar/line.\n"
            "  Для candlestick series ДОЛЖНЫ содержать 4 элемента: open, high, low, close — каждый с data=[числа по свечам].\n"
            "- КРИТИЧЕСКИ ВАЖНО для pie/donut: если в detail есть несколько сегментов в series — перенеси ВСЕ сегменты. "
            "НЕ объединяй и НЕ сворачивай сегменты в один. Если в detail series=[...N сегментов...], "
            "в output тоже должно быть N сегментов в series. Каждый сегмент — отдельный элемент с name, value, hex_code. "
            "Если не уверен в value — ставь value=null, но НЕ удаляй сегмент и НЕ заменяй все сегменты на один с value=100.\n"
            "- КРИТИЧЕСКИ ВАЖНО для pie/donut: legend_items[] должен совпадать по количеству с series[]. "
            "Если в detail legend_items содержит больше записей чем series — перенеси все записи legend_items в series тоже.\n"
            "- Если в kpis[] есть sparkline — сохраняй его без изменений.\n"
            "- Если в kpis[] есть breakdown — сохраняй его без изменений.\n"
            "- ИТОГОВАЯ ПРОВЕРКА: для каждого chart убедись, что categories[] не пустой и series[] не пустой.\n"
            "  Если categories=[] и в detail categories не пустой — ты допустил ошибку, исправь.\n"
            "  Если series=[] и в detail series не пустой — ты допустил ошибку, исправь.\n"
            "\nInventory:\n"
            f"{json.dumps(inventory, ensure_ascii=False)[:12000]}\n"
            "\nDetail extraction:\n"
            f"{json.dumps(detail, ensure_ascii=False)[:16000]}\n"
            "\nTable extraction:\n"
            f"{json.dumps(table_charts, ensure_ascii=False)[:12000]}\n"
            "\nКаталог widget families BI:\n"
            f"{widget_catalog_text}\n"
        )

    @classmethod
    def _normalize_inventory_payload(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            return {"dashboard_title": "", "blocks": []}
        normalized = {"dashboard_title": str(payload.get("dashboard_title") or "").strip(), "blocks": []}
        blocks = payload.get("blocks")
        if not isinstance(blocks, list):
            return normalized
        for idx, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            try:
                confidence = max(0.0, min(1.0, float(block.get("confidence") or 0.0)))
            except (TypeError, ValueError):
                confidence = 0.0
            normalized["blocks"].append(
                {
                    "block_id": str(block.get("block_id") or f"b{idx + 1}"),
                    "title": str(block.get("title") or "").strip(),
                    "block_kind": cls._block_kind_from_type(block.get("block_kind") or block.get("chart_type")),
                    "chart_type": str(block.get("chart_type") or "unknown").strip().lower() or "unknown",
                    "position": cls._normalize_position(block.get("position"), idx),
                    "confidence": confidence,
                }
            )
        return normalized

    @classmethod
    def _apply_inventory_positions(cls, parsed: Dict[str, Any], inventory: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(parsed, dict):
            return parsed
        blocks = inventory.get("blocks") if isinstance(inventory, dict) else []
        if not isinstance(blocks, list) or not blocks:
            return parsed
        block_lookup: Dict[str, Dict[str, Any]] = {}
        for block in blocks:
            if not isinstance(block, dict):
                continue
            key = str(block.get("block_id") or "").strip()
            if key:
                block_lookup[key] = block

        def _apply(items: List[Dict[str, Any]], field: str) -> None:
            for idx, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                block_id = str(item.get("block_id") or "").strip()
                matched = block_lookup.get(block_id)
                if not matched:
                    title = str(item.get(field) or item.get("title") or "").strip()
                    best_score = 0.0
                    for candidate in blocks:
                        if not isinstance(candidate, dict):
                            continue
                        current_score = cls._title_score(title, candidate.get("title"))
                        if current_score > best_score:
                            best_score = current_score
                            matched = candidate
                    if best_score < 0.18:
                        matched = None
                if matched:
                    item["position"] = cls._normalize_position(matched.get("position"), idx)
                    if "confidence" not in item and matched.get("confidence") is not None:
                        try:
                            item["confidence"] = max(0.0, min(1.0, float(matched.get("confidence") or 0.0)))
                        except (TypeError, ValueError):
                            pass

        kpis = parsed.get("kpis")
        if isinstance(kpis, list):
            _apply(kpis, "name")
        charts = parsed.get("charts")
        if isinstance(charts, list):
            _apply(charts, "title")
        return parsed
