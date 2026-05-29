"""Deterministic chart insights and quality scoring."""
from __future__ import annotations

from typing import Any


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        normalized = str(value).strip().replace(" ", "").replace(",", ".")
        return float(normalized)
    except Exception:
        return None


def _fmt_number(value: float) -> str:
    if value.is_integer():
        return f"{int(value):,}".replace(",", " ")
    return f"{value:,.1f}".replace(",", " ")


def _top_category(categories: list[str], values: list[float]) -> tuple[str, float] | None:
    if not categories or not values:
        return None
    pairs = [(categories[i], values[i]) for i in range(min(len(categories), len(values)))]
    if not pairs:
        return None
    return max(pairs, key=lambda item: item[1])


def _series_values(chart: dict[str, Any]) -> tuple[list[str], list[float]]:
    categories = [str(value) for value in chart.get("categories") or []]
    series = chart.get("series") or []
    if not series:
        return categories, []
    raw_values = series[0].get("values") or []
    values = [_as_float(value) or 0.0 for value in raw_values]
    return categories, values


def enrich_chart(chart: dict[str, Any]) -> dict[str, Any]:
    """Add insights and a compact quality score to a dashboard chart."""
    chart_type = str(chart.get("type") or "")
    insights: list[str] = []
    warnings: list[str] = []
    anomaly_categories: list[str] = []
    score = 100

    if chart_type == "kpi":
        value = chart.get("value")
        subtitle = chart.get("subtitle") or chart.get("aggregation") or ""
        if value not in (None, ""):
            insights.append(f"Значение: {value}{f' ({subtitle})' if subtitle else ''}.")
        if not value:
            warnings.append("KPI не содержит значения.")
            score -= 35

    elif chart_type in {"pie", "donut"}:
        slices = chart.get("slices") or []
        values = [_as_float(item.get("value")) or 0.0 for item in slices]
        total = sum(values)
        if slices and total > 0:
            max_idx = max(range(len(values)), key=lambda idx: values[idx])
            top = slices[max_idx]
            share = values[max_idx] / total * 100
            insights.append(f"Категория «{top.get('label')}» даёт {share:.1f}% от суммы.")
            if share >= 45 and len(slices) >= 3:
                anomaly_categories.append(str(top.get("label")))
                insights.append(f"Доля «{top.get('label')}» необычно высокая для структуры.")
            if len(slices) > 1:
                insights.append(f"Показано {len(slices)} сегментов, общий объём {_fmt_number(total)}.")
        if len(slices) > 8:
            warnings.append("Слишком много сегментов для pie-графика.")
            score -= 25
        if total <= 0:
            warnings.append("Сумма сегментов нулевая.")
            score -= 30

    elif chart_type == "table":
        table = chart.get("table") or {}
        rows = table.get("rows") or []
        columns = table.get("columns") or []
        insights.append(f"Показано {len(rows)} строк и {len(columns)} колонок.")
        if len(columns) > 8:
            warnings.append("В таблице много колонок, подписи могут быть перегружены.")
            score -= 15
        if not rows:
            warnings.append("Таблица пустая.")
            score -= 35

    elif chart_type == "filter":
        filter_payload = chart.get("filter") or {}
        options = filter_payload.get("options") or []
        if options:
            top_option = max(options, key=lambda item: _as_float(item.get("count")) or 0.0)
            insights.append(f"Автофильтр собрал {len(options)} значений.")
            insights.append(f"Самое частое значение: «{top_option.get('label')}».")
        else:
            warnings.append("Фильтр не содержит значений.")
            score -= 25

    else:
        categories, values = _series_values(chart)
        total = sum(values)
        top = _top_category(categories, values)
        if top:
            label, value = top
            insights.append(f"Пик приходится на «{label}»: {_fmt_number(value)}.")
            if total > 0:
                share = value / total * 100
                insights.append(f"Категория «{label}» даёт {share:.1f}% от суммы.")
                mean = total / max(len(values), 1)
                if (share >= 42 and len(values) >= 3) or (mean > 0 and value >= mean * 2.2):
                    anomaly_categories.append(label)
                    insights.append(f"«{label}» заметно выше остальных категорий.")
        if chart_type == "line" and len(values) >= 2:
            first, last = values[0], values[-1]
            if first != 0:
                delta = (last - first) / abs(first) * 100
                direction = "рост" if delta >= 0 else "падение"
                insights.append(f"От первой к последней точке: {direction} на {abs(delta):.1f}%.")
            drops = [
                (idx, values[idx - 1] - values[idx])
                for idx in range(1, len(values))
                if values[idx - 1] > 0 and (values[idx - 1] - values[idx]) / values[idx - 1] >= 0.35
            ]
            if drops and categories:
                drop_idx, _ = max(drops, key=lambda item: item[1])
                anomaly_categories.append(categories[drop_idx])
                insights.append(f"Есть резкое падение после «{categories[drop_idx - 1]}».")
            spikes = [
                (idx, values[idx])
                for idx in range(1, len(values))
                if values[idx - 1] > 0 and values[idx] / values[idx - 1] >= 1.5
            ]
            if spikes and categories:
                spike_idx, _ = max(spikes, key=lambda item: item[1])
                anomaly_categories.append(categories[spike_idx])
                insights.append(f"Есть резкий скачок в точке «{categories[spike_idx]}».")
        if len(categories) > 12 and chart_type in {"bar", "hbar", "line"}:
            warnings.append("Много категорий, подписи могут читаться хуже.")
            score -= 20
        if any(len(label) > 28 for label in categories):
            warnings.append("Есть длинные подписи категорий.")
            score -= 10
        if chart_type in {"pie", "donut"} and len(categories) > 8:
            warnings.append("Pie-график перегружен сегментами.")
            score -= 25
        if not values:
            warnings.append("Нет числовых значений для графика.")
            score -= 35

    if not insights:
        insights.append("Недостаточно данных для автоматического вывода.")

    next_chart = dict(chart)
    next_chart["insights"] = insights[:3]
    next_chart["qualityScore"] = max(0, min(100, score))
    next_chart["qualityWarnings"] = warnings[:4]
    if anomaly_categories:
        next_chart["highlightedCategories"] = list(dict.fromkeys(anomaly_categories))[:3]
        next_chart["highlightColor"] = "anomaly"
        next_chart["anomalyBadge"] = "anomaly"
    return next_chart
