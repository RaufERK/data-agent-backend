"""
Определение 5 тест-наборов для eval pipeline.

Каждый набор — это список изображений из /home/user-tot/Desktop/pannels/gold_dash/дэши/
и папка для результатов.

Наборы:
  A — Идеальные (score 10): regression guard, эталон
  B — Почти идеальные (score 8): фикс цвета/фон
  C — KPI пустые (score 6): фикс данных в KPI-блоки
  D — Сложные (много виджетов, score 6): фикс потерь при detection
  E — Простые (score 8, мало виджетов): smoke test

Запуск конкретного набора:
    cd /home/user-tot/Desktop/data_agent
    .venv/bin/python scripts/run_eval_set_a.py
    .venv/bin/python scripts/run_eval_set_b.py
    ...

Или напрямую через baseline_gold_eval.py:
    .venv/bin/python scripts/baseline_gold_eval.py \
        --images 21.png 2.png \
        --output eval_results/my_run
"""

IMAGES_DIR = "/home/user-tot/Desktop/pannels/gold_dash/дэши"

EVAL_SETS = {
    "A": {
        "name": "Идеальные — regression guard",
        "description": (
            "Кейсы со score 10. Эталон: после любых изменений кода эти кейсы "
            "должны сохранять score >= 9. Регрессия здесь — красный флаг."
        ),
        "target_score": 10,
        "fix_focus": "regression guard — ничего не ломать",
        "images": [
            "21.png",          # score 10 — пиксельное совпадение
            "16.png",          # score 6 в v2, но score 10 в baseline_gold_v3 — хороший кандидат
            "photo_2026-02-26_16-58-23.jpg",  # score 8 стабильно
            "image002.png",    # score 8 стабильно
            "28.png",          # score 8 стабильно, мало виджетов
        ],
        "output": "eval_results/set_a_ideal",
    },
    "B": {
        "name": "Почти идеальные — фикс цвета/фон",
        "description": (
            "Кейсы со score 8: структура и layout правильные, "
            "проблема только в цветах, фоне, контрасте. "
            "Цель: довести до score 9-10 через правильную передачу цветовой схемы."
        ),
        "target_score": 10,
        "fix_focus": "цвета, фон, контраст — вытащить из оригинала и передать в Navigator",
        "images": [
            "2.png",   # score 8, issue: цвет/фон
            "4.png",   # score 8, issue: цвет/фон
            "8.png",   # score 8, issue: детали виджетов + KPI
            "13.png",  # score 8, issue: KPI-числа + легенды
            "25.png",  # score 8, issue: цвет/фон
        ],
        "output": "eval_results/set_b_colors",
    },
    "C": {
        "name": "KPI пустые — фикс данных в KPI-блоки",
        "description": (
            "Кейсы со score 6, где основная проблема — пустые или нулевые KPI-числа. "
            "Структура виджетов правильная, но значения не попадают в Navigator. "
            "Цель: довести KPI fill rate до 100%."
        ),
        "target_score": 8,
        "fix_focus": "данные в KPI-блоки — числа, единицы, delta",
        "images": [
            "1.png",   # score 6, issue: KPI пустые + цвет
            "5.png",   # score 6, issue: KPI частично не совпадают
            "9.png",   # score 6, issue: KPI не совпадают
            "17.png",  # score 6, issue: KPI не полностью
            "22.png",  # score 6, issue: KPI частично
        ],
        "output": "eval_results/set_c_kpi",
    },
    "D": {
        "name": "Сложные — фикс потерь при detection",
        "description": (
            "Кейсы со score 6, где дашборд сложный (много виджетов разных типов). "
            "Основная проблема — виджеты теряются при detection или неверно классифицируются. "
            "Цель: widget_coverage >= 0.95 для всех кейсов."
        ),
        "target_score": 8,
        "fix_focus": "detection coverage — не терять виджеты, правильно классифицировать типы",
        "images": [
            "3.png",   # score 6, много виджетов
            "6.png",   # score 6, issue: KPI + цвет
            "7.png",   # score 6, issue: цвет + KPI
            "14.png",  # score 6, issue: KPI + цвет
            "24.png",  # score 6, issue: KPI + цвет
        ],
        "output": "eval_results/set_d_complex",
    },
    "E": {
        "name": "Простые — smoke test",
        "description": (
            "Простые дашборды с малым числом виджетов (score 8). "
            "Должны проходить быстро и стабильно. "
            "Используется как smoke test после каждого изменения кода."
        ),
        "target_score": 9,
        "fix_focus": "smoke test — всё должно работать без ошибок",
        "images": [
            "28.png",                          # score 8, мало виджетов
            "photo_2026-02-26_16-58-23.jpg",   # score 8, мало виджетов
            "image002.png",                    # score 8, мало виджетов
            "photo_2026-02-26_17-00-19.jpg",   # score 6 → улучшаем
            "photo_2026-02-26_16-58-28.jpg",   # score 6 → улучшаем
        ],
        "output": "eval_results/set_e_smoke",
    },
}
