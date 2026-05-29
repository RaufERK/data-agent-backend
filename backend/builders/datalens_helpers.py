"""
Конвертер дашборда AI Platform → Yandex DataLens JSON формат (export v1).

Генерирует advanced-chart_node (Editor чарты) с данными, зашитыми прямо
в JavaScript — не требует датасетов/подключений в DataLens.

Структура выходного JSON:
{
  "export": {
    "version": "v1",
    "entries": {
      "dash": { "1": { "dash": { "data": {...}, "name": "..." } } },
      "widget": { "<id>": { "widget": {...} } }
    }
  },
  "hash": "<str>"
}
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import string
from typing import Any, Dict, List, Optional

# DataLens допускает: A-Za-zА-Яа-яЁё0-9_@()%.,:;'|-–—−$*& и пробелы
_DATALENS_ALLOWED = re.compile(r"[^A-Za-zА-Яа-яЁё0-9_@()%.,:;'|\-–—−$*& ]")
_DATALENS_EDGE = re.compile(r"^[^A-Za-zА-Яа-яЁё0-9_@()%]+|[^A-Za-zА-Яа-яЁё0-9_@()%]+$")


def _sanitize_title(title: str, fallback: str = "Widget") -> str:
    """Приводит строку к формату, допустимому в DataLens title/name."""
    if not title:
        return fallback
    cleaned = _DATALENS_ALLOWED.sub(" ", title)
    cleaned = _DATALENS_EDGE.sub("", cleaned).strip()
    if not cleaned:
        return fallback
    return cleaned


# Имена slice_name/chart_type которые означают KPI/метрику
_METRIC_KEYWORDS = {"metric", "kpi", "factmetric", "big_number", "number", "indicator"}

_GRID_WIDTH = 36  # DataLens использует 36-колоночную сетку


def _short_id(n: int = 2) -> str:
    chars = string.ascii_letters + string.digits
    return "".join(random.choices(chars, k=n))


def _make_hash(data: Any) -> str:
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True)
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def _is_metric_chart(chart: Dict) -> bool:
    ct = (chart.get("chart_type") or chart.get("viz_type") or "").lower()
    name = (chart.get("slice_name") or chart.get("name") or "").lower()
    if any(kw in ct for kw in _METRIC_KEYWORDS):
        return True
    if any(kw in name for kw in _METRIC_KEYWORDS):
        return True
    return False


def _js_str(s: str) -> str:
    """Экранирует строку для вставки в JS-литерал (в backtick-строку)."""
    return s.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")


def _js_data(obj: Any) -> str:
    """Сериализует Python-объект в JSON-строку для вставки в JS."""
    return json.dumps(obj, ensure_ascii=False)


# ---------------------------------------------------------------------------
# JS-шаблоны для Editor чартов
# ---------------------------------------------------------------------------

def _make_metric_js(title: str, value: Any, unit: str = "") -> str:
    """JS prepare для KPI/метрики — большое число с заголовком."""
    val_js = _js_data(value)
    title_js = _js_str(title)
    unit_js = _js_str(unit)
    return f"""module.exports = {{
    render: Editor.wrapFn({{
        fn: function(options) {{
            var val = {val_js};
            var formatted = val === null || val === undefined
                ? '—'
                : (typeof val === 'number'
                    ? val.toLocaleString('ru-RU')
                    : String(val));
            var unit = `{unit_js}`;
            return Editor.generateHtml(`<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;font-family:var(--g-font-family-sans,sans-serif);padding:8px 4px;box-sizing:border-box;">
                    <div style="font-size:13px;color:#888;margin-bottom:4px;text-align:center;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%">{title_js}</div>
                    <div style="font-size:28px;font-weight:600;color:#333;text-align:center">${{formatted}}${{unit ? ' ' + unit : ''}}</div>
                </div>`);
        }}
    }})
}};"""


def _make_bar_js(title: str, categories: List[str], series: List[Dict]) -> str:
    """JS prepare для столбчатой / горизонтальной диаграммы."""
    cats_js = _js_data(categories)
    series_js = _js_data(series)
    title_js = _js_str(title)
    colors = ["#4E8AF4", "#F4934E", "#4EF49A", "#F44E8A", "#9A4EF4", "#F4E14E"]
    colors_js = _js_data(colors)
    return f"""module.exports = {{
    render: Editor.wrapFn({{
        fn: function(options) {{
            var cats = {cats_js};
            var series = {series_js};
            var colors = {colors_js};
            var W = options.width || 400;
            var H = options.height || 300;
            var padL = 50, padR = 10, padT = 30, padB = 60;
            var chartW = W - padL - padR;
            var chartH = H - padT - padB;
            var n = cats.length;
            var ns = series.length;
            var groupW = n > 0 ? chartW / n : chartW;
            var barW = ns > 0 ? Math.max(2, (groupW * 0.8) / ns) : groupW * 0.8;
            var allVals = series.reduce(function(a,s){{return a.concat(s.data);}}, []);
            var maxVal = allVals.length ? Math.max.apply(null, allVals.map(function(v){{return v||0;}})) : 1;
            if (maxVal === 0) maxVal = 1;
            var bars = '';
            series.forEach(function(s, si) {{
                (s.data || []).forEach(function(v, ci) {{
                    var x = padL + ci * groupW + (groupW * 0.1) + si * barW;
                    var bh = Math.max(0, (v || 0) / maxVal * chartH);
                    var y = padT + chartH - bh;
                    var color = colors[si % colors.length];
                    bars += '<rect x="' + x.toFixed(1) + '" y="' + y.toFixed(1) + '" width="' + barW.toFixed(1) + '" height="' + bh.toFixed(1) + '" fill="' + color + '" rx="2"/>';
                }});
            }});
            var xLabels = cats.map(function(c, i) {{
                var x = padL + i * groupW + groupW / 2;
                var label = String(c);
                if (label.length > 8) label = label.slice(0, 7) + '…';
                return '<text x="' + x.toFixed(1) + '" y="' + (padT + chartH + 16) + '" text-anchor="middle" font-size="10" fill="#666">' + label + '</text>';
            }}).join('');
            var ySteps = 4;
            var yLines = '';
            for (var i = 0; i <= ySteps; i++) {{
                var yv = maxVal * i / ySteps;
                var y = padT + chartH - (yv / maxVal * chartH);
                var label = yv >= 1000000 ? (yv/1000000).toFixed(1)+'M' : yv >= 1000 ? (yv/1000).toFixed(1)+'K' : yv.toFixed(0);
                yLines += '<line x1="' + padL + '" y1="' + y.toFixed(1) + '" x2="' + (W - padR) + '" y2="' + y.toFixed(1) + '" stroke="#eee" stroke-width="1"/>';
                yLines += '<text x="' + (padL - 4) + '" y="' + (y + 4).toFixed(1) + '" text-anchor="end" font-size="10" fill="#888">' + label + '</text>';
            }}
            var legend = series.map(function(s, si) {{
                var lx = padL + si * 90;
                return '<rect x="' + lx + '" y="' + (H - 18) + '" width="10" height="10" fill="' + colors[si % colors.length] + '"/><text x="' + (lx + 14) + '" y="' + (H - 9) + '" font-size="10" fill="#555">' + (s.name||'') + '</text>';
            }}).join('');
            var svg = '<svg xmlns="http://www.w3.org/2000/svg" width="' + W + '" height="' + H + '" style="font-family:sans-serif">' +
                '<text x="' + (W/2) + '" y="18" text-anchor="middle" font-size="13" font-weight="600" fill="#333">{title_js}</text>' +
                yLines + bars + xLabels + legend + '</svg>';
            return Editor.generateHtml(svg);
        }}
    }})
}};"""


def _make_line_js(title: str, categories: List[str], series: List[Dict]) -> str:
    """JS prepare для линейного графика."""
    cats_js = _js_data(categories)
    series_js = _js_data(series)
    title_js = _js_str(title)
    colors = ["#4E8AF4", "#F4934E", "#4EF49A", "#F44E8A", "#9A4EF4"]
    colors_js = _js_data(colors)
    return f"""module.exports = {{
    render: Editor.wrapFn({{
        fn: function(options) {{
            var cats = {cats_js};
            var series = {series_js};
            var colors = {colors_js};
            var W = options.width || 400;
            var H = options.height || 300;
            var padL = 50, padR = 10, padT = 30, padB = 60;
            var chartW = W - padL - padR;
            var chartH = H - padT - padB;
            var n = cats.length;
            var allVals = series.reduce(function(a,s){{return a.concat(s.data);}}, []);
            var maxVal = allVals.length ? Math.max.apply(null, allVals.map(function(v){{return v||0;}})) : 1;
            var minVal = allVals.length ? Math.min.apply(null, allVals.map(function(v){{return v||0;}})) : 0;
            if (maxVal === minVal) maxVal = minVal + 1;
            function px(ci, v) {{
                var x = n > 1 ? padL + ci / (n - 1) * chartW : padL + chartW / 2;
                var y = padT + chartH - (v - minVal) / (maxVal - minVal) * chartH;
                return {{x: x, y: y}};
            }}
            var paths = series.map(function(s, si) {{
                var pts = (s.data || []).map(function(v, i) {{ return px(i, v || 0); }});
                if (!pts.length) return '';
                var d = pts.map(function(p, i) {{ return (i === 0 ? 'M' : 'L') + p.x.toFixed(1) + ',' + p.y.toFixed(1); }}).join(' ');
                var color = colors[si % colors.length];
                var dots = pts.map(function(p) {{ return '<circle cx="' + p.x.toFixed(1) + '" cy="' + p.y.toFixed(1) + '" r="3" fill="' + color + '"/>'; }}).join('');
                return '<path d="' + d + '" fill="none" stroke="' + color + '" stroke-width="2"/>' + dots;
            }}).join('');
            var xLabels = cats.map(function(c, i) {{
                var p = px(i, minVal);
                var label = String(c);
                if (label.length > 8) label = label.slice(0, 7) + '…';
                return '<text x="' + p.x.toFixed(1) + '" y="' + (padT + chartH + 16) + '" text-anchor="middle" font-size="10" fill="#666">' + label + '</text>';
            }}).join('');
            var ySteps = 4;
            var yLines = '';
            for (var i = 0; i <= ySteps; i++) {{
                var yv = minVal + (maxVal - minVal) * i / ySteps;
                var y = padT + chartH - (i / ySteps * chartH);
                var label = Math.abs(yv) >= 1000000 ? (yv/1000000).toFixed(1)+'M' : Math.abs(yv) >= 1000 ? (yv/1000).toFixed(1)+'K' : yv.toFixed(0);
                yLines += '<line x1="' + padL + '" y1="' + y.toFixed(1) + '" x2="' + (W-padR) + '" y2="' + y.toFixed(1) + '" stroke="#eee" stroke-width="1"/>';
                yLines += '<text x="' + (padL-4) + '" y="' + (y+4).toFixed(1) + '" text-anchor="end" font-size="10" fill="#888">' + label + '</text>';
            }}
            var legend = series.map(function(s, si) {{
                var lx = padL + si * 90;
                return '<rect x="' + lx + '" y="' + (H-18) + '" width="10" height="10" fill="' + colors[si%colors.length] + '"/><text x="' + (lx+14) + '" y="' + (H-9) + '" font-size="10" fill="#555">' + (s.name||'') + '</text>';
            }}).join('');
            var svg = '<svg xmlns="http://www.w3.org/2000/svg" width="' + W + '" height="' + H + '" style="font-family:sans-serif">' +
                '<text x="' + (W/2) + '" y="18" text-anchor="middle" font-size="13" font-weight="600" fill="#333">{title_js}</text>' +
                yLines + paths + xLabels + legend + '</svg>';
            return Editor.generateHtml(svg);
        }}
    }})
}};"""


def _make_pie_js(title: str, labels: List[str], values: List[float]) -> str:
    """JS prepare для круговой диаграммы."""
    labels_js = _js_data(labels)
    values_js = _js_data(values)
    title_js = _js_str(title)
    colors = ["#4E8AF4", "#F4934E", "#4EF49A", "#F44E8A", "#9A4EF4", "#F4E14E", "#4EF4E1"]
    colors_js = _js_data(colors)
    return f"""module.exports = {{
    render: Editor.wrapFn({{
        fn: function(options) {{
            var labels = {labels_js};
            var values = {values_js};
            var colors = {colors_js};
            var W = options.width || 300;
            var H = options.height || 300;
            var cx = W / 2, cy = H / 2 - 10, r = Math.min(cx, cy) - 40;
            var total = values.reduce(function(a,v){{return a+(v||0);}}, 0) || 1;
            var slices = '';
            var legend = '';
            var angle = -Math.PI / 2;
            values.forEach(function(v, i) {{
                var frac = (v || 0) / total;
                var sweep = frac * 2 * Math.PI;
                var x1 = cx + r * Math.cos(angle);
                var y1 = cy + r * Math.sin(angle);
                var x2 = cx + r * Math.cos(angle + sweep);
                var y2 = cy + r * Math.sin(angle + sweep);
                var large = sweep > Math.PI ? 1 : 0;
                var color = colors[i % colors.length];
                if (frac > 0) {{
                    slices += '<path d="M' + cx.toFixed(1) + ',' + cy.toFixed(1) + ' L' + x1.toFixed(1) + ',' + y1.toFixed(1) + ' A' + r + ',' + r + ' 0 ' + large + ',1 ' + x2.toFixed(1) + ',' + y2.toFixed(1) + ' Z" fill="' + color + '" stroke="#fff" stroke-width="1.5"/>';
                }}
                var lx = 8;
                var ly = H - 60 + i * 16;
                var label = String(labels[i] || '');
                if (label.length > 20) label = label.slice(0, 19) + '…';
                var pct = (frac * 100).toFixed(1);
                legend += '<rect x="' + lx + '" y="' + (ly-8) + '" width="10" height="10" fill="' + color + '"/><text x="' + (lx+14) + '" y="' + ly + '" font-size="10" fill="#555">' + label + ' (' + pct + '%)</text>';
                angle += sweep;
            }});
            var svg = '<svg xmlns="http://www.w3.org/2000/svg" width="' + W + '" height="' + H + '" style="font-family:sans-serif">' +
                '<text x="' + (W/2) + '" y="16" text-anchor="middle" font-size="13" font-weight="600" fill="#333">{title_js}</text>' +
                slices + legend + '</svg>';
            return Editor.generateHtml(svg);
        }}
    }})
}};"""


def _make_table_js(title: str, columns: List[str], rows: List[List]) -> str:
    """JS prepare для таблицы."""
    cols_js = _js_data(columns)
    rows_js = _js_data(rows)
    title_js = _js_str(title)
    return f"""module.exports = {{
    render: Editor.wrapFn({{
        fn: function(options) {{
            var cols = {cols_js};
            var rows = {rows_js};
            var W = options.width || 500;
            var headerBg = '#f0f4fa';
            var evenBg = '#f9fbff';
            var thStyle = 'padding:6px 10px;background:' + headerBg + ';border:1px solid #dde3ee;font-size:12px;font-weight:600;color:#333;white-space:nowrap;text-align:left';
            var tdStyle = 'padding:5px 10px;border:1px solid #e8edf5;font-size:12px;color:#444';
            var head = '<tr>' + cols.map(function(c){{return '<th style="' + thStyle + '">' + String(c) + '</th>';}}).join('') + '</tr>';
            var body = rows.map(function(row, ri) {{
                var bg = ri % 2 === 1 ? 'background:' + evenBg + ';' : '';
                var cells = Array.isArray(row)
                    ? row.map(function(v){{return '<td style="' + tdStyle + bg + '">' + (v === null || v === undefined ? '' : String(v)) + '</td>';}}).join('')
                    : cols.map(function(c){{var v=row[c];return '<td style="' + tdStyle + bg + '">' + (v === null || v === undefined ? '' : String(v)) + '</td>';}}).join('');
                return '<tr>' + cells + '</tr>';
            }}).join('');
            var html = '<div style="font-family:var(--g-font-family-sans,sans-serif);padding:4px;width:100%;overflow:auto;box-sizing:border-box">' +
                ('{title_js}' ? '<div style="font-size:13px;font-weight:600;color:#333;margin-bottom:6px;padding:2px 4px">{title_js}</div>' : '') +
                '<table style="border-collapse:collapse;width:100%;min-width:' + Math.max(300, cols.length * 100) + 'px">' +
                '<thead>' + head + '</thead><tbody>' + body + '</tbody></table></div>';
            return Editor.generateHtml(html);
        }}
    }})
}};"""


def _make_empty_js(title: str) -> str:
    """Заглушка для неподдерживаемых типов чартов."""
    title_js = _js_str(title)
    return f"""module.exports = {{
    render: Editor.wrapFn({{
        fn: function(options) {{
            return Editor.generateHtml('<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#999;font-family:sans-serif;font-size:13px">{title_js}</div>');
        }}
    }})
}};"""


def _make_editor_widget(name: str, prepare_js: str) -> Dict:
    """Создаёт объект advanced-chart_node для DataLens."""
    return {
        "widget": {
            "type": "advanced-chart_node",
            "name": name,
            "annotation": {"description": ""},
            "data": {
                "meta": '{\n    "links": {}\n}',
                "params": "module.exports = {};\n",
                "sources": "module.exports = {};\n",
                "controls": "module.exports = {};\n",
                "prepare": prepare_js,
                "config": "module.exports = {};\n",
            },
        }
    }


# ---------------------------------------------------------------------------
# Helpers для извлечения данных из чартов
# ---------------------------------------------------------------------------

def _extract_categories(chart: Dict) -> List[str]:
    """Извлекает категории (ось X) из чарта."""
    if chart.get("categories"):
        return [str(c) for c in chart["categories"]]
    columns = chart.get("columns") or []
    if columns:
        return [str(columns[0])]
    return []


def _extract_series(chart: Dict) -> List[Dict]:
    """Извлекает серии данных из чарта."""
    series = chart.get("series") or []
    if series:
        result = []
        for s in series:
            name = str(s.get("name") or s.get("label") or "Значение")
            data = s.get("data") or []
            result.append({"name": name, "data": [float(v) if v is not None else 0.0 for v in data]})
        return result
    return []


def _extract_kpi_value(kpi: Dict) -> Any:
    """Извлекает значение KPI."""
    for key in ("value", "metric_value", "current_value", "amount"):
        v = kpi.get(key)
        if v is not None:
            return v
    return None


def _try_float(v: Any) -> Optional[float]:
    """Пытается привести значение к float."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _try_int(v: Any, default: int = 0) -> int:
    """Пытается привести значение к int."""
    try:
        if v is None:
            return default
        return int(round(float(v)))
    except (TypeError, ValueError):
        return default

from .datalens_table_helpers import (
    _auto_fields,
    _rows_to_pie,
    _rows_to_series_and_categories,
)

__all__ = ["_auto_fields", "_rows_to_pie", "_rows_to_series_and_categories"]
