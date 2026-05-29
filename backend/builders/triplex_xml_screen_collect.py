"""Widget collection helpers for Triplex screen XML export."""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from backend.utils.color_utils import normalize_hex_color as _normalize_hex_color


class _XmlScreenCollectMixin:
    def _collect_widgets(
        self,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, str]]:
        """Build widget attrib dicts and collect datasets for all charts.

        Returns:
            widgets: list of t19 row attrib dicts
            widget_datasets: list of {name, columns, rows} for every dataset referenced
            dataset_stable_map: pre-computed stable alias per dataset name
        """
        widgets: List[Dict[str, Any]] = []
        widget_datasets: List[Dict[str, Any]] = []
        seen_datasets: set[str] = set()
        dataset_stable_map: Dict[str, str] = {}
        layout_offsets: Dict[str, int] = {}
        placed_rects: List[Tuple[int, int, int, int]] = []

        def _overlaps(rect: Tuple[int, int, int, int]) -> bool:
            x1, y1, x2, y2 = rect
            for px1, py1, px2, py2 in placed_rects:
                if max(x1, px1) < min(x2, px2) and max(y1, py1) < min(y2, py2):
                    return True
            return False

        def _place_widget(x: int, y: int, width: int, height: int) -> Tuple[int, int]:
            width = max(1, min(12, int(width)))
            height = max(1, int(height))
            x = max(0, min(12 - width, int(x)))
            y = max(0, int(y))
            while _overlaps((x, y, x + width, y + height)):
                y += 1
            placed_rects.append((x, y, x + width, y + height))
            return x, y

        for idx, chart in enumerate(self.charts, start=1):
            slice_name = (chart.get("slice_name") or chart.get("name") or "").strip()
            if not slice_name:
                continue
            dataset_name = (
                chart.get("dataset")
                or chart.get("table_name")
                or self._normalize_dataset_key(slice_name)
            )
            if not dataset_name:
                dataset_name = slice_name
            matching_layouts = self.layout_map.get(slice_name) or []
            layout_offset = layout_offsets.get(slice_name, 0)
            if matching_layouts:
                if layout_offset < len(matching_layouts):
                    layout = matching_layouts[layout_offset]
                else:
                    layout = matching_layouts[-1]
                layout_offsets[slice_name] = layout_offset + 1
                # Prefer dataset from layout when it differs from chart's fallback
                layout_dataset = (layout.get("dataset") or "").strip()
                if layout_dataset and layout_dataset != dataset_name and layout_dataset in self.tables:
                    dataset_name = layout_dataset
            else:
                layout = {}

            meta_entry = dict(self.chart_meta.get(slice_name) or self.chart_meta.get(dataset_name) or {})
            for key in ("x_field", "y_field", "metric_fields", "series_colors", "color"):
                if not meta_entry.get(key) and chart.get(key):
                    meta_entry[key] = chart.get(key)
            raw_chart_type = (
                meta_entry.get("chart_type")
                or chart.get("chart_type")
                or chart.get("viz_type")
            )
            row_type = chart.get("row_type")
            chart_type, nav_widget = self._normalize_chart_type(raw_chart_type, row_type)
            widget_type = nav_widget or self._widget_type_id(chart_type)

            if not chart.get("series") and isinstance(meta_entry.get("series_colors"), list):
                chart = dict(chart)
                chart["series"] = [
                    {"name": str(sc.get("name") or sc.get("label") or ""), "hex_code": sc.get("hex_code") or sc.get("color")}
                    for sc in meta_entry["series_colors"]
                    if isinstance(sc, dict)
                ]

            # Also collect per-chart primary color into palette
            meta_color = _normalize_hex_color(meta_entry.get("color"))
            if meta_color and meta_color not in self._palette_color_set:
                self._palette_color_set.add(meta_color)
                self._palette_colors.append((slice_name, meta_color))

            cols_info = self._infer_columns(dataset_name)
            axes = self._infer_axes(chart_type, cols_info["columns"], cols_info["rows"], meta_entry)
            x_field = axes.get("x")
            y_field = axes.get("y")
            filter_field = chart.get("filter_field")
            filter_value = chart.get("filter_value")
            if dataset_name == "FactDashboardRaw":
                raw_columns = cols_info["columns"]
                raw_rows = [row for row in cols_info["rows"] if isinstance(row, dict)]
                requested_filter = str(filter_field or "").strip()
                requested_value = str(filter_value or "").strip()
                if requested_filter not in raw_columns:
                    requested_filter = ""
                if requested_filter and requested_value:
                    def _val_match(row_val: Any, req_val: str) -> bool:
                        if str(row_val or "").strip() == req_val:
                            return True
                        # Numeric comparison: "2" == 2
                        try:
                            return float(row_val) == float(req_val)
                        except (TypeError, ValueError):
                            return False
                    has_match = any(_val_match(row.get(requested_filter), requested_value) for row in raw_rows)
                    if not has_match and "widget_title" in raw_columns:
                        requested_filter = "widget_title"
                        requested_value = slice_name
                elif "widget_title" in raw_columns:
                    requested_filter = "widget_title"
                    requested_value = slice_name
                elif "widget_id" in raw_columns:
                    requested_filter = "widget_id"
                    requested_value = str(chart.get("id") or filter_value or idx)
                filter_field = requested_filter or None
                filter_value = requested_value or None
            resolved_columns = self._resolve_columns(cols_info["columns"], x_field, y_field)
            metric_fields = []
            raw_metric_fields = meta_entry.get("metric_fields") if isinstance(meta_entry, dict) else None
            if not isinstance(raw_metric_fields, list):
                raw_metric_fields = chart.get("metric_fields")
            if isinstance(raw_metric_fields, list):
                metric_fields = [
                    str(field)
                    for field in raw_metric_fields
                    if str(field) in cols_info["columns"]
                ]
            if chart_type == "candlestick" and x_field:
                ohlc_cols = [c for c in cols_info["columns"] if c.lower() in ("open", "high", "low", "close")]
                if ohlc_cols:
                    resolved_columns = [x_field] + ohlc_cols
                    y_field = ohlc_cols[0]
            elif chart_type not in {"table", "pivot_table"} and x_field:
                # Widgets may share one raw vitrina with many metrics. Keep the
                # xparams for each visual focused on its selected fields while
                # the emitted Navigator source below still contains the full raw
                # table. Otherwise unrelated metric columns can make Navigator
                # downgrade charts to tables.
                focused_columns = [x_field]
                focused_metrics = metric_fields or ([y_field] if y_field else [])
                for metric_field in focused_metrics:
                    if metric_field and metric_field not in focused_columns:
                        focused_columns.append(metric_field)
                # Include the 'series' column for bar/line charts when data has
                # multiple distinct series — _normalize_long_format_to_wide will
                # pivot it to wide format so Navigator renders grouped bars.
                if chart_type in {"bar", "bar_horizontal", "line", "area", "combo"}:
                    series_col = next(
                        (c for c in cols_info["columns"]
                         if c.lower().strip() in {"series", "серия", "group", "группа"}),
                        None,
                    )
                    if series_col and series_col not in focused_columns:
                        sample = [r for r in cols_info["rows"] if isinstance(r, dict)][:50]
                        unique_series = set(str(r.get(series_col, "")) for r in sample if r.get(series_col))
                        if len(unique_series) >= 2:
                            focused_columns.append(series_col)
                if len(focused_columns) > 1:
                    resolved_columns = focused_columns
            # When navigator_single_raw_source=True, pre-filter raw rows for this widget
            # BEFORE any normalization so long→wide pivot operates on the correct subset.
            # Exclude genuine big_number (KPI) widgets — they use a dedicated metric source.
            # Include gauge→big_number fallbacks since they come from chart data rows.
            _is_gauge_fallback = (
                chart_type == "big_number"
                and str(raw_chart_type or "").lower() in {"gauge", "progress", "thermometer"}
            )
            use_per_widget_source = (
                (chart_type != "big_number" or _is_gauge_fallback)
                and self.payload.get("navigator_single_raw_source")
                and dataset_name == "FactDashboardRaw"
                and filter_field
                and filter_value
            )
            pre_filtered_rows = cols_info["rows"]
            if use_per_widget_source:
                ff, fv = filter_field, str(filter_value)
                def _val_match_pw(rv: Any, rv2: str) -> bool:
                    if str(rv or "").strip() == rv2:
                        return True
                    try:
                        return float(rv) == float(rv2)
                    except (TypeError, ValueError):
                        return False
                pre_filtered = [
                    r for r in cols_info["rows"]
                    if isinstance(r, dict) and _val_match_pw(r.get(ff), fv)
                ]
                if pre_filtered:
                    pre_filtered_rows = pre_filtered

            # Normalize wide-format (multi-numeric-column) bar data to long format
            wide_norm = self._normalize_wide_format(chart_type, resolved_columns, pre_filtered_rows, x_field)
            if wide_norm is not None:
                resolved_columns = wide_norm["columns"]
                effective_rows = wide_norm["rows"]
                x_field = wide_norm["x_field"]
                y_field = wide_norm["y_field"]
            else:
                effective_rows = pre_filtered_rows

            # Normalize long-format (x, series_name, value) bar/line data to wide-format.
            # Navigator cannot group by a string series column — each series must be its
            # own numeric column.  Only run when wide_norm did NOT already transform the data.
            if wide_norm is None:
                long_norm = self._normalize_long_format_to_wide(
                    chart_type, resolved_columns, effective_rows, x_field, y_field
                )
                if long_norm is not None:
                    resolved_columns = long_norm["columns"]
                    effective_rows = long_norm["rows"]
                    x_field = long_norm["x_field"]
                    y_field = long_norm["y_field"]

            if self._should_force_table_visual(
                chart_type,
                resolved_columns,
                effective_rows,
                x_field,
                y_field,
            ):
                chart_type = "table"
                widget_type = self._widget_type_id(chart_type)
            if chart_type in {"table", "pivot_table"}:
                # Drop internal FactDashboard plumbing columns — they are
                # technical keys not meant to be shown as table data.
                _internal = {"widget_id", "widget_title", "widget_type", "original_chart_type"}
                resolved_columns = [c for c in resolved_columns if c not in _internal]
                preserve_empty_columns = self._should_preserve_empty_table_columns(resolved_columns, effective_rows)
                if preserve_empty_columns:
                    effective_rows = self._compact_wide_table_text(resolved_columns, effective_rows)
                else:
                    resolved_columns = self._drop_empty_columns(resolved_columns, effective_rows)
                resolved_columns = self._move_first(resolved_columns, x_field)

            # Sanitize column names for Navigator compatibility.
            # Special chars (₽, commas, %, Δ …) in sExp break Navigator's parser.
            # _sanitize_columns returns safe identifiers + a display_names map so
            # the originals are preserved as sName labels in xparams.
            resolved_columns, display_names, effective_rows = self._sanitize_columns(
                resolved_columns, effective_rows
            )
            # Remap x/y field names to their sanitized equivalents
            if x_field and display_names:
                x_field = next((s for s, o in display_names.items() if o == x_field), x_field)
            if y_field and display_names:
                y_field = next((s for s, o in display_names.items() if o == y_field), y_field)

            if dataset_name not in dataset_stable_map:
                dataset_stable_map[dataset_name] = self._dataset_stable(
                    self._with_suffix(dataset_name)
                )
            raw_stable = dataset_stable_map[dataset_name]
            xparams_dataset_name = dataset_name
            xparams_dataset_alias = f"{dataset_name}__widget_{idx}"
            xparams_stable = raw_stable
            xparams_dataset_query = None
            if use_per_widget_source:
                per_widget_name = f"FactDashboardRaw_w{idx}"
                per_widget_stable_key = per_widget_name
                if per_widget_stable_key not in dataset_stable_map:
                    dataset_stable_map[per_widget_stable_key] = self._dataset_stable(
                        self._with_suffix(per_widget_name)
                    )
                xparams_dataset_name = per_widget_name
                xparams_dataset_alias = per_widget_name
                xparams_stable = dataset_stable_map[per_widget_stable_key]
                if per_widget_stable_key not in seen_datasets:
                    seen_datasets.add(per_widget_stable_key)
                    widget_datasets.append({
                        "name": per_widget_name,
                        "columns": resolved_columns,
                        "rows": effective_rows,
                    })
            elif (
                chart_type != "big_number"
                and not self.payload.get("navigator_single_raw_source")
                and dataset_name == "FactDashboardRaw"
                and (y_field or filter_field)
                and resolved_columns
            ):
                xparams_dataset_query = self._build_widget_navsql_query(
                    stable=raw_stable,
                    columns=resolved_columns,
                    filter_field=filter_field or y_field,
                    filter_value=filter_value,
                    display_names=display_names,
                )
            xparams = self._build_xparams(
                xparams_dataset_name,
                xparams_dataset_alias,
                resolved_columns,
                effective_rows,
                chart_type,
                x_field,
                y_field,
                chart,
                stable=xparams_stable,
                display_names=display_names,
                dataset_query=xparams_dataset_query,
            )

            x = layout.get("col", (idx - 1) % 3 * self.DEFAULT_WIDGET_WIDTH)
            y = layout.get("row", (idx - 1) // 3 * self.DEFAULT_WIDGET_HEIGHT)
            width = layout.get("width", self.DEFAULT_WIDGET_WIDTH)
            height = layout.get("height", self.DEFAULT_WIDGET_HEIGHT)

            try:
                x = int(x)
            except (TypeError, ValueError):
                x = 0
            try:
                y = int(y)
            except (TypeError, ValueError):
                y = 0
            try:
                width = int(width)
            except (TypeError, ValueError):
                width = self.DEFAULT_WIDGET_WIDTH
            height = self._normalize_layout_height(
                height,
                chart_type=chart_type,
                row_type=row_type,
                width=width,
                row_count=len(effective_rows) if isinstance(effective_rows, list) else 0,
            )
            x, y = _place_widget(x, y, width, height)

            widget_id = self._next_id()
            widgets.append({
                "nid": str(widget_id),
                "nscreenid": str(self.screen_id),
                "nwidgettypeid": widget_type,
                "sname_ru": slice_name,
                "sname_en": slice_name,
                "nxcoord": str(x),
                "nycoord": str(y),
                "nwidth": str(width),
                "nheight": str(height),
                "norder": str(idx),
                "nviewtype": "0",
                "xparams": xparams,
                "isisolated": "false",
                "isaienabled": "true",
                "isexport": "false",
            })

            if chart_type == "big_number" and len(getattr(self, "kpi_rows", []) or []) <= 3:
                kpi_row = self._find_kpi_row_for_widget(
                    chart,
                    slice_name=slice_name,
                    dataset_name=dataset_name,
                )
                spark_dataset_name = self._kpi_sparkline_dataset_name(kpi_row) if kpi_row else None
                spark_entry = self.tables.get(spark_dataset_name) if spark_dataset_name else None
                if isinstance(spark_entry, dict):
                    spark_rows = spark_entry.get("rows") if isinstance(spark_entry.get("rows"), list) else []
                    if len(spark_rows) >= 2:
                        spark_type_raw = str(spark_entry.get("_sparkline_type") or "line").strip().lower()
                        spark_chart_type = "bar" if spark_type_raw == "bar" else "line"
                        spark_columns = ["period", "value"]
                        spark_display_names = {"period": "Период", "value": "Значение"}
                        spark_dataset_alias = spark_dataset_name or f"{dataset_name}__sparkline"
                        if spark_dataset_alias not in dataset_stable_map:
                            dataset_stable_map[spark_dataset_alias] = self._dataset_stable(
                                self._with_suffix(spark_dataset_alias)
                            )
                        spark_stable = dataset_stable_map[spark_dataset_alias]
                        spark_xparams = self._build_xparams(
                            spark_dataset_alias,
                            spark_dataset_alias,
                            spark_columns,
                            spark_rows,
                            spark_chart_type,
                            "period",
                            "value",
                            {
                                "slice_name": f"{slice_name}: динамика",
                                "series": chart.get("series") or [],
                            },
                            stable=spark_stable,
                            display_names=spark_display_names,
                        )
                        spark_widget_id = self._next_id()
                        spark_height = 5 if width <= 4 else 6
                        spark_x, spark_y = _place_widget(x, y + height, width, spark_height)
                        widgets.append({
                            "nid": str(spark_widget_id),
                            "nscreenid": str(self.screen_id),
                            "nwidgettypeid": self._widget_type_id(spark_chart_type),
                            "sname_ru": f"{slice_name}: динамика",
                            "sname_en": f"{slice_name}: trend",
                            "nxcoord": str(spark_x),
                            "nycoord": str(spark_y),
                            "nwidth": str(width),
                            "nheight": str(spark_height),
                            "norder": str(idx * 100 + 1),
                            "nviewtype": "0",
                            "xparams": spark_xparams,
                            "isisolated": "false",
                            "isaienabled": "true",
                            "isexport": "false",
                        })
                        if spark_dataset_alias not in seen_datasets:
                            seen_datasets.add(spark_dataset_alias)
                            widget_datasets.append({
                                "name": spark_dataset_alias,
                                "columns": spark_columns,
                                "rows": spark_rows,
                            })

            # Collect dataset for t37 (screen datasets)
            if dataset_name not in seen_datasets:
                seen_datasets.add(dataset_name)
                source_entry = self.tables.get(dataset_name)
                source_columns = resolved_columns
                source_rows = effective_rows
                if isinstance(source_entry, dict) and chart_type not in {"table", "pivot_table"}:
                    full_columns = source_entry.get("columns")
                    full_rows = source_entry.get("rows")
                    if isinstance(full_columns, list) and full_columns:
                        source_columns = full_columns
                    if isinstance(full_rows, list):
                        source_rows = full_rows
                widget_datasets.append({
                    "name": dataset_name,
                    "columns": source_columns,
                    "rows": source_rows,
                })

            # Do not create analytical reports (Navigator "Таблицы") from
            # dashboard datasets. Widgets use t12 user sources directly.

        return widgets, widget_datasets, dataset_stable_map
