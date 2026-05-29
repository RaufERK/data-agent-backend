#!/usr/bin/env python3
"""Build a full static perfume dashboard for Foresight HTML/Image insertion."""
from __future__ import annotations

import html
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright
from scripts.publish_perfume_dashboard import datasets

OUT = Path("artifacts/foresight_perfume_static")
HTML = OUT / "perfume_dashboard_static.html"
PNG = OUT / "perfume_dashboard_static.png"


def _max(rows: list[dict], key: str) -> float:
    return max(float(r[key]) for r in rows) if rows else 1.0


def bar_svg(rows: list[dict], label_key: str, value_key: str) -> str:
    max_v = _max(rows, value_key)
    bars = []
    labels = []
    for i, row in enumerate(rows):
        x = 42 + i * 82
        h = 170 * float(row[value_key]) / max_v
        y = 210 - h
        bars.append(f'<rect x="{x}" y="{y:.1f}" width="44" height="{h:.1f}" rx="3"/>')
        labels.append(f'<text x="{x + 22}" y="236" text-anchor="middle">{html.escape(str(row[label_key]))}</text>')
    return f"""
    <svg viewBox="0 0 720 260" class="chart-svg bar">
      <g class="grid">{''.join(f'<line x1="28" x2="700" y1="{40+i*34}" y2="{40+i*34}"/>' for i in range(6))}</g>
      <g class="bars">{''.join(bars)}</g>
      <g class="labels">{''.join(labels)}</g>
    </svg>"""


def line_svg(rows: list[dict], label_key: str, value_key: str) -> str:
    max_v = _max(rows, value_key)
    min_v = min(float(r[value_key]) for r in rows)
    span = max(max_v - min_v, 1.0)
    points = []
    labels = []
    for i, row in enumerate(rows):
        x = 45 + i * 88
        y = 210 - (float(row[value_key]) - min_v) / span * 165
        points.append((x, y))
        labels.append(f'<text x="{x}" y="236" text-anchor="middle">{html.escape(str(row[label_key]))}</text>')
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    dots = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5"/>' for x, y in points)
    return f"""
    <svg viewBox="0 0 720 260" class="chart-svg line">
      <g class="grid">{''.join(f'<line x1="28" x2="700" y1="{40+i*34}" y2="{40+i*34}"/>' for i in range(6))}</g>
      <polyline points="{poly}"/>
      <g>{dots}</g>
      <g class="labels">{''.join(labels)}</g>
    </svg>"""


def pie_svg(rows: list[dict], label_key: str, value_key: str) -> str:
    total = sum(float(r[value_key]) for r in rows) or 1.0
    colors = ["#d94aa8", "#7557d9", "#20a39e", "#f5a623"]
    start = -math.pi / 2
    slices = []
    legend = []
    for i, row in enumerate(rows):
        val = float(row[value_key])
        end = start + 2 * math.pi * val / total
        x1, y1 = 170 + 118 * math.cos(start), 128 + 118 * math.sin(start)
        x2, y2 = 170 + 118 * math.cos(end), 128 + 118 * math.sin(end)
        large = 1 if end - start > math.pi else 0
        color = colors[i % len(colors)]
        slices.append(
            f'<path d="M170,128 L{x1:.1f},{y1:.1f} A118,118 0 {large},1 {x2:.1f},{y2:.1f} Z" fill="{color}"/>'
        )
        pct = val / total * 100
        legend.append(
            f'<div><span style="background:{color}"></span>{html.escape(str(row[label_key]))} <b>{pct:.1f}%</b></div>'
        )
        start = end
    return f"""
    <div class="pie-wrap">
      <svg viewBox="0 0 340 260" class="pie-svg">{''.join(slices)}<circle cx="170" cy="128" r="54"/></svg>
      <div class="legend">{''.join(legend)}</div>
    </div>"""


def render_html() -> str:
    ds = datasets()
    kpi_rev = ds["kpi_rev"][1][0]["value"]
    kpi_sales = ds["kpi_sales"][1][0]["value"]
    kpi_margin = ds["kpi_margin"][1][0]["value"]
    brands = ds["bar_brand"][1]
    pie = ds["pie_type"][1]
    months = ds["line_month"][1]
    table = ds["tbl_brand"][1]
    rows = "\n".join(
        f"<tr><td>{html.escape(r['brand'])}</td><td>{r['total_sales_k']:,}</td><td>{r['total_rev_mln']:,.1f}</td></tr>"
        for r in table
    )
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Парфюмерный рынок 2024-2025</title>
<style>
*{{box-sizing:border-box}} body{{margin:0;background:#f4f0fb;color:#2f2854;font:14px Arial,sans-serif}}
.dash{{width:1600px;height:900px;padding:18px;display:grid;grid-template-columns:repeat(12,1fr);grid-template-rows:132px 270px 270px;gap:14px}}
.card{{background:white;border:1px solid #e5ddf2;border-radius:8px;box-shadow:0 2px 8px #7860a01f;padding:12px;overflow:hidden}}
.kpi{{grid-column:span 4;background:linear-gradient(135deg,#fff,#f5fbf8)}} h2{{margin:0 0 10px;font-size:19px;line-height:1.1;color:#31265d}}
.kpi .num{{font-size:42px;font-weight:700;margin-top:22px}} .kpi .sub{{color:#7b748c;margin-top:4px}}
.bar-card,.line-card{{grid-column:span 6}} .pie-card,.table-card{{grid-column:span 6}}
.chart-svg{{width:100%;height:215px}} .grid line{{stroke:#e7e1ee;stroke-width:1}} .bars rect{{fill:#d94aa8}} .labels text{{font-size:11px;fill:#777}}
.line polyline{{fill:none;stroke:#d94aa8;stroke-width:4;stroke-linejoin:round}} .line circle{{fill:#fff;stroke:#d94aa8;stroke-width:3}}
.pie-wrap{{display:flex;align-items:center;height:220px}} .pie-svg{{width:48%;height:220px}} .pie-svg circle{{fill:white}} .legend{{font-size:15px;line-height:2.1}} .legend span{{display:inline-block;width:12px;height:12px;border-radius:2px;margin-right:8px}}
table{{width:100%;border-collapse:collapse;font-size:12px}} th,td{{padding:4px 10px;border-bottom:1px solid #ebe6f3;text-align:right}} th:first-child,td:first-child{{text-align:left}} th{{color:#655c7d;background:#faf8fd}}
</style>
</head>
<body>
<main class="dash">
  <section class="card kpi"><h2>Выручка — всего млн руб</h2><div class="num">{kpi_rev:,.1f}</div><div class="sub">8 брендов, 8 месяцев</div></section>
  <section class="card kpi"><h2>Продажи — всего тыс единиц</h2><div class="num">{kpi_sales:,.0f}</div><div class="sub">суммарный объём продаж</div></section>
  <section class="card kpi"><h2>Средняя маржа %</h2><div class="num">{kpi_margin:,.1f}%</div><div class="sub">среднее по брендам и месяцам</div></section>
  <section class="card bar-card"><h2>Выручка по брендам</h2>{bar_svg(brands, "brand", "total_rev_mln")}</section>
  <section class="card pie-card"><h2>Доля по типу аромата</h2>{pie_svg(pie, "aroma_type", "revenue_mln")}</section>
  <section class="card line-card"><h2>Динамика выручки по месяцам</h2>{line_svg(months, "month", "total_rev_mln")}</section>
  <section class="card table-card"><h2>Детали по брендам</h2><table><thead><tr><th>Бренд</th><th>Продажи, тыс</th><th>Выручка, млн руб</th></tr></thead><tbody>{rows}</tbody></table></section>
</main>
</body>
</html>"""


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    HTML.write_text(render_html(), encoding="utf-8")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 900}, device_scale_factor=1)
        page.goto(HTML.resolve().as_uri(), wait_until="networkidle")
        page.screenshot(path=str(PNG), full_page=True)
        browser.close()
    print(json.dumps({"html": str(HTML), "png": str(PNG)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
