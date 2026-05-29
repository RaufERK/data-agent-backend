"""Navigator xparams builders for Triplex export."""
from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

from backend.builders.triplex_xml_parser import _append_colors, _hex_to_nav, _series_color_map


class _VisualXparamsMixin:
    def _build_diagram_visual(
        self,
        visuals_el: ET.Element,
        chart_type: str,
        dataset_id: str,
        x_field: Optional[str],
        y_field: Optional[str],
        x_field_id: Optional[str],
        y_field_id: Optional[str],
        columns: List[str],
        field_ids: Dict[str, str],
        field_types: Dict[str, str],
        display_names: Optional[Dict[str, str]],
        series_colors: List[tuple],
        primary_nav_color: Optional[str],
        palette_ref: str,
    ) -> str:
        """Append a <diagram> visual to *visuals_el* for bar/line/area/scatter types.

        Returns the effective params_chart_type (may change to 'line' for
        multi-series bar data that Navigator renders better as a plot).
        """
        params_chart_type = chart_type
        diagram_el = ET.SubElement(visuals_el, "diagram")
        series_list = ET.SubElement(diagram_el, "seriesList")
        series_id = "series1"
        series_type = "plot" if chart_type in ("line", "area", "scatter") else "histogram"
        series_attrs: Dict[str, str] = {
            "sID": series_id,
            "sType": series_type,
            "sName": y_field or "Значение",
            "sStyle": "solid",
            "isAccumulation": "0",
            "isValuesHidden": "0",
            "isPlusDisplayed": "0",
            "sDataSetID": dataset_id,
        }
        if x_field_id:
            series_attrs["sXFieldID"] = x_field_id
        if y_field_id:
            series_attrs["sYFieldID"] = y_field_id
        if series_type == "plot":
            series_attrs["isDotsHidden"] = "0"
            series_attrs["isSmooth"] = "1" if chart_type in ("line", "area") else "0"
        else:
            if len(series_colors) == 1:
                nav_color = _hex_to_nav(series_colors[0][1])
                if nav_color:
                    series_attrs["sColor"] = nav_color
                else:
                    series_attrs["nPalette"] = palette_ref
            else:
                series_attrs["nPalette"] = palette_ref
            series_attrs["isNormalized"] = "0"
            series_attrs["isPercentToggle"] = "0"

        # Build series elements: one per Y column for multi-series line/area/scatter/stacked bar,
        # single element for everything else.
        plot_y_cols: List[tuple] = []  # (col_name, nav_color)
        _default_colors = ["#4787ff", "#ff7f45", "#2ecc71", "#e74c3c", "#9b59b6",
                           "#f39c12", "#1abc9c", "#e91e63", "#00bcd4", "#ff5722"]
        color_map = {label: _hex_to_nav(hx) for label, hx in series_colors}
        non_x_numeric = [
            c for c in columns
            if c != x_field and field_types.get(c) == "number"
        ]
        # For multi-series bar charts keep histogram type; emit one series element
        # per numeric Y column.  Only fall back to plot for line/area/scatter.
        is_multi_histogram = series_type == "histogram" and len(non_x_numeric) > 1
        if is_multi_histogram:
            # Remove palette/accumulation attrs; each series gets its own color
            series_attrs.pop("nPalette", None)
            series_attrs.pop("isNormalized", None)
            series_attrs.pop("isPercentToggle", None)
        if series_type == "plot" or is_multi_histogram:
            # Collect all numeric (non-x) columns as separate Y series.
            if len(non_x_numeric) > 1:
                for ci, col in enumerate(non_x_numeric):
                    display_col = (display_names or {}).get(col, col)
                    col_color = color_map.get(display_col) or color_map.get(col) or _hex_to_nav(_default_colors[ci % len(_default_colors)])
                    plot_y_cols.append((col, col_color))
            else:
                # Single Y column — use primary color
                plot_y_cols.append((y_field, primary_nav_color or _hex_to_nav("#4787ff")))

        series_ids_added: List[str] = []
        if plot_y_cols:
            for si, (col_name, col_nav_color) in enumerate(plot_y_cols):
                sid = f"series{si + 1}"
                s_attrs = dict(series_attrs)
                s_attrs["sID"] = sid
                # For multi-series histogram each element must keep sType=histogram
                if is_multi_histogram:
                    s_attrs["sType"] = "histogram"
                s_attrs["sName"] = col_name or y_field or "Значение"
                s_attrs["sColor"] = col_nav_color or _hex_to_nav("#4787ff") or "0x4787ff"
                col_field_id = field_ids.get(col_name) if col_name else y_field_id
                if col_field_id:
                    s_attrs["sYFieldID"] = col_field_id
                elif y_field_id:
                    s_attrs["sYFieldID"] = y_field_id
                ET.SubElement(series_list, "series", attrib=s_attrs)
                series_ids_added.append(sid)
        else:
            ET.SubElement(series_list, "series", attrib=series_attrs)
            series_ids_added.append(series_id)

        ET.SubElement(diagram_el, "constants")
        canvases = ET.SubElement(diagram_el, "canvases")
        canvas_attrs: Dict[str, str] = {
            "sID": "canvas1",
            "sName": "Новое полотно",
            "isHorizontal": "1" if chart_type == "bar_horizontal" else "0",
            "isNameHidden": "1",
            "isAxisesHidden": "1",
            "isScaleLinesHidden": "0",
            "isWeekendHighlighted": "0",
            "isDetailDisabled": "0",
            "isComparingDisabled": "0",
            "isAutoHideLabelsDisabled": "0",
            "isCompressed": "0",
            "nPrecision": "1",
        }
        # Убрана автосортировка для сохранения исходного порядка категорий
        canvas_el = ET.SubElement(canvases, "canvas", attrib=canvas_attrs)
        for sid in series_ids_added:
            ET.SubElement(canvas_el, "series", attrib={"sID": sid})
        ET.SubElement(canvas_el, "legend", attrib={"isHidden": "0"})
        return params_chart_type

    def _build_xparams(
        self,
        dataset_name: str,
        dataset_alias: str,
        columns: List[str],
        rows: List[dict],
        chart_type: str,
        x_field: Optional[str],
        y_field: Optional[str],
        chart: Optional[Dict[str, Any]],
        stable: Optional[str] = None,
        display_names: Optional[Dict[str, str]] = None,
        dataset_query: Optional[str] = None,
    ) -> str:
        columns = self._resolve_columns(columns, x_field, y_field)

        series_colors = _series_color_map(chart) if chart else []
        self._collect_palette_colors(series_colors)
        primary_nav_color = _hex_to_nav(series_colors[0][1]) if series_colors else None
        palette_ref = "__PALETTE__"
        params_chart_type = chart_type

        widget_el = ET.Element("widget")
        datasets_el = ET.SubElement(widget_el, "datasets")
        # Reference the VIEW in src schema — Navigator will query it dynamically
        source_ref = stable or dataset_name
        # sID is an internal xparams identifier referenced by every visual
        # element via sDataSetID. Keep it short and ASCII-only; long Cyrillic
        # dataset names import successfully but can fail at render time.
        dataset_key = f"{source_ref}:{dataset_alias or dataset_name}"
        dataset_id = f"ds_{hashlib.md5(str(dataset_key).encode('utf-8')).hexdigest()[:12]}"
        dataset_attrs = {
            "sID": dataset_id,
            "sName": dataset_name,
            "sSource": "NAVSQL_LOCAL" if dataset_query else source_ref,
            "sType": "navsql" if dataset_query else "user",
            "isExtended": "0",
            "isDependent": "0",
        }
        dataset_el = ET.SubElement(datasets_el, "dataset", attrib=dataset_attrs)
        fields_el = ET.SubElement(dataset_el, "fields")

        sample_rows = [r for r in rows if isinstance(r, dict)][:20]
        dim_idx = 0
        metric_idx = 0
        col_idx = 0
        field_ids: Dict[str, str] = {}
        field_types: Dict[str, str] = {}
        x_field_id = None
        y_field_id = None
        for col in columns:
            values = [r.get(col) for r in sample_rows if col in r]
            col_kind = self._column_kind(values)
            column_type = "string"
            if col_kind == "number":
                column_type = "number"
            elif col_kind == "datetime":
                column_type = "datetime"
            if col == x_field and chart_type in {"bar", "bar_horizontal", "pie", "donut", "funnel", "treemap", "sunburst"}:
                column_type = "string"
            # y_field is always numeric — even when rows are empty or contain
            # placeholder strings the column is a measure and Navigator must
            # treat it as a number to render KPI cards and chart axes correctly.
            if col == y_field and chart_type not in {"table", "pivot_table"}:
                column_type = "number"

            if col == x_field:
                field_id = f"dim{dim_idx}"
                dim_idx += 1
                x_field_id = field_id
            elif col == y_field:
                field_id = f"metric{metric_idx}"
                metric_idx += 1
                y_field_id = field_id
            else:
                field_id = f"col{col_idx}"
                col_idx += 1

            # sExp must be a Navigator-safe identifier (used internally).
            # sName is the display label — use original if display_names provided.
            col_display = (display_names or {}).get(col, col)
            ET.SubElement(fields_el, "r", attrib={
                "sID": field_id,
                "sName": col_display,
                "sType": "column",
                "isHidden": "0",
                "sExp": col,
                "sColumnType": column_type,
            })
            field_ids[col] = field_id
            field_types[col] = column_type

        if dataset_query:
            navsql_el = ET.SubElement(dataset_el, "navsql")
            query_el = ET.SubElement(navsql_el, "query")
            query_el.text = dataset_query

        if not x_field_id and columns:
            x_field_id = field_ids.get(columns[0])
        if not y_field_id and columns:
            fallback_idx = 1 if len(columns) > 1 else 0
            y_field_id = field_ids.get(columns[fallback_idx])

        visuals_el = ET.SubElement(widget_el, "visuals")
        ET.SubElement(visuals_el, "widgetElements")

        categorical_cols = [col for col, kind in field_types.items() if kind == "string"]
        numeric_cols = [col for col, kind in field_types.items() if kind == "number"]

        if chart_type in ("bar", "bar_horizontal", "line", "area", "scatter"):
            params_chart_type = self._build_diagram_visual(
                visuals_el, chart_type, dataset_id,
                x_field, y_field, x_field_id, y_field_id,
                columns, field_ids, field_types, display_names,
                series_colors, primary_nav_color, palette_ref,
            )

        elif chart_type in ("table", "pivot_table"):
            table_el = ET.SubElement(visuals_el, "table", attrib={
                "isAlternateRowColors": "1",
                "sDataSetID": dataset_id,
            })
            ET.SubElement(table_el, "hierarchy")
            ET.SubElement(table_el, "experimental")
            columns_el = ET.SubElement(table_el, "columns")
            # Populate column list explicitly so Navigator renders all fields.
            # An empty <columns /> causes Navigator to show "Нет данных" even
            # when the source has data — it needs to know which fields to display.
            for col in columns:
                fid = field_ids.get(col)
                if not fid:
                    continue
                col_attrs: Dict[str, str] = {"sFieldID": fid}
                if field_types.get(col) == "number":
                    col_attrs["nPrecision"] = "1"
                ET.SubElement(columns_el, "r", attrib=col_attrs)

        elif chart_type == "pie":
            value_field_id = y_field_id or x_field_id
            title_field_id = x_field_id or y_field_id
            donut_attrs = {
                "nPalette": palette_ref,
                "sLegendAlign": "right",
                "sDataSetID": dataset_id,
                "isPercentDefault": "0",
                "isPercentToggle": "1",
                "isLabelsHidden": "0",
                "isTotalHidden": "0",
                "sLabelMask": "$1 ($2)",
                "isDatasetOrder": "0",
                "isLegendWithValues": "1",
            }
            if value_field_id:
                donut_attrs["sValueFieldID"] = value_field_id
            if title_field_id:
                donut_attrs["sTitleFieldID"] = title_field_id
            donut_el = ET.SubElement(visuals_el, "donut", attrib=donut_attrs)
            _append_colors(donut_el, series_colors)

        elif chart_type == "big_number":
            value_field_id = y_field_id or x_field_id
            unit = ""
            if isinstance(chart, dict):
                unit = str(chart.get("unit") or chart.get("value_unit") or "").strip()
            card_el = ET.SubElement(visuals_el, "card")
            block_groups = ET.SubElement(card_el, "blockGroups")
            ET.SubElement(block_groups, "blockGroup", attrib={"sID": "group1", "sName": "Группа"})
            rows_el = ET.SubElement(card_el, "rows")
            row_el = ET.SubElement(rows_el, "row", attrib={"isSeparatorDisplayed": "1"})
            block_attrs = {"sDataSetID": dataset_id, "nPrecision": "1"}
            if unit:
                block_attrs["sPostfix"] = unit
                block_attrs["sUnit"] = unit
                block_attrs["sMeasure"] = unit
                block_attrs["sScaleType"] = "none"
            block_el = ET.SubElement(row_el, "block", attrib=block_attrs)
            if value_field_id:
                value_attrs = {
                    "sFontSize": "large",
                    "sValueFieldID": value_field_id,
                }
                if unit:
                    # Navigator versions differ in the exact suffix attribute
                    # consumed by card widgets; keeping all three is harmless
                    # for import and preserves the unit for renderers that
                    # understand any of them.
                    value_attrs["sPostfix"] = unit
                    value_attrs["sSuffix"] = unit
                    value_attrs["sUnit"] = unit
                ET.SubElement(block_el, "value", attrib=value_attrs)

        elif chart_type == "sankey":
            source_col = x_field or (categorical_cols[0] if categorical_cols else None)
            target_col = categorical_cols[1] if len(categorical_cols) > 1 else source_col
            value_col = y_field or (numeric_cols[0] if numeric_cols else None)
            sankey_attrs = {
                "sDataSetID": dataset_id,
                "nPalette": palette_ref,
            }
            if source_col and field_ids.get(source_col):
                sankey_attrs["sFactorFieldID"] = field_ids[source_col]
            if target_col and field_ids.get(target_col):
                sankey_attrs["sTotalFactorFieldID"] = field_ids[target_col]
            if value_col and field_ids.get(value_col):
                sankey_attrs["sValueFieldID"] = field_ids[value_col]
            ET.SubElement(visuals_el, "sankey", attrib=sankey_attrs)

        elif chart_type == "funnel":
            title_col = x_field or (categorical_cols[0] if categorical_cols else None)
            value_col = y_field or (numeric_cols[0] if numeric_cols else None)
            funnel_attrs = {
                "sStyle": "rectangle",
                "sDataSetID": dataset_id,
                "isValuesHidden": "0",
                "nPalette": palette_ref,
            }
            if title_col and field_ids.get(title_col):
                funnel_attrs["sTitleFieldID"] = field_ids[title_col]
            if value_col and field_ids.get(value_col):
                funnel_attrs["sValueFieldID"] = field_ids[value_col]
            funnel_el = ET.SubElement(visuals_el, "funnel", attrib=funnel_attrs)
            _append_colors(funnel_el, series_colors)

        elif chart_type == "radar":
            axes_col = x_field or (categorical_cols[0] if categorical_cols else None)
            series_col = categorical_cols[1] if len(categorical_cols) > 1 else axes_col
            value_col = y_field or (numeric_cols[0] if numeric_cols else None)
            radar_el = ET.SubElement(visuals_el, "radarchart", attrib={"nPalette": palette_ref})
            data_attrs = {"sDataSetID": dataset_id}
            if axes_col and field_ids.get(axes_col):
                data_attrs["sAxesFieldID"] = field_ids[axes_col]
            if series_col and field_ids.get(series_col):
                data_attrs["sSeriesFieldID"] = field_ids[series_col]
            if value_col and field_ids.get(value_col):
                data_attrs["sValuesFieldID"] = field_ids[value_col]
            ET.SubElement(radar_el, "data", attrib=data_attrs)
            ET.SubElement(radar_el, "axisConfigs")
            ET.SubElement(radar_el, "seriesConfigs")

        elif chart_type == "sunburst":
            title_col = x_field or (categorical_cols[0] if categorical_cols else None)
            parent_col = categorical_cols[1] if len(categorical_cols) > 1 else title_col
            value_col = y_field or (numeric_cols[0] if numeric_cols else None)
            sunburst_attrs = {
                "sDisplayType": "expanded",
                "sDataSetID": dataset_id,
                "isEqualSizedSectors": "0",
                "isCanSwitchDisplayType": "0",
                "isValuesHidden": "0",
                "nPalette": palette_ref,
            }
            if parent_col and field_ids.get(parent_col):
                sunburst_attrs["sPartParentID"] = field_ids[parent_col]
            if title_col and field_ids.get(title_col):
                sunburst_attrs["sPartID"] = field_ids[title_col]
                sunburst_attrs["sTitleFieldID"] = field_ids[title_col]
            if value_col and field_ids.get(value_col):
                sunburst_attrs["sValueFieldID"] = field_ids[value_col]
            ET.SubElement(visuals_el, "sunburst", attrib=sunburst_attrs)

        elif chart_type == "candlestick":
            # Render as a multi-series line chart in Navigator (open/high/low/close lines).
            # Navigator has no native candlestick visual — lines on one canvas convey the
            # OHLC structure and keep real data visible.
            ohlc_names = ["open", "high", "low", "close"]
            ohlc_colors = ["#26a69a", "#ef5350", "#ef5350", "#26a69a"]
            diagram_el = ET.SubElement(visuals_el, "diagram")
            series_list_el = ET.SubElement(diagram_el, "seriesList")
            series_ids_added: List[str] = []
            ohlc_cols = [c for c in columns if c.lower() in ohlc_names and c != x_field]
            if not ohlc_cols:
                ohlc_cols = [c for c in columns if c != x_field and field_types.get(c) == "number"]
            for si, col in enumerate(ohlc_cols[:4]):
                sid = f"series{si + 1}"
                col_fid = field_ids.get(col)
                s_attrs: Dict[str, str] = {
                    "sID": sid,
                    "sType": "plot",
                    "sName": col,
                    "sStyle": "solid",
                    "isAccumulation": "0",
                    "isValuesHidden": "0",
                    "isPlusDisplayed": "0",
                    "isDotsHidden": "1",
                    "isSmooth": "0",
                    "sDataSetID": dataset_id,
                    "sColor": _hex_to_nav(ohlc_colors[si % len(ohlc_colors)]) or "0x26a69a",
                }
                if x_field_id:
                    s_attrs["sXFieldID"] = x_field_id
                if col_fid:
                    s_attrs["sYFieldID"] = col_fid
                ET.SubElement(series_list_el, "series", attrib=s_attrs)
                series_ids_added.append(sid)
            ET.SubElement(diagram_el, "constants")
            canvases_el = ET.SubElement(diagram_el, "canvases")
            canvas_el = ET.SubElement(canvases_el, "canvas", attrib={
                "sID": "canvas1", "sName": "Новое полотно",
                "isHorizontal": "0", "isNameHidden": "1", "isAxisesHidden": "1",
                "isScaleLinesHidden": "0", "isWeekendHighlighted": "0",
                "isDetailDisabled": "0", "isComparingDisabled": "0",
                "isAutoHideLabelsDisabled": "0", "isCompressed": "0", "nPrecision": "1",
            })
            for sid in series_ids_added:
                ET.SubElement(canvas_el, "series", attrib={"sID": sid})
            ET.SubElement(canvas_el, "legend", attrib={"isHidden": "0"})
            params_chart_type = "line"

        params_el = ET.SubElement(widget_el, "params")
        ET.SubElement(params_el, "r", attrib={"sID": "chartType", "sValue": params_chart_type})
        if chart_type not in {"table", "pivot_table"} and x_field:
            ET.SubElement(params_el, "r", attrib={"sID": "xField", "sValue": x_field})
        if chart_type not in {"table", "pivot_table"} and y_field:
            ET.SubElement(params_el, "r", attrib={"sID": "yField", "sValue": y_field})

        return ET.tostring(widget_el, encoding="unicode")

    # Table number -> (schema.table, isPK) mapping for the <d> section.
    # Mirrors the Navigator's data.tdbtable registry.
    TABLE_DEFINITIONS = [
        (1, "rm", "tposition", True),
        (2, "rme", "tpositiondashboard", True),
        (3, "rme", "tsubjectareaindicator", True),
        (4, "data", "kpi_", False),
        (5, "data", "loadcluster", False),
        (6, "data", "tppindicatorprocess", False),
        (7, "data", "tppindicatorprocessvalue", False),
        (8, "data", "tppindicatorprocesscluster", False),
        (9, "rme", "tsubjectareatuserdictionary", True),
        (10, "data", "tuserdictionary", False),
        (11, "rme", "tsubjectareausersource", True),
        (12, "data", "tusersource", False),
        (13, "data", "tusersourcefileuploadtemplate", False),
        (14, "data", "tppindicatorprocesscluster", False),
        (15, "ui", "tdashboard", False),
        (16, "ui", "tdicmenuitem", False),
        (17, "ui", "tscreen_v30", False),
        (18, "ui", "tscreensection_v30", False),
        (19, "ui", "tscreenwidget_v30", False),
        (20, "ui", "tcontrolobject_v30", False),
        (21, "ui", "tscreenwidgetparameters_v30", False),
        (22, "ui", "tcombinewidget_v30", False),
        (23, "ui", "tcontrolobjectlink_v30", False),
        (24, "ui", "tscreenwidgetdisabled_v30", False),
        (25, "ui", "tcontrolobjecthiddenscreenwidget_v30", False),
        (26, "ui", "tcontrolobjectemail_v30", False),
        (27, "ui", "tlink", False),
        (28, "ui", "tdashboarddomain", False),
        (29, "ui", "tdashboarddomaincard", False),
        (30, "ui", "tdashboarddomainlink", False),
        (31, "ui", "tcardcustomaction", False),
        (32, "ui", "tkpigridsettings", False),
        (33, "ui", "tcontrolobjectlinkautogenerated_v30", False),
        (34, "ui", "tdomain", False),
        (35, "ui", "tdashboardemaillink", False),
        (36, "ui", "tdashboardemailrecipient", False),
        (37, "ui", "tscreendataset_v30", False),
        (38, "ui", "tcontrolobjectdisabled_v30", False),
        (39, "ao", "treport", False),
        (40, "ao", "treportsqlparam", False),
        (41, "ao", "treportsettings", False),
        (42, "ao", "theader", False),
        (43, "ao", "theadercolumns", False),
        (44, "ao", "tcolumnsettings", False),
        (45, "ao", "tcolumnformat", False),
        (46, "ao", "tformatrule", False),
        (47, "dvn", "trules", False),
        (48, "dvn", "trulemetrics", False),
        (49, "dvn", "truleparams", False),
        (50, "dvn", "trulehtmltables", False),
        (51, "dvn", "tscheduler", False),
        (52, "dvn", "tsubscribe", False),
        (53, "dvn", "tsubscribesystem", False),
        (54, "dvn", "tsubscribeuser", False),
        (55, "dvn", "tsubscribedynamic", False),
        (56, "dvn", "tmessagesettings", False),
        (57, "dvn", "trulefilters", False),
        (58, "dvn", "tsubscribedynamicgroups", False),
        (59, "dvn", "tsubscribedynamicusers", False),
        (60, "dvn", "tproclibrary", False),
        (61, "ui", "ticonset", True),
        (62, "rme", "tpositionao", True),
        (63, "ui", "tpalette", True),
        (70, "dm", "tdatamodel", False),
        (71, "dm", "tdatamodelsource", False),
        (72, "dm", "tdatamodelsource_column", False),
        (73, "dm", "tdatamodellink", False),
        (74, "dm", "tdatamodelkey", False),
        (75, "dm", "tdatamodelfilter", False),
        (76, "dm", "tdatamodelrls", False),
        (77, "dm", "tdatamodelrlsobjects", False),
        (80, "queue", "tjobs", False),
        (81, "queue", "tjobsteps", False),
        (82, "queue", "tjobconditions", False),
        (83, "queue", "tjobconditionresults", False),
        (84, "queue", "tscheduler", False),
        (85, "rme", "tsubjectareajobs", True),
        (86, "queue", "tjobevent", False),
        (100, "rme", "tsubjectareadatamodel", True),
        (121, "data", "tusersourcetransformation", False),
        (201, "rm", "tsubjectarea", True),
        (202, "rme", "tsubjectareadashboard", True),
        (203, "rm", "tusersubjectarea", True),
        (204, "rm", "tusersubjectareaenvironment", True),
        (205, "rm", "tusersubjectarearole", True),
        (262, "rme", "tsubjectareaao", True),
    ]
