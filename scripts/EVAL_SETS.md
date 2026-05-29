# Тест-наборы для eval pipeline

## Обзор

36 кейсов из `final_batch_v2` разбиты на 5 наборов по характеру проблемы.
Каждый набор прогоняется отдельно, чтобы изолировать фикс и не ждать всё сразу.

```
Набор  Кейсы              Score сейчас  Цель   Проблема
─────  ─────────────────  ────────────  ─────  ──────────────────────────────
  A    21, 16, photo×2,28    8-10/10    10/10  Regression guard — не ломать
  B    2, 4, 8, 13, 25        8/10      10/10  Цвет/фон/контраст
  C    1, 5, 9, 17, 22        6/10       8/10  KPI пустые / значения не те
  D    3, 6, 7, 14, 24        6/10       8/10  Потери виджетов при detection
  E    28, photo×3, img002   6-8/10      9/10  Smoke test — простые дашборды
```

## Запуск

```bash
cd /home/user-tot/Desktop/data_agent

# Список наборов
.venv/bin/python scripts/run_eval_set.py --list

# Один набор
.venv/bin/python scripts/run_eval_set.py E   # smoke test (быстрый)
.venv/bin/python scripts/run_eval_set.py A   # regression guard
.venv/bin/python scripts/run_eval_set.py B   # цвета
.venv/bin/python scripts/run_eval_set.py C   # KPI
.venv/bin/python scripts/run_eval_set.py D   # сложные

# Все наборы
.venv/bin/python scripts/run_eval_set.py all
```

## Результаты

Каждый набор пишет в `eval_results/set_X_name/`:
```
baseline_report.json    — все метрики по кейсам
baseline_report.csv     — таблица для сравнения
C01/
  judgment.json         — score + issues + verdict от LLM
  navigator_screenshot.png
  navigator.xml
  analysis.json
  db_widgets.json
```

## Workflow фикса

1. Прогнать набор → посмотреть `judgment.json` и `comparison.png`
2. Найти конкретный баг в коде (один за раз)
3. Починить
4. Прогнать набор заново → score должен вырасти
5. Прогнать набор A (regression guard) → score не должен упасть
6. Коммит

## Описание наборов

### A — Идеальные (regression guard)
**Кейсы:** `21.png`, `16.png`, `photo_2026-02-26_16-58-23.jpg`, `image002.png`, `28.png`

Кейс 21 — единственный score 10 в истории, пиксельное совпадение.
После каждого изменения кода прогонять этот набор первым.
Падение ниже 8 — стоп, откатить изменение.

### B — Почти идеальные (цвета/фон)
**Кейсы:** `2.png`, `4.png`, `8.png`, `13.png`, `25.png`

Структура верная, layout правильный, данные есть — но цвета/фон/контраст не те.
Фикс: вытащить доминирующие цвета из оригинала и передать в xparams Navigator.

### C — KPI пустые
**Кейсы:** `1.png`, `5.png`, `9.png`, `17.png`, `22.png`

Виджеты есть, но KPI-числа нулевые или синтетические.
Фикс: убедиться что значения из analysis.spec.kpis попадают в XML xparams.

### D — Сложные (много виджетов)
**Кейсы:** `3.png`, `6.png`, `7.png`, `14.png`, `24.png`

Дашборды с 10+ виджетами разных типов. Часть виджетов теряется при detection.
Фикс: улучшить widget_coverage в analysis → xml → navigator цепочке.

### E — Простые (smoke test)
**Кейсы:** `28.png`, `photo_2026-02-26_16-58-23.jpg`, `image002.png`, `photo_2026-02-26_17-00-19.jpg`, `photo_2026-02-26_16-58-28.jpg`

Простые дашборды, 3-8 виджетов. Должны проходить быстро.
Прогоняются как smoke test после каждого изменения.
