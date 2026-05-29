#!/usr/bin/env python3
"""
Парфюмерный дашборд в Foresight.

Стратегия (проверенная, как в foresight_service.py):
  - Логин → Dashboards → New
  - W1: INSERT > Chart (UI) → wizard → SaveObjectAs (API) → сохраняем key
  - W2-7: reopen edit_url → INSERT > Chart (UI) → wizard → Ctrl+S
  - Финал: reopen → bind_cube + select_all_dims + layout → Ctrl+S
"""
from __future__ import annotations

import csv
import json
import math
import random
import re
import string
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.config import Settings
from playwright.sync_api import sync_playwright

_cfg = Settings()

# ─── Датасет: парфюмерный рынок 2024–2025 ───────────────────────────────────

BRANDS = ["Chanel", "Dior", "Guerlain", "Hermès", "Lancôme",
          "Yves Saint Laurent", "Givenchy", "Bulgari"]
TYPES  = ["Eau de Parfum", "Eau de Toilette", "Parfum Extrait", "Eau de Cologne"]
MONTHS = ["2024-10", "2024-11", "2024-12",
          "2025-01", "2025-02", "2025-03", "2025-04", "2025-05"]

BRAND_REV_BASE  = {"Chanel":85, "Dior":78, "Guerlain":52, "Hermès":68,
                    "Lancôme":41, "Yves Saint Laurent":55, "Givenchy":44, "Bulgari":39}
BRAND_SALE_BASE = {"Chanel":420, "Dior":390, "Guerlain":180, "Hermès":160,
                    "Lancôme":210, "Yves Saint Laurent":250, "Givenchy":190, "Bulgari":140}
SEASON          = [1.15, 1.40, 1.60, 0.85, 0.90, 0.95, 1.05, 1.10]
TYPE_SHARE      = {"Eau de Parfum":0.42, "Eau de Toilette":0.33,
                    "Parfum Extrait":0.13, "Eau de Cologne":0.12}
TYPE_PRICE      = {"Eau de Parfum":12500, "Eau de Toilette":7800,
                    "Parfum Extrait":28000, "Eau de Cologne":4500}


def _r(v, s, sp=0.08):
    return max(0.0, v * (1.0 + math.sin(s * 37.13 + 1.7) * sp))


def datasets() -> dict[str, tuple[list[str], list[dict]]]:
    rev_rows = [
        {"brand": b, "month": MONTHS[mi],
         "sales_k": round(_r(BRAND_SALE_BASE[b]*SEASON[mi], mi*17+BRANDS.index(b))),
         "revenue_mln": round(_r(BRAND_REV_BASE[b]*SEASON[mi], mi*13+BRANDS.index(b)+5), 1),
         "margin_pct": round(min(0.85, 0.52+_r(0.06, mi+BRANDS.index(b)*3, 0.15)), 3)}
        for mi in range(len(MONTHS)) for b in BRANDS
    ]
    top = sorted([
        {"brand": b,
         "total_sales_k": sum(round(_r(BRAND_SALE_BASE[b]*SEASON[mi], mi*17+BRANDS.index(b))) for mi in range(len(MONTHS))),
         "total_rev_mln": round(sum(_r(BRAND_REV_BASE[b]*SEASON[mi], mi*13+BRANDS.index(b)+5) for mi in range(len(MONTHS))), 1)}
        for b in BRANDS
    ], key=lambda r: r["total_rev_mln"], reverse=True)
    type_rows = [
        {"aroma_type": t,
         "total_sales_k": sum(round(BRAND_SALE_BASE[b]*TYPE_SHARE[t]*1.05) for b in BRANDS),
         "revenue_mln": round(sum(BRAND_REV_BASE[b] for b in BRANDS)*TYPE_SHARE[t]*(TYPE_PRICE[t]/10000), 1)}
        for t in TYPES
    ]
    monthly = [
        {"month": MONTHS[mi],
         "total_sales_k": sum(round(_r(BRAND_SALE_BASE[b]*SEASON[mi], mi*17+BRANDS.index(b))) for b in BRANDS),
         "total_rev_mln": round(sum(_r(BRAND_REV_BASE[b]*SEASON[mi], mi*13+BRANDS.index(b)+5) for b in BRANDS), 1)}
        for mi in range(len(MONTHS))
    ]
    total_rev   = round(sum(r["revenue_mln"] for r in rev_rows), 1)
    total_sales = sum(r["sales_k"] for r in rev_rows)
    avg_margin  = round(sum(r["margin_pct"] for r in rev_rows) / len(rev_rows) * 100, 1)

    return {
        "kpi_rev":    (["label","value"], [{"label":"Выручка млн руб","value":total_rev}]),
        "kpi_sales":  (["label","value"], [{"label":"Продажи тыс ед", "value":total_sales}]),
        "kpi_margin": (["label","value"], [{"label":"Маржа процент",  "value":avg_margin}]),
        "bar_brand":  (["brand","total_rev_mln"],        [{k:r[k] for k in ["brand","total_rev_mln"]}         for r in top]),
        "pie_type":   (["aroma_type","revenue_mln"],     [{k:r[k] for k in ["aroma_type","revenue_mln"]}      for r in type_rows]),
        "line_month": (["month","total_rev_mln"],         [{k:r[k] for k in ["month","total_rev_mln"]}          for r in monthly]),
        "tbl_brand":  (["brand","total_sales_k","total_rev_mln"], top),
    }


PLAN = [
    # (ds_name,      title,                           chart_type,  block_type)
    ("kpi_rev",    "Выручка — всего млн руб",         "big_number","Gauge"),
    ("kpi_sales",  "Продажи — всего тыс единиц",      "big_number","Gauge"),
    ("kpi_margin", "Средняя маржа %",                  "big_number","Gauge"),
    ("bar_brand",  "Выручка по брендам",               "bar",       "Chart"),
    ("pie_type",   "Доля по типу аромата",             "pie",       "Chart"),
    ("line_month", "Динамика выручки по месяцам",      "line",      "Chart"),
    ("tbl_brand",  "Детали по брендам",                "table",     "Table"),
]
LAYOUT = [
    ( 0.0,  0.0, 32.0, 20.0),
    (34.0,  0.0, 32.0, 20.0),
    (68.0,  0.0, 32.0, 20.0),
    ( 0.0, 22.0, 49.0, 37.0),
    (51.0, 22.0, 49.0, 37.0),
    ( 0.0, 61.0, 49.0, 38.0),
    (51.0, 61.0, 49.0, 38.0),
]

# ─── CSV ─────────────────────────────────────────────────────────────────────

def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fields})

# ─── PPService ────────────────────────────────────────────────────────────────

_PP  = {"content-type":"application/json;charset=UTF-8","get-ppbi-time":"1"}


def pp(ctx, base: str, body: dict) -> dict:
    r = ctx.post(f"{base}/app/PPService.axd",
                 data=json.dumps(body, ensure_ascii=False), headers=_PP)
    if r.status != 200:
        raise RuntimeError(f"PP {r.status}: {r.text()[:400]}")
    return json.loads(r.text())


def rid(n=16):
    return "S" + "".join(random.choices(string.ascii_uppercase+string.digits, k=n-1))


def bind_cube(ctx, base, eax_id, cube_key, debug=False):
    """Привязывает куб к DSO и устанавливает dataRange=None (все данные)."""
    r = pp(ctx, base, {"SetEaxMd":{"tEax":{"id":eax_id},"tArg":{
        "pattern":{"dataSources":"Set"},
        "meta":{"dataSources":{
            "its":{"it":[{"k":0,"vis":True,"cube":{"obDesc":{"n":"","i":"","k":cube_key,"c":0}}}]},
            "OpenOptions":"DataAndSelection",
        }},
        "refresh":{"fetchData":True,"map":True,"grid":True,"bubbleTree":True,
                   "treeMap":True,"chart":True,"speedometer":True,"saveData":False},
        "metaGet":{"chart":True,"grid":True,"speedometer":True,"dataSources":"Get","dataRange":True},
    }}})
    if debug:
        meta = r.get("SetEaxMdResult",{}).get("meta",{})
        dr = meta.get("dataRange")
        print(f"      [bind_cube] dataRange after bind: {dr}")


def reset_data_range(ctx, base, eax_id):
    """Сбрасывает dataRange в None (все данные) через SetEaxMd dataRange.
    Вызывать после wizard и ДО Ctrl+S чтобы сохранился правильный диапазон."""
    try:
        r = pp(ctx, base, {"SetEaxMd":{"tEax":{"id":eax_id},"tArg":{
            "pattern":{"dataRange":True},
            "meta":{"dataRange":{"type":"None"}},
            "refresh":{"fetchData":True,"chart":True,"grid":True,"saveData":False},
            "metaGet":{"dataRange":True},
        }}})
        check = pp(ctx, base, {"GetEaxMd":{"tEax":{"id":eax_id},"tArg":{
            "pattern":{"chart":True},
        }}})
        dr = (check.get("GetEaxMdResult",{}).get("meta",{})
              .get("chart",{}).get("dataRange"))
        print(f"      [reset_data_range] chart dataRange: {dr}")
        return True
    except Exception as e:
        print(f"      [reset_data_range] WARN: {e}")
        return False


def select_all_dims(ctx, base, eax_id):
    try:
        r = pp(ctx, base, {"GetEaxMd": {"tEax":{"id":eax_id},"tArg":{"pattern":{
            "dims":True,
            "dimArg":{"elsArg":{"totalCount":True,"selectionInfo":True,
                                "filter":{"levels":0,"elementsGroup":True},
                                "pattern":{"attributes":"*"}},
                      "pattern":{"getDescr":True,"getIsAllSelected":True}},
        }}}})
        meta = r.get("GetEaxMdResult",{}).get("meta",{})
        dims = meta.get("dims",{}).get("its",{}).get("it",[])
        if isinstance(dims, dict):
            dims = [dims]
        for d in dims:
            dim_key = d.get("k") or d.get("key")
            dim_id = f"{eax_id}!{dim_key}" if dim_key is not None else d.get("id")
            pp(ctx, base, {"BatchExec":{"tArg":{"its":{"it":[{"ChangeDimSelection":{
                "tDim":{"id":dim_id},
                "tArg":{"elSelectOp":"Select","elRelative":"All",
                        "elKeys":{"it":[]},"ignoreMissingKeys":False,
                        "pattern":{"attributes":"*"},"schemaNoApply":True},
            }}]}}}})
        pp(ctx, base, {"SetEaxMd":{"tEax":{"id":eax_id},"tArg":{
            "pattern":{"grid":True},
            "meta":{"grid":{"dataDisplayMode":"Interactive"}},
            "refresh":{"chart":True,"fetchData":True,"saveData":False},
        }}})
        print(f"      dims selected: {len(dims)}")
        return len(dims)
    except Exception as e:
        print(f"      select_all_dims WARN: {e}")
        return 0


def configure_chart_axes(ctx, base, eax_id, ctype):
    """Put category dimension on X axis and Values on series.

    Foresight imports CSV charts as Values-on-X/category-as-objectives. With
    that orientation, selecting all elements still tends to render one cell.
    """
    if ctype in ("table", "big_number"):
        return
    try:
        r = pp(ctx, base, {"GetEaxMd":{"tEax":{"id":eax_id},"tArg":{
            "pattern":{"chart":True},
        }}})
        chart = r.get("GetEaxMdResult",{}).get("meta",{}).get("chart",{})
        timeline = chart.get("timeLineDimension") or {}
        objectives = chart.get("objectivesDimension") or {}
        timeline_is_facts = str(timeline.get("id","")).startswith("FACTS") or timeline.get("n") == "Values"
        objectives_is_facts = str(objectives.get("id","")).startswith("FACTS") or objectives.get("n") == "Values"
        meta = {"seriesInRows": False if ctype in ("pie", "donut") else True}
        if timeline_is_facts and not objectives_is_facts and timeline.get("k") and objectives.get("k"):
            meta["timeLineDimension"] = {"k": objectives["k"]}
            meta["objectivesDimension"] = {"k": timeline["k"]}
        pp(ctx, base, {"SetEaxMd":{"tEax":{"id":eax_id},"tArg":{
            "pattern":{"chart":True},
            "meta":{"chart":meta},
            "refresh":{"fetchData":True,"chart":True,"saveData":False},
            "metaGet":{"chart":True},
        }}})
        print(f"      axes configured: {meta}")
    except Exception as e:
        print(f"      configure_chart_axes WARN: {e}")


def set_vis(ctx, base, eax_id, ctype):
    hi = {"bar":"column","hbar":"bar","line":"line","area":"area","pie":"pie","donut":"pie"}.get(ctype,"column")
    if ctype == "table":
        mode = {"chart":{"enabled":False,"visible":False,"active":False,"viewOrder":1},
                "grid":{"enabled":True,"visible":True,"active":True,"viewOrder":0},
                "speedometer":{"enabled":False,"visible":False,"active":False,"viewOrder":1},
                "bubbleChart":{"enabled":False,"visible":False,"active":False,"viewOrder":1},
                "bubbleTree":{"enabled":False,"visible":False,"active":False,"viewOrder":1},
                "treeMap":{"enabled":False,"visible":False,"active":False,"viewOrder":1},
                "mapChart":{"enabled":False,"visible":False,"active":False,"viewOrder":1}}
    elif ctype == "big_number":
        mode = {"chart":{"enabled":False,"visible":False,"active":False,"viewOrder":1},
                "grid":{"enabled":False,"visible":False,"active":False,"viewOrder":1},
                "speedometer":{"enabled":True,"visible":True,"active":True,"viewOrder":0},
                "bubbleChart":{"enabled":False,"visible":False,"active":False,"viewOrder":1},
                "bubbleTree":{"enabled":False,"visible":False,"active":False,"viewOrder":1},
                "treeMap":{"enabled":False,"visible":False,"active":False,"viewOrder":1},
                "mapChart":{"enabled":False,"visible":False,"active":False,"viewOrder":1}}
    else:
        pp(ctx, base, {"SetEaxMd":{"tEax":{"id":eax_id},"tArg":{
            "pattern":{"setChart":{"meta":{"hiChart":json.dumps({
                "chart":{"defaultSeriesType":hi},
                "plotOptions":{hi:{"dataLabels":{"enabled":True}} if hi=="pie" else {"series":{}}},
                "template":None,
            },ensure_ascii=False)}}},
            "meta":{},
        }}})
        mode = {"chart":{"enabled":True,"visible":True,"active":True,"viewOrder":0},
                "grid":{"enabled":False,"visible":False,"active":False,"viewOrder":1},
                "speedometer":{"enabled":False,"visible":False,"active":False,"viewOrder":1},
                "bubbleChart":{"enabled":False,"visible":False,"active":False,"viewOrder":1},
                "bubbleTree":{"enabled":False,"visible":False,"active":False,"viewOrder":1},
                "treeMap":{"enabled":False,"visible":False,"active":False,"viewOrder":1},
                "mapChart":{"enabled":False,"visible":False,"active":False,"viewOrder":1}}
    pp(ctx, base, {"SetEaxMd":{"tEax":{"id":eax_id},"tArg":{
        "pattern":{"grid":True,"chart":True,"bubbleChart":True,
                   "bubbleTree":True,"treeMap":True,"mapChart":True,"speedometer":True},
        "meta":mode,
        "metaGet":{"chart":True,"grid":True,"speedometer":True},
    }}})


def do_layout(ctx, base, adhoc_id, widget_dso_ids):
    block_colors = ["#f8f4fc","#f4f8fc","#f4fcf8","#fffbf0","#f0f4ff","#fff0f4","#f8fff0"]
    areas = []
    for i, dso_id in enumerate(widget_dso_ids):
        _, w_title, w_type, w_block_type = PLAN[i]
        left, top, width, height = LAYOUT[i]
        right  = max(0.0, 100.0 - left - width)
        bottom = max(0.0, 100.0 - top  - height)
        areas.append({
            "@key": dso_id,
            "block": {
                "@type": w_block_type, "@key": dso_id,
                "prop": [
                    {"@tag":"name","@val":w_title},
                    {"@tag":"background","prop":[
                        {"@tag":"useBackground","@val":"1"},
                        {"@tag":"backgroundColor","@val":block_colors[i%len(block_colors)]},
                        {"@tag":"useGradient","@val":"0"},
                    ]},
                    {"@tag":"layout","prop":[
                        {"@tag":"left",         "@val":f"{left:.2f}"},
                        {"@tag":"right",        "@val":f"{right:.2f}"},
                        {"@tag":"top",          "@val":f"{top:.2f}"},
                        {"@tag":"bottom",       "@val":f"{bottom:.2f}"},
                        {"@tag":"leftUnit",     "@val":"%"},
                        {"@tag":"rightUnit",    "@val":"%"},
                        {"@tag":"topUnit",      "@val":"%"},
                        {"@tag":"bottomUnit",   "@val":"%"},
                        {"@tag":"anchorLeft",   "@val":"1"},
                        {"@tag":"anchorTop",    "@val":"1"},
                        {"@tag":"anchorRight",  "@val":"1"},
                        {"@tag":"anchorBottom", "@val":"1"},
                    ]},
                    {"@tag":"margins","prop":{"@tag":"useMargins","@val":"1"}},
                    {"@tag":"interactivity","@val":"1"},
                    {"@tag":"decor","prop":[
                        {"@tag":"cornerRadius","@val":"8"},
                        {"@tag":"useBorderRadius","@val":"1"},
                        {"@tag":"useBorder","@val":"0"},
                        {"@tag":"useShadow","@val":"1"},
                        {"@tag":"shadowColor","@val":"#9b8fbf"},
                        {"@tag":"shadowWidth","@val":"5"},
                        {"@tag":"shadowOpacity","@val":"15"},
                        {"@tag":"paddings","prop":[
                            {"@tag":"usePaddings","@val":"1"},
                            {"@tag":"left","@val":"10"},{"@tag":"right","@val":"10"},
                            {"@tag":"top","@val":"10"},{"@tag":"bottom","@val":"10"},
                        ]},
                    ]},
                    {"@tag":"title","prop":[
                        {"@tag":"show","@val":"1"},
                        {"@tag":"font","prop":[
                            {"@tag":"color","@val":"#3a2d5c"},
                            {"@tag":"family","@val":"Arial"},
                            {"@tag":"isBold","@val":"1"},
                            {"@tag":"size","@val":"13"},
                        ]},
                        {"@tag":"align","@val":"Left"},
                    ]},
                ],
            },
        })
    slide_key = rid()
    pp(ctx, base, {"SetAdHoc":{"tAdHocId":{"id":adhoc_id},"tArg":{
        "meta":{"Md8":{
            "activeSlideKey":1,
            "slides":{"its":{"it":[{"key":1,"mainPanel":{"block":{
                "@type":"Slide","@key":slide_key,
                "prop":[
                    {"@tag":"name","@val":"Парфюм 2024–2025"},
                    {"@tag":"background","prop":[
                        {"@tag":"useBackground","@val":"1"},
                        {"@tag":"backgroundColor","@val":"#f0ecf8"},
                        {"@tag":"useGradient","@val":"0"},
                    ]},
                    {"@tag":"margins","prop":{"@tag":"useMargins","@val":"0"}},
                    {"@tag":"interactivity","@val":"1"},
                    {"@tag":"decor","prop":{"@tag":"paddings","prop":[
                        {"@tag":"usePaddings","@val":"0"},
                        {"@tag":"left","@val":"10"},{"@tag":"right","@val":"10"},
                        {"@tag":"top","@val":"10"},{"@tag":"bottom","@val":"10"},
                    ]}},
                    {"@tag":"layouts","area":areas},
                ],
            }}}]}},
        }},
        "pattern":{"layout":{"activeSlideKey":True,"slides":"Change"}},
    }}})
    pp(ctx, base, {"SetAdHoc":{"tAdHocId":{"id":adhoc_id},"tArg":{
        "meta":{"Md":{"kap":{"@version":"10.8","block":{
            "@type":"Dashboard","@key":rid(),
            "prop":[
                {"@tag":"name","@val":"Парфюм 2024–2025"},
                {"@tag":"autoLayout","@val":"1"},
                {"@tag":"pageLayout","prop":{"@tag":"sizeMode","@val":"stretch"}},
                {"@tag":"counter","@val":str(len(areas))},
            ],
        }}}},
        "pattern":{"md":True},
    }}})
    print(f"   layout OK: {len(areas)} blocks")


def save_as(ctx, base, adhoc_id, name, obj_id):
    return pp(ctx, base, {"SaveObjectAs":{
        "tObject":{"id":adhoc_id},
        "tArg":{"destination":{"operation":"CreateNew","create":{
            "name":name,"id":obj_id,"permanent":True,
            "parent":{"i":"","n":"","k":0,"c":0,"p":0,"h":False},
        },"keepMoniker":True}},
    }})


# ─── Import wizard helper ─────────────────────────────────────────────────────

def run_insert_chart_wizard(page, csv_path: Path, label: str, debug_dir: Path) -> None:
    """INSERT > Chart → Data import wizard для создания нового блока с кубом."""
    def shot(name):
        page.screenshot(path=str(debug_dir/f"{name}.png"), full_page=True)

    shot(f"{label}_before")

    # INSERT > Chart
    try:
        page.locator("#InsertCategory").click(timeout=5000)
    except Exception:
        page.mouse.click(220, 42)
    page.wait_for_timeout(800)
    page.mouse.click(286, 98)
    page.wait_for_timeout(800)
    page.mouse.click(290, 154)
    page.wait_for_timeout(5000)  # ждём пока создастся новый блок

    shot(f"{label}_after_insert")

    # После INSERT панель может показывать "Slide" вместо Data sources нового блока.
    # Нужно кликнуть на новый chart блок чтобы его выбрать, потом перейти на вкладку Data.
    # Ищем все chart-блоки на слайде и кликаем на последний добавленный.
    body_txt = page.locator("body").inner_text()
    if "Data import" not in body_txt:
        # Кликаем на вкладку Data в верхней панели (она активирует data panel для текущего блока)
        # Сначала нужно выбрать новый блок — кликаем в центр canvas (правая часть экрана)
        # Новый блок обычно создаётся в центре слайда
        page.mouse.click(900, 450)
        page.wait_for_timeout(1000)
        page.mouse.click(900, 450)
        page.wait_for_timeout(1000)
        shot(f"{label}_click_block")
        # Теперь кликаем на Data tab
        try:
            page.get_by_text("DATA", exact=True).click(timeout=3000)
            page.wait_for_timeout(2000)
        except Exception:
            try:
                page.get_by_text("Data", exact=True).first.click(timeout=3000)
                page.wait_for_timeout(2000)
            except Exception:
                pass

    shot(f"{label}_data_panel")

    # Нажимаем "Data import..."
    imported = False
    for _attempt in range(5):
        body_txt2 = page.locator("body").inner_text()
        if "Data import" in body_txt2:
            try:
                page.get_by_text("Data import", exact=False).last.click(timeout=3000, force=True)
                imported = True
                break
            except Exception:
                pass
        page.wait_for_timeout(1500)
    if not imported:
        shot(f"{label}_noimport")
        raise RuntimeError(f"[{label}] Data import not found after 5 attempts")

    page.wait_for_timeout(2000)

    try:
        page.get_by_text("File with data", exact=False).click(timeout=5000, force=True)
    except Exception:
        page.mouse.click(585, 450)
    page.wait_for_timeout(800)

    # Next > → file chooser
    uploaded = False
    try:
        with page.expect_file_chooser(timeout=5000) as fc:
            page.get_by_text("Next >", exact=True).last.click(timeout=5000, force=True)
        fc.value.set_files(str(csv_path))
        uploaded = True
    except Exception:
        pass
    if not uploaded:
        try:
            page.get_by_text("Next >", exact=True).last.click(timeout=5000, force=True)
        except Exception:
            page.mouse.click(970, 727)
        page.wait_for_timeout(2500)
        try:
            with page.expect_file_chooser(timeout=5000) as fc:
                page.get_by_text("Browse", exact=False).click(timeout=5000, force=True)
            fc.value.set_files(str(csv_path))
        except Exception as e2:
            shot(f"{label}_nochooser")
            raise RuntimeError(f"[{label}] file chooser failed: {e2}")

    page.wait_for_timeout(5000)
    shot(f"{label}_uploaded")

    imported_from_preview = False
    try:
        import_btn = page.get_by_text("Import", exact=True).last
        if import_btn.is_visible(timeout=1500):
            import_btn.click(timeout=3000, force=True)
            imported_from_preview = True
    except Exception:
        imported_from_preview = False

    if not imported_from_preview:
        try:
            page.get_by_text("Next >", exact=True).last.click(timeout=5000, force=True)
        except Exception:
            page.mouse.click(970, 727)
        page.wait_for_timeout(5000)

        try:
            page.get_by_text("Import", exact=True).last.click(timeout=5000, force=True)
        except Exception:
            page.mouse.click(1055, 727)

    page.wait_for_timeout(12000)

    for btn in ["OK","Finish"]:
        try:
            page.get_by_text(btn, exact=True).last.click(timeout=3000, force=True)
        except Exception:
            pass
        page.wait_for_timeout(2000)

    # Убеждаемся что диалог закрылся
    for _ in range(3):
        body_check = page.locator("body").inner_text()
        if "Data import" not in body_check:
            break
        try:
            page.get_by_text("Finish", exact=True).last.click(timeout=2000, force=True)
        except Exception:
            try:
                page.get_by_text("Cancel", exact=True).last.click(timeout=2000, force=True)
            except Exception:
                page.keyboard.press("Escape")
        page.wait_for_timeout(2000)

    shot(f"{label}_done")

    apply_selection_all_ui(page, label, debug_dir)


def _tab_center(page, tab_text: str):
    """Return center of a visible left-panel tab, preferring PP tab containers."""
    return page.evaluate(
        """(tabText) => {
            const candidates = Array.from(document.querySelectorAll('.PPButtonContentContainer, .PPContent, div, span'));
            const visible = (el) => {
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
            };
            const exact = candidates.filter(el => visible(el) && el.textContent.trim() === tabText);
            exact.sort((a, b) => {
                const ac = a.classList.contains('PPButtonContentContainer') ? 0 : 1;
                const bc = b.classList.contains('PPButtonContentContainer') ? 0 : 1;
                return ac - bc;
            });
            const el = exact[0];
            if (!el) return null;
            const r = el.getBoundingClientRect();
            return {x: r.left + r.width / 2, y: r.top + r.height / 2, left: r.left, top: r.top, width: r.width, height: r.height};
        }""",
        tab_text,
    )


def _click_panel_tab(page, tab_text: str, fallback_xy: tuple[int, int]) -> bool:
    center = _tab_center(page, tab_text)
    if center:
        page.mouse.click(center["x"], center["y"])
        return True
    page.mouse.click(*fallback_xy)
    return False


def _click_first_visible_text(page, texts: list[str], exact: bool = False) -> str | None:
    for text in texts:
        try:
            page.get_by_text(text, exact=exact).first.click(timeout=2000, force=True)
            page.wait_for_timeout(1200)
            return text
        except Exception:
            pass
    return None


def _right_click_first_dimension_row(page) -> bool:
    box = page.evaluate(
        """() => {
            const names = ['brand', 'month', 'aroma_type', 'label', 'value', 'total_rev_mln', 'total_sales_k'];
            const els = Array.from(document.querySelectorAll('div, span, td, li'));
            const visible = (el) => {
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
            };
            const el = els.find(el => {
                const text = el.textContent.trim();
                return visible(el) && names.some(name => text === name || text.startsWith(name + ' '));
            });
            if (!el) return null;
            const r = el.getBoundingClientRect();
            return {x: r.left + Math.min(24, r.width / 2), y: r.top + r.height / 2};
        }"""
    )
    if not box:
        return False
    page.mouse.click(box["x"], box["y"], button="right")
    page.wait_for_timeout(1200)
    return True


def apply_selection_all_ui(page, label: str, debug_dir: Path | None = None) -> bool:
    """Open Selection tab and select all visible dimension elements in the UI layer.

    Ctrl+S persists the browser UI state, so this deliberately uses mouse/UI
    actions instead of only PPService ChangeDimSelection calls.
    """
    def shot(name):
        if debug_dir:
            page.screenshot(path=str(debug_dir / f"{name}.png"), full_page=True)

    try:
        # Exact measured fallback from docs/foresight_datarange_bug.md:
        # Selection center=(127,190), Data center=(54,190).
        found_tab = _click_panel_tab(page, "Selection", (127, 190))
        page.wait_for_timeout(1800)
        shot(f"{label}_selection_tab")
        print(f"      [select_all_ui] Selection tab {'bbox' if found_tab else 'fallback'}")

        clicked = _click_first_visible_text(page, ["Select All", "Select all", "Выбрать все", "All", "Все"])
        if clicked:
            print(f"      [select_all_ui] clicked '{clicked}'")
        else:
            # Some Foresight builds expose "Select all" only in the dimension row context menu.
            if _right_click_first_dimension_row(page):
                clicked = _click_first_visible_text(page, ["Select all", "Select All", "All", "Выбрать все", "Все"])
                if clicked:
                    print(f"      [select_all_ui] context clicked '{clicked}'")

        if not clicked:
            page.keyboard.press("Control+a")
            page.wait_for_timeout(1000)
            print("      [select_all_ui] fallback Ctrl+A")

        shot(f"{label}_selection_done")
        _click_panel_tab(page, "Data", (54, 190))
        page.wait_for_timeout(800)
        return True
    except Exception as se:
        print(f"      [select_all_ui] WARN: {se}")
        return False


def _dashboard_canvas_box(page) -> dict[str, float]:
    box = page.evaluate(
        """() => {
            const els = Array.from(document.querySelectorAll('div'));
            const visible = (el) => {
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width > 400 && r.height > 250 && r.top > 170 &&
                       s.visibility !== 'hidden' && s.display !== 'none';
            };
            const scored = els
                .filter(visible)
                .map(el => {
                    const r = el.getBoundingClientRect();
                    const cls = String(el.className || '');
                    const bonus = /kap|Layout|Slide|dashboard/i.test(cls) ? 1000000 : 0;
                    return {left: r.left, top: r.top, width: r.width, height: r.height, score: r.width * r.height + bonus};
                })
                .sort((a, b) => b.score - a.score);
            return scored[0] || null;
        }"""
    )
    if box:
        return box
    viewport = page.viewport_size or {"width": 1600, "height": 900}
    return {
        "left": 320.0,
        "top": 205.0,
        "width": float(viewport["width"] - 340),
        "height": float(viewport["height"] - 235),
    }


def apply_selection_all_for_layout_blocks(page, debug_dir: Path | None = None) -> None:
    """Select every laid-out widget block and apply UI Select All before Ctrl+S."""
    box = _dashboard_canvas_box(page)
    print(
        "   canvas box: "
        f"x={box['left']:.0f} y={box['top']:.0f} w={box['width']:.0f} h={box['height']:.0f}"
    )
    for i, (left, top, width, height) in enumerate(LAYOUT):
        x = box["left"] + (left + width / 2.0) / 100.0 * box["width"]
        y = box["top"] + (top + height / 2.0) / 100.0 * box["height"]
        print(f"   UI select-all W{i+1}: click ({x:.0f}, {y:.0f})")
        page.mouse.click(x, y)
        page.wait_for_timeout(1000)
        apply_selection_all_ui(page, f"final_w{i}", debug_dir)


def ctrl_s(page, wait_ms=5000):
    """Сохраняем дашборд через Ctrl+S (сохраняет DSO состояние в репозиторий).
    Если появляется диалог Save As — нажимаем OK для сохранения в существующее место."""
    page.keyboard.press("Control+S")
    page.wait_for_timeout(2000)
    # Если появился диалог Save As — нажимаем OK
    try:
        ok_btn = page.get_by_role("button", name="OK").last
        if ok_btn.is_visible(timeout=2000):
            ok_btn.click(timeout=2000)
            page.wait_for_timeout(2000)
    except Exception:
        pass
    page.wait_for_timeout(max(0, wait_ms - 4000))


def reopen_edit(page, edit_url: str, wait_ms=8000):
    """Переходим к сохранённому дашборду в edit mode."""
    page.goto(edit_url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(wait_ms)


# ─── Основная функция ─────────────────────────────────────────────────────────

def publish_deferred_save():
    debug_dir = Path("/tmp/foresight_perfume_debug")
    debug_dir.mkdir(parents=True, exist_ok=True)
    for f in debug_dir.glob("*.png"):
        f.unlink()

    base   = _cfg.foresight_base_url.rstrip("/")
    suffix = "".join(random.choices(string.digits, k=4))
    obj_id = f"DA_PERFUME_{suffix}"
    title  = "Парфюмерный рынок 2024–2025"

    print(f"\n{'='*60}")
    print(f"Публикация: {title}  [{obj_id}]")
    print(f"{'='*60}")

    ds = datasets()

    with tempfile.TemporaryDirectory(prefix="perfume_pub_") as tmpdir:
        tmp = Path(tmpdir)
        csv_by_name = {}
        for name, (fields, rows) in ds.items():
            p = tmp / f"{name}.csv"
            write_csv(p, fields, rows)
            csv_by_name[name] = p
            print(f"  CSV {name}: {len(rows)} rows, fields={fields}")

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(viewport={"width":1600,"height":900})
            page = ctx.new_page()

            state: dict = {
                "root_id": None,
                "adhoc_id": None,
                "cube_keys": [],
                "eax_ids": [],
            }

            def on_req(req):
                post = req.post_data or ""
                if not state["root_id"] and "!M!Root" in post:
                    m = re.search(r"([A-Z0-9]+!M!Root)", post)
                    if m: state["root_id"] = m.group(1)
                if '"tAdHocId":{"id":"' in post:
                    m = re.search(r'"tAdHocId":\{"id":"([^"]+)"\}', post)
                    if m: state["adhoc_id"] = m.group(1)
                cm = re.search(r'"cube":\{"obDesc":\{.*?"k":(\d+)', post)
                if cm:
                    k = int(cm.group(1))
                    if k not in state["cube_keys"]: state["cube_keys"].append(k)

            def on_resp(resp):
                try:
                    if "PPService.axd" not in resp.url: return
                    body = resp.text()
                    for m in re.finditer(r'"id":"([^"]+!DSO![^"]+)"', body):
                        eid = m.group(1)
                        if eid not in state["eax_ids"]: state["eax_ids"].append(eid)
                    for m in re.finditer(r'"k":(\d{5,})', body):
                        k2 = int(m.group(1))
                        if k2 not in state["cube_keys"]: state["cube_keys"].append(k2)
                    # Захватываем куб из SetEaxMd ответов
                    if '"dataSources"' in body and '"obDesc"' in body:
                        for m in re.finditer(r'"obDesc":\{[^}]*"k":(\d{5,})', body):
                            k3 = int(m.group(1))
                            if k3 not in state["cube_keys"]: state["cube_keys"].append(k3)
                except Exception:
                    pass

            page.on("request",  on_req)
            page.on("response", on_resp)

            # ── Логин ─────────────────────────────────────────────────────────
            print("\n[1] Логин...")
            page.goto(f"{base}/app/login.html#repo={_cfg.foresight_repo_id}",
                      wait_until="domcontentloaded", timeout=60000)
            page.fill('input[name="username"]', _cfg.foresight_repo_login)
            page.fill('input[type="password"]', _cfg.foresight_repo_password)
            page.keyboard.press("Enter")
            page.wait_for_timeout(5000)
            page.screenshot(path=str(debug_dir/"00_login.png"), full_page=True)

            # ── Dashboards → New ──────────────────────────────────────────────
            print("[2] Dashboards → New...")
            # Ждём успешного входа (страница меняется с login)
            for _ in range(30):
                if "login" not in page.url:
                    break
                page.wait_for_timeout(500)
            page.wait_for_timeout(2000)
            page.screenshot(path=str(debug_dir/"02_after_login.png"), full_page=True)

            for attempt in range(3):
                try:
                    page.get_by_text("Dashboards", exact=True).click(timeout=8000)
                    break
                except Exception:
                    try:
                        page.mouse.click(512, 420)
                    except Exception:
                        pass
                    page.wait_for_timeout(2000)

            for _ in range(40):
                if state["root_id"]: break
                page.wait_for_timeout(500)
            if not state["root_id"]:
                page.screenshot(path=str(debug_dir/"02_no_root.png"), full_page=True)
                raise RuntimeError("root_id не захвачен")

            try:
                page.get_by_text("New", exact=True).last.click(timeout=3000, force=True)
            except Exception:
                page.mouse.click(515, 580)
            for _ in range(20):
                if state["adhoc_id"]: break
                page.wait_for_timeout(500)
            if not state["adhoc_id"]:
                raise RuntimeError("adhoc_id не захвачен")

            adhoc_id = state["adhoc_id"]
            print(f"   adhoc_id captured: {adhoc_id[:50]}...")
            page.screenshot(path=str(debug_dir/"01_new_dash.png"), full_page=True)

            print("\n[3] Импорт всех виджетов во временный AdHoc (без Ctrl+S)...")
            widget_dso_ids = []
            widget_eax_ids = []
            per_cube_key = []

            for idx, (ds_name, w_title, w_type, _) in enumerate(PLAN):
                print(f"\n   W{idx+1}/{len(PLAN)}: {w_title} ({w_type}) [{ds_name}]")
                csv_path = csv_by_name[ds_name]

                eax_before2  = list(state["eax_ids"])
                cube_before2 = list(state["cube_keys"])

                run_insert_chart_wizard(page, csv_path, f"w{idx}", debug_dir)

                new_eax2  = [e for e in state["eax_ids"]  if e not in eax_before2]
                new_keys2 = [k for k in state["cube_keys"] if k not in cube_before2]
                wi_eax_id   = new_eax2[-1]  if new_eax2  else None
                wi_cube_key = max(new_keys2) if new_keys2 else None
                wi_dso_id   = wi_eax_id.split("!DSO!")[-1] if wi_eax_id else rid()
                print(f"   eax={wi_eax_id[:60] if wi_eax_id else None}  cube={wi_cube_key}  dso={wi_dso_id}")

                cur_adhoc = state["adhoc_id"] or adhoc_id
                try:
                    gr = pp(ctx.request, base, {"GetAdHoc":{"tAdHocId":{"id":cur_adhoc},"tArg":{
                        "pattern":{"dataSourceObjects":"Get"},
                    }}})
                    dso_list_cur = (gr.get("GetAdHocResult",{}).get("meta",{})
                                    .get("dataSourceObjects",{}).get("its",{}).get("it",[]))
                    if isinstance(dso_list_cur, dict): dso_list_cur = [dso_list_cur]
                    if dso_list_cur:
                        last_dso = dso_list_cur[-1]["id"]
                        last_eax = f"{cur_adhoc}!DSO!{last_dso}"
                        print(f"   GetAdHoc→lastDSO: {last_dso}")
                        reset_data_range(ctx.request, base, last_eax)
                        if wi_eax_id is None:
                            wi_eax_id = last_eax
                            wi_dso_id = last_dso
                except Exception as ge:
                   print(f"   GetAdHoc WARN: {ge}")

                widget_dso_ids.append(wi_dso_id)
                widget_eax_ids.append(wi_eax_id)
                per_cube_key.append(wi_cube_key)

            print("\n[4] Финальная настройка во временном AdHoc: vis + layout + dataRange=None...")
            final_adhoc_id = state["adhoc_id"] or adhoc_id
            print(f"   final adhoc_id: {final_adhoc_id[:60]}...")

            actual_dso_ids = []
            try:
                get_resp = pp(ctx.request, base, {"GetAdHoc":{"tAdHocId":{"id":final_adhoc_id},"tArg":{
                    "pattern":{"dataSourceObjects":"Get"},
                }}})
                dso_list = (get_resp.get("GetAdHocResult",{}).get("meta",{})
                            .get("dataSourceObjects",{}).get("its",{}).get("it",[]))
                if isinstance(dso_list, dict): dso_list = [dso_list]
                print(f"   DSOs в репозитории: {len(dso_list)}")
                for d in dso_list:
                    actual_dso_ids.append(d["id"])
                    print(f"   DSO: {d['id']}  obj_key={d.get('dsoObject',{}).get('k')}")
            except Exception as ge:
                print(f"   GetAdHoc WARN: {ge}")

            # Если GetAdHoc дал нам DSO ids — используем их (по порядку)
            # иначе используем наши widget_dso_ids
            layout_dso_ids = actual_dso_ids if len(actual_dso_ids) == len(PLAN) else widget_dso_ids
            print(f"   Используем {len(layout_dso_ids)} DSO ids для layout")

            final_eax_ids = [f"{final_adhoc_id}!DSO!{dso_id}" for dso_id in layout_dso_ids]

            print("\n[4b] Настройка типов визуализаций...")
            for i, (_, _, w_type, _) in enumerate(PLAN):
                if i >= len(final_eax_ids): break
                eax_id = final_eax_ids[i]
                print(f"   W{i+1} {w_type}  eax={eax_id[:55]}")
                try:
                    set_vis(ctx.request, base, eax_id, w_type)
                    configure_chart_axes(ctx.request, base, eax_id, w_type)
                    select_all_dims(ctx.request, base, eax_id)
                except Exception as e:
                    print(f"   set_vis WARN: {e}")

            print("\n[4c] Layout...")
            do_layout(ctx.request, base, final_adhoc_id, layout_dso_ids)

            print("\n[4d] Финальный reset dataRange=None для всех DSO перед SaveObjectAs...")
            for i, eax_id in enumerate(final_eax_ids):
                select_all_dims(ctx.request, base, eax_id)
                if i < len(PLAN):
                    configure_chart_axes(ctx.request, base, eax_id, PLAN[i][2])

            print("[5] SaveObjectAs в конце...")
            save_resp = save_as(ctx.request, base, final_adhoc_id, title, obj_id)
            ob = (save_resp.get("SaveObjectAsResult",{}).get("object")
                  or save_resp.get("tResult",{}).get("ob") or {})
            saved_key = ob.get("k") or ob.get("key")
            if not saved_key:
                raise RuntimeError(f"SaveObjectAs no key: {save_resp}")

            view_url = f"{base}/app/dashboard.html#key={saved_key}&mode=view&name=Dashboard&repo={_cfg.foresight_repo_id}"
            edit_url = f"{base}/app/dashboard.html#key={saved_key}&mode=edit&name=Dashboard&repo={_cfg.foresight_repo_id}"
            print(f"   Saved key={saved_key}")

            # ── Скриншот итогового вида ────────────────────────────────────────
            print("[6] Финальный скриншот...")
            try:
                page.goto(view_url, wait_until="networkidle", timeout=60000)
                page.wait_for_timeout(10000)
            except Exception:
                pass
            page.screenshot(path=str(debug_dir/"99_final.png"), full_page=True)

            try:
                page.goto(edit_url, wait_until="networkidle", timeout=60000)
                page.wait_for_timeout(6000)
                page.screenshot(path=str(debug_dir/"99_edit.png"), full_page=True)
            except Exception:
                pass

            browser.close()

    print(f"\n{'='*60}")
    print("✓ ГОТОВО!")
    print(f"  Object ID  : {obj_id}")
    print(f"  Object Key : {saved_key}")
    print(f"  View URL   : {view_url}")
    print(f"  Edit URL   : {edit_url}")
    print(f"  Debug PNG  : {debug_dir}/99_final.png")
    print(f"{'='*60}\n")
    return {"object_key": saved_key, "view_url": view_url, "edit_url": edit_url}


if __name__ == "__main__":
    publish_deferred_save()
