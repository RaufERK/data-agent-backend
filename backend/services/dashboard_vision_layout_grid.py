"""Grid layout builder mixin for DashboardVisionService."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


class _LayoutGridMixin:
    @classmethod
    def _build_grid_layout(cls, parsed: Dict[str, Any]) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for idx, kpi in enumerate(parsed.get("kpis") or []):
            if not isinstance(kpi, dict):
                continue
            title = str(kpi.get("name") or "").strip()
            if not title:
                continue
            metric_code = re.sub(r"(^_+|_+$)", "", re.sub(r"[^a-z0-9а-яё]+", "_", title.lower())) or f"metric_{idx+1}"
            items.append({
                "title": title,
                "row_type": "metric",
                "viz_type": "big_number_total",
                "dataset": f"FactDashboardMetric_{metric_code}",
                "position": cls._normalize_position(kpi.get("position"), idx),
            })
        base_index = len(items)
        for idx, chart in enumerate(parsed.get("charts") or []):
            if not isinstance(chart, dict):
                continue
            title = cls._chart_title(chart)
            if not title:
                continue
            items.append({
                "title": title,
                "row_type": "table",
                "viz_type": str(chart.get("chart_type") or "table"),
                "dataset": str(chart.get("table_hint") or title),
                "position": cls._normalize_position(chart.get("position"), base_index + idx),
            })
        if not items:
            return []

        metric_count = sum(1 for item in items if item["row_type"] == "metric")

        def _widget_size(item: Dict[str, Any]) -> tuple[int, int]:
            # Return (width columns, height grid units).  Keep row and height
            # in the same compact coordinate system; exporters can scale it for
            # their own canvas, but the packer must not mix pixels and rows.
            is_metric = item["row_type"] == "metric"
            viz_type = str(item.get("viz_type") or "table").lower()
            position = item.get("position") if isinstance(item.get("position"), dict) else {}
            try:
                width_hint = float(position.get("width")) if position.get("width") is not None else 1.0
            except (TypeError, ValueError):
                width_hint = 1.0

            if is_metric:
                if metric_count >= 3:
                    return 3 if 0.0 < width_hint <= 0.28 else 4, 5
                if 0.0 < width_hint <= 0.28:
                    return 3, 5
                if width_hint <= 0.42:
                    return 4, 5
                return 4, 5
            elif viz_type in {"table", "pivot_table", "pivot_table_v2"}:
                return (6 if 0.0 < width_hint <= 0.62 else 12), 9
            elif viz_type in {"bar", "bar_horizontal", "line", "area", "combo"}:
                return (6, 8) if 0.0 < width_hint <= 0.62 else (12, 12)
            elif viz_type in {"pie", "donut", "treemap", "sunburst", "sankey", "funnel"}:
                return (6 if 0.0 < width_hint <= 0.62 else 12), 8
            else:
                return (6 if 0.0 < width_hint <= 0.62 else 12), 8

        def _layout_entry(
            item: Dict[str, Any],
            *,
            row: int,
            col: int,
            width: int,
            span: int,
            layout: List[Dict[str, Any]],
        ) -> None:
            is_metric = item["row_type"] == "metric"
            layout.append({
                "id": f"img_{len(layout) + 1}",
                "slice_name": item["title"],
                "label": f"KPI: {item['title']}" if is_metric else item["title"],
                "dataset": item["dataset"],
                "viz_type": item["viz_type"],
                "type": item["viz_type"],
                "row_type": item["row_type"],
                "row": row,
                "col": col,
                "width": width,
                "height": span,
            })

        def _scaled_widths(raw_widths: List[int], min_widths: List[int]) -> List[int]:
            count = len(raw_widths)
            if count == 1:
                return [max(min_widths[0], min(12, raw_widths[0]))]
            if sum(min_widths) >= 12:
                base = 12 // count
                widths = [base] * count
                for idx in range(12 - sum(widths)):
                    widths[idx % count] += 1
                return widths
            total_raw = max(1, sum(max(1, width) for width in raw_widths))
            widths = [max(min_widths[idx], round((max(1, raw_widths[idx]) / total_raw) * 12)) for idx in range(count)]
            total = sum(widths)
            while total < 12:
                grow_idx = max(range(count), key=lambda idx: raw_widths[idx] - widths[idx])
                widths[grow_idx] += 1
                total += 1
            while total > 12:
                shrink_candidates = [idx for idx in range(count) if widths[idx] > min_widths[idx]]
                if not shrink_candidates:
                    break
                shrink_idx = max(shrink_candidates, key=lambda idx: widths[idx] - min_widths[idx])
                widths[shrink_idx] -= 1
                total -= 1
            if sum(widths) != 12:
                widths[-1] += 12 - sum(widths)
            return widths

        def _build_row_layout(source_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            row_groups: List[Dict[str, Any]] = []
            for item in source_items:
                top = item["position"]["top"]
                threshold = max(0.06, min(0.12, item["position"]["height"] * 0.6))
                if not row_groups or abs(top - row_groups[-1]["anchor"]) > threshold:
                    row_groups.append({"anchor": top, "items": [item]})
                else:
                    group = row_groups[-1]
                    group["items"].append(item)
                    group["anchor"] = (group["anchor"] + top) / 2.0

            layout: List[Dict[str, Any]] = []
            current_row = 0

            def _append_row(row_items: List[Dict[str, Any]], width_override: Optional[List[int]] = None) -> None:
                nonlocal current_row
                if not row_items:
                    return
                sized = []
                for item in row_items:
                    width, span = _widget_size(item)
                    sized.append((item, width, span))

                if width_override is None:
                    min_widths = [4 if item["row_type"] == "metric" else 6 for item, _, _ in sized]
                    widths = _scaled_widths([width for _, width, _ in sized], min_widths)
                else:
                    widths = list(width_override)

                span = max(span for _, _, span in sized)
                col = 0
                for idx, (item, raw_width, _) in enumerate(sized):
                    width = widths[idx]
                    if len(sized) == 1:
                        desired_col = max(0, min(11, round(item["position"]["left"] * 12)))
                        width = max(4 if item["row_type"] == "metric" else 6, min(12, raw_width))
                        if desired_col + width > 12:
                            desired_col = max(0, 12 - width)
                        col = desired_col
                    _layout_entry(item, row=current_row, col=col, width=width, span=span, layout=layout)
                    col += width
                current_row += span

            def _append_metric_block(metric_items: List[Dict[str, Any]]) -> None:
                pending = list(metric_items)
                while pending:
                    chunk = pending[:4]
                    pending = pending[4:]
                    if len(chunk) == 4:
                        _append_row(chunk[:2], [6, 6])
                        _append_row(chunk[2:], [6, 6])
                    elif len(chunk) == 3:
                        _append_row(chunk, [4, 4, 4])
                    elif len(chunk) == 2:
                        _append_row(chunk, [6, 6])
                    else:
                        _append_row(chunk, [_widget_size(chunk[0])[0]])

            def _append_content_block(content_items: List[Dict[str, Any]]) -> None:
                pending = list(content_items)
                while pending:
                    if len(pending) == 1:
                        only = pending[0]
                        raw_width, _ = _widget_size(only)
                        if only["position"]["width"] >= 0.92:
                            _append_row([only], [12])
                        else:
                            _append_row([only], [raw_width])
                        return
                    if len(pending) == 2:
                        _append_row(pending)
                        return
                    _append_row(pending[:2])
                    pending = pending[2:]

            group_index = 0
            while group_index < len(row_groups):
                group_items = sorted(
                    row_groups[group_index]["items"],
                    key=lambda item: (item["position"]["left"], -item["position"]["width"]),
                )
                metric_only = all(item["row_type"] == "metric" for item in group_items)

                if metric_only:
                    merged_items = list(group_items)
                    look_ahead = group_index + 1
                    while look_ahead < len(row_groups):
                        next_items = sorted(
                            row_groups[look_ahead]["items"],
                            key=lambda item: (item["position"]["left"], -item["position"]["width"]),
                        )
                        if not next_items or not all(item["row_type"] == "metric" for item in next_items):
                            break
                        if len(merged_items) + len(next_items) > 4:
                            break
                        merged_items.extend(next_items)
                        look_ahead += 1

                    if len(merged_items) == 4:
                        _append_row(merged_items[:2], [6, 6])
                        _append_row(merged_items[2:], [6, 6])
                    elif len(merged_items) == 3:
                        _append_row(merged_items, [4, 4, 4])
                    elif len(merged_items) == 2:
                        _append_row(merged_items, [6, 6])
                    else:
                        _append_row(merged_items, [_widget_size(merged_items[0])[0]])
                    group_index = look_ahead
                    continue

                metric_items = [item for item in group_items if item["row_type"] == "metric"]
                content_items = [item for item in group_items if item["row_type"] != "metric"]
                if metric_items and content_items and len(group_items) > 2:
                    _append_metric_block(metric_items)
                    _append_content_block(content_items)
                else:
                    _append_row(group_items)
                group_index += 1
            return layout

        def _build_spatial_layout(source_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            ordered = sorted(source_items, key=lambda item: (item["position"]["top"], item["position"]["left"]))
            deferred_full_width: List[Dict[str, Any]] = []
            primary: List[Dict[str, Any]] = []
            for idx, item in enumerate(ordered):
                width, _ = _widget_size(item)
                later_metrics = any(
                    other["row_type"] == "metric"
                    and other["position"]["top"] > item["position"]["top"]
                    and other["position"]["top"] < item["position"]["top"] + 0.38
                    and other["position"]["width"] <= 0.55
                    for other in ordered[idx + 1:]
                )
                if (
                    item["row_type"] != "metric"
                    and str(item.get("viz_type") or "").lower() not in {"table", "pivot_table", "pivot_table_v2"}
                    and width >= 11
                    and later_metrics
                ):
                    deferred_full_width.append(item)
                else:
                    primary.append(item)
            queue = primary + deferred_full_width

            layout: List[Dict[str, Any]] = []
            column_heights: List[int] = [0] * 12
            for item in queue:
                width, span = _widget_size(item)
                if width >= 11:
                    width = 12
                    col = 0
                else:
                    col = max(0, min(11, round(item["position"]["left"] * 12)))
                    if col + width > 12:
                        col = max(0, 12 - width)
                end_col = max(col + 1, min(12, col + width))
                row = max(column_heights[col:end_col], default=0)
                _layout_entry(item, row=row, col=col, width=width, span=span, layout=layout)
                new_height = row + span
                for column_idx in range(col, end_col):
                    column_heights[column_idx] = new_height
            return layout

        def _build_dense_metric_stack_layout(source_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            metrics = [item for item in source_items if item["row_type"] == "metric"]
            contents = [item for item in source_items if item["row_type"] != "metric"]
            if not metrics or not contents:
                return []

            regular_contents = sorted(
                [item for item in contents if item["position"]["width"] < 0.85],
                key=lambda item: (item["position"]["top"], item["position"]["left"]),
            )
            full_width_contents = sorted(
                [item for item in contents if item["position"]["width"] >= 0.85],
                key=lambda item: (item["position"]["top"], item["position"]["left"]),
            )
            lead_content = regular_contents[0] if regular_contents else None

            layout: List[Dict[str, Any]] = []
            current_row = 0
            remaining_metrics = sorted(metrics, key=lambda item: (item["position"]["top"], item["position"]["left"]))
            side_col = 6
            side_width = 6

            if lead_content is not None:
                remaining_regular_contents = regular_contents[1:]
                lead_metrics: List[Dict[str, Any]] = []
                while remaining_metrics and len(lead_metrics) < 2:
                    metric = remaining_metrics[0]
                    if metric["position"]["top"] <= lead_content["position"]["top"] + max(0.12, lead_content["position"]["height"] * 0.6):
                        lead_metrics.append(metric)
                        remaining_metrics.pop(0)
                    else:
                        break

                # Scale left_width by actual content position width so narrow
                # content widgets don't eat half the screen when metrics are wide.
                content_pos_width = float(lead_content.get("position", {}).get("width", 0.5) or 0.5)
                if lead_metrics:
                    left_width = max(4, min(6, round(content_pos_width * 12)))
                else:
                    left_width = min(8, max(6, _widget_size(lead_content)[0]))
                left_span = _widget_size(lead_content)[1]
                side_col = left_width
                side_width = max(4, 12 - left_width)
                right_span = _widget_size(lead_metrics[0])[1] if lead_metrics else 0
                row_span = max(left_span, right_span or left_span)
                _layout_entry(lead_content, row=current_row, col=0, width=left_width, span=row_span, layout=layout)
                if len(lead_metrics) == 1:
                    _layout_entry(lead_metrics[0], row=current_row, col=side_col, width=side_width, span=row_span, layout=layout)
                elif len(lead_metrics) == 2:
                    top_metric, bottom_metric = lead_metrics
                    top_span = max(6, _widget_size(top_metric)[1])
                    bottom_span = max(6, _widget_size(bottom_metric)[1])
                    _layout_entry(top_metric, row=current_row, col=side_col, width=side_width, span=top_span, layout=layout)
                    _layout_entry(bottom_metric, row=current_row + top_span, col=side_col, width=side_width, span=bottom_span, layout=layout)
                    row_span = max(left_span, top_span + bottom_span)
                current_row += row_span
            else:
                remaining_regular_contents = regular_contents

            if remaining_metrics:
                # Pack remaining KPI tiles two-per-row.
                # When there are 4+ remaining and the side column is only half the
                # screen, place them full-width (2 per row, 6 cols each) so they
                # are large enough to read.
                n_remaining = len(remaining_metrics)
                if n_remaining >= 4 and side_width <= 6:
                    # Full-width KPI rows: 2 per row × 6 cols each
                    kpi_col_start = 0
                    kpi_w = 6
                else:
                    kpi_col_start = side_col
                    kpi_w = max(4, side_width // 2)
                for pair_start in range(0, len(remaining_metrics), 2):
                    pair = remaining_metrics[pair_start:pair_start + 2]
                    row_span = max(_widget_size(m)[1] for m in pair)
                    for k, metric in enumerate(pair):
                        _layout_entry(
                            metric,
                            row=current_row,
                            col=kpi_col_start + k * kpi_w,
                            width=kpi_w,
                            span=row_span,
                            layout=layout,
                        )
                    current_row += row_span

            for content in remaining_regular_contents:
                width, span = _widget_size(content)
                viz_type = str(content.get("viz_type") or "").lower()
                if viz_type not in {"table", "pivot_table", "pivot_table_v2"}:
                    width = 12
                else:
                    width = min(12, max(8, width))
                _layout_entry(content, row=current_row, col=0 if width >= 12 else 12 - width, width=width, span=span, layout=layout)
                current_row += span

            for content in full_width_contents:
                _, span = _widget_size(content)
                _layout_entry(content, row=current_row, col=0, width=12, span=span, layout=layout)
                current_row += span

            return layout

        def _layout_score(layout: List[Dict[str, Any]], source_items: List[Dict[str, Any]]) -> float:
            if not layout:
                return float("inf")
            score = 0.0
            total_height = 0
            for entry, item in zip(layout, source_items):
                width = max(1, int(entry.get("width") or 1))
                col = max(0, int(entry.get("col") or 0))
                span = max(1, round(float(entry.get("height") or 20) / 20.0))
                total_height = max(total_height, int(entry.get("row") or 0) + span)
                width_norm = width / 12.0
                col_norm = col / 12.0
                pos = item["position"]
                score += abs(width_norm - pos["width"]) * 14.0
                score += abs(col_norm - pos["left"]) * 10.0
                if item["row_type"] == "metric" and width >= 11:
                    score += 25.0
                if item["row_type"] != "metric" and width <= 6 and pos["width"] >= 0.6:
                    score += 12.0
            return score + total_height * 0.4

        # Shelf packer: keep source order and row grouping, but avoid sparse
        # centered singletons and giant vertical gaps.  This intentionally
        # favors a readable Navigator dashboard over pixel-perfect placement.
        def _tetris_layout(source_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            ordered = sorted(source_items, key=lambda item: (item["position"]["top"], item["position"]["left"]))
            layout: List[Dict[str, Any]] = []
            current_row = 0
            row_items: List[tuple[Dict[str, Any], int, int]] = []
            row_width = 0

            def flush_row() -> None:
                nonlocal current_row, row_items, row_width
                if not row_items:
                    return
                if len(row_items) > 1 and row_width != 12:
                    has_metric = any(item["row_type"] == "metric" for item, _, _ in row_items)
                    has_content = any(item["row_type"] != "metric" for item, _, _ in row_items)
                    if has_metric and has_content:
                        metric_slots = [idx for idx, (item, _, _) in enumerate(row_items) if item["row_type"] == "metric"]
                        content_slots = [idx for idx, (item, _, _) in enumerate(row_items) if item["row_type"] != "metric"]
                        scaled_widths = [width for _, width, _ in row_items]
                        metric_width = 4 if len(metric_slots) <= 1 else 3
                        for idx in metric_slots:
                            scaled_widths[idx] = metric_width
                        remaining = max(1, 12 - sum(scaled_widths[idx] for idx in metric_slots))
                        content_widths = _scaled_widths(
                            [scaled_widths[idx] for idx in content_slots],
                            [min(6, scaled_widths[idx]) for idx in content_slots],
                        )
                        # _scaled_widths fills to 12; re-scale that result to
                        # the space left after compact KPI cards.
                        total_content = max(1, sum(content_widths))
                        for pos, idx in enumerate(content_slots):
                            scaled_widths[idx] = max(1, round(content_widths[pos] / total_content * remaining))
                        while sum(scaled_widths) < 12:
                            grow_idx = max(content_slots, key=lambda idx: scaled_widths[idx])
                            scaled_widths[grow_idx] += 1
                        while sum(scaled_widths) > 12:
                            shrink_candidates = [idx for idx in content_slots if scaled_widths[idx] > 1]
                            if not shrink_candidates:
                                break
                            shrink_idx = max(shrink_candidates, key=lambda idx: scaled_widths[idx])
                            scaled_widths[shrink_idx] -= 1
                    else:
                        raw_widths = [width for _, width, _ in row_items]
                        min_widths = [3 if item["row_type"] == "metric" else min(6, width) for item, width, _ in row_items]
                        scaled_widths = _scaled_widths(raw_widths, min_widths)
                    row_items = [
                        (item, max(1, min(12, scaled_widths[idx])), span)
                        for idx, (item, _, span) in enumerate(row_items)
                    ]
                elif len(row_items) == 1:
                    item, width, span = row_items[0]
                    pos = item.get("position") if isinstance(item.get("position"), dict) else {}
                    pos_width = float(pos.get("width", 0.0) or 0.0)
                    if item["row_type"] != "metric" and pos_width >= 0.78:
                        width = 12
                    row_items = [(item, max(1, min(12, width)), span)]

                row_span = max(span for _, _, span in row_items)
                col = 0
                for item, width, span in row_items:
                    _layout_entry(item, row=current_row, col=col, width=width, span=span, layout=layout)
                    col += width
                current_row += row_span
                row_items = []
                row_width = 0

            def starts_new_source_band(prev: Dict[str, Any], cur: Dict[str, Any]) -> bool:
                prev_pos = prev.get("position") if isinstance(prev.get("position"), dict) else {}
                cur_pos = cur.get("position") if isinstance(cur.get("position"), dict) else {}
                prev_top = float(prev_pos.get("top", 0.0) or 0.0)
                cur_top = float(cur_pos.get("top", 0.0) or 0.0)
                prev_height = float(prev_pos.get("height", 0.0) or 0.0)
                threshold = max(0.055, min(0.12, prev_height * 0.65))
                return (cur_top - prev_top) > threshold

            previous_item: Optional[Dict[str, Any]] = None
            for item in ordered:
                width, span = _widget_size(item)
                width = max(1, min(12, width))

                is_new_source_band = previous_item is not None and starts_new_source_band(previous_item, item)
                if is_new_source_band:
                    can_continue_metric_row = (
                        item["row_type"] == "metric"
                        and row_items
                        and all(row_item["row_type"] == "metric" for row_item, _, _ in row_items)
                        and row_width + width <= 12
                    )
                    if not can_continue_metric_row:
                        flush_row()
                if row_items and row_width + width > 12:
                    can_scale_same_band = (
                        not is_new_source_band
                        and len(row_items) < 3
                        and all(row_item["row_type"] != "metric" for row_item, _, _ in row_items)
                        and item["row_type"] != "metric"
                    )
                    if not can_scale_same_band:
                        flush_row()
                if row_items and row_width + width > 12 and len(row_items) >= 3:
                    flush_row()
                row_items.append((item, width, span))
                row_width += width
                previous_item = item

            flush_row()

            return layout

        items.sort(key=lambda item: (item["position"]["top"], item["position"]["left"]))
        return _tetris_layout(items)
