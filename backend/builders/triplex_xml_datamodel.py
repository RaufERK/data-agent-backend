"""Data-model XML builders for Triplex export."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any, Dict, List, Optional


class _XmlDataModelMixin:
    NAV_DATAMODEL_TYPE_BY_KIND = {
        "datetime": "1",
        "string": "2",
        "number": "4",
    }

    def _build_data_model_joins(
        self,
        t73: ET.Element,
        t74: ET.Element,
        dm_id: int,
        dm_source_registry: List[Dict[str, Any]],
        dm_source_columns: Dict[int, List[tuple]],
        dm_source_map: Dict[int, int],
        t71_rows: Dict[int, ET.Element],
    ) -> None:
        """Populate t73 (links) and t74 (key pairs) for the data model.

        Strategy:
          1. Identify the consolidated KPI table (if any) as the "hub".
          2. Join chart/detail tables TO the hub via semantically
             meaningful key columns (metric_code, category, period, name).
          3. Never join two tables of the same "family" to each other.
          4. Exclude generic measure columns from join keys.
        Navigator expects t73 rows (one per joined source) and t74 key pairs.
        """
        if len(dm_source_registry) <= 1:
            # Mark the single source as root and return.
            for src_id, node in t71_rows.items():
                node.set("isroot", "true")
            return

        dm_src_ids = list(dm_source_columns.keys())

        # Classify sources: find the consolidated KPI hub
        hub_id: Optional[int] = None
        detail_ids: List[int] = []
        dm_src_id_to_reg: Dict[int, Any] = {}
        logical_name_to_dm_src: Dict[str, int] = {}
        for src in dm_source_registry:
            dm_sid = dm_source_map[src["source_id"]]
            dm_src_id_to_reg[dm_sid] = src
            logical_name = str(src.get("_logical_name") or "").strip()
            if logical_name:
                logical_name_to_dm_src[logical_name] = dm_sid
            if src.get("_is_consolidated_kpi"):
                hub_id = dm_sid
            else:
                detail_ids.append(dm_sid)

        # If no consolidated KPI, pick the source with the most columns
        # as the hub (star-schema center).
        if hub_id is None and dm_src_ids:
            hub_id = max(
                dm_src_ids,
                key=lambda sid: len(dm_source_columns.get(sid, [])),
            )
            detail_ids = [sid for sid in dm_src_ids if sid != hub_id]

        child_sources: set[int] = set()
        explicit_join_used = False
        for detail_id, src in dm_src_id_to_reg.items():
            join_specs = src.get("_join_to") if isinstance(src.get("_join_to"), list) else []
            detail_cols = {name: cid for name, cid in dm_source_columns.get(detail_id, [])}
            for join_spec in join_specs:
                if not isinstance(join_spec, dict):
                    continue
                target_name = str(join_spec.get("target_logical_name") or "").strip()
                source_col = str(join_spec.get("source_column") or "").strip()
                target_col = str(join_spec.get("target_column") or "").strip()
                if not target_name or not source_col or not target_col:
                    continue
                target_id = logical_name_to_dm_src.get(target_name)
                if target_id is None or target_id == detail_id:
                    continue
                target_cols = {name: cid for name, cid in dm_source_columns.get(target_id, [])}
                if source_col not in detail_cols or target_col not in target_cols:
                    continue
                link_id = self._next_dm_id()
                ET.SubElement(t73, "r", attrib={
                    "nid": str(link_id),
                    "ndatamodel": str(dm_id),
                    "ntype": "2",
                    "nsourceid": str(detail_id),
                    "isoptional": "true",
                })
                ET.SubElement(t74, "r", attrib={
                    "nid": str(self._next_dm_id()),
                    "ndatamodellink": str(link_id),
                    "nleftid": str(target_cols[target_col]),
                    "nrightid": str(detail_cols[source_col]),
                    "ntype": "1",
                })
                child_sources.add(detail_id)
                explicit_join_used = True

        if not explicit_join_used and hub_id is not None:
            hub_cols = {name: cid for name, cid in dm_source_columns.get(hub_id, [])}

            for detail_id in detail_ids:
                detail_cols = {name: cid for name, cid in dm_source_columns.get(detail_id, [])}
                # Find joinable columns: shared names, not measures
                common = set(hub_cols.keys()) & set(detail_cols.keys())
                common -= self._MEASURE_COLS
                if not common:
                    continue

                # Prefer well-known dimension columns; fall back to any common
                preferred = common & self._PREFERRED_JOIN_COLS
                join_col = sorted(preferred)[0] if preferred else sorted(common)[0]

                link_id = self._next_dm_id()
                ET.SubElement(t73, "r", attrib={
                    "nid": str(link_id),
                    "ndatamodel": str(dm_id),
                    "ntype": "2",  # LEFT JOIN
                    "nsourceid": str(detail_id),
                    "isoptional": "true",
                })
                ET.SubElement(t74, "r", attrib={
                    "nid": str(self._next_dm_id()),
                    "ndatamodellink": str(link_id),
                    "nleftid": str(hub_cols[join_col]),
                    "nrightid": str(detail_cols[join_col]),
                    "ntype": "1",
                })
                child_sources.add(detail_id)

        # Mark roots in t71 (sources that are never "right"/joined).
        for src_id, node in t71_rows.items():
            node.set("isroot", "false" if src_id in child_sources else "true")

    def _build_data_model(
        self,
        data: ET.Element,
        datamodel_info: ET.Element,
        dm_source_registry: List[Dict[str, Any]],
    ) -> None:
        """Build Navigator data model tables t70-t75 and t100.

        The data model describes the structure of all data sources used in the
        dashboard: which sources exist, what columns they have, and how they
        relate to each other (joins).  Navigator uses this to display an
        interactive ER-diagram and to build queries across sources.

        Tables:
          t70  (dm.tdatamodel)              – the model itself
          t71  (dm.tdatamodelsource)         – sources in the model
          t72  (dm.tdatamodelsource_column)  – columns of each source
          t73  (dm.tdatamodellink)           – join links between sources
          t74  (dm.tdatamodelkey)            – join keys (column pairs)
          t75  (dm.tdatamodelfilter)         – filters (empty for now)
          t100 (rme.tsubjectareadatamodel)   – link model to subject area
        """
        if not dm_source_registry:
            # No sources — emit empty stubs
            for tag in ("t70", "t71", "t72", "t73", "t74", "t75", "t100"):
                ET.SubElement(data, tag, attrib={"sCheck": "0"})
            return

        # ---- t70: one data model per dashboard ----
        dm_id = self._next_dm_id()
        dm_name = self._with_suffix(self.dashboard_title or "Data Model")
        dm_alias = re.sub(r"[^\w]", "_", dm_name, flags=re.UNICODE)
        dm_alias = re.sub(r"_+", "_", dm_alias).strip("_") or "data_model"

        t70 = ET.SubElement(data, "t70", attrib={"sCheck": "0"})
        ET.SubElement(t70, "r", attrib={
            "nid": str(dm_id),
            "sname": dm_name,
            "salias": dm_alias,
            "sdescription": f"Auto-generated data model for {dm_name}",
            "isai": "false",
        })

        # info/datamodel record
        ET.SubElement(datamodel_info, "r", attrib={
            "nID": str(dm_id),
            "sName": dm_name,
            "sDescription": f"Auto-generated data model for {dm_name}",
        })

        # ---- t71: one dm source per user source ----
        t71 = ET.SubElement(data, "t71", attrib={"sCheck": "0"})
        # Map source_id -> dm_source_id for column references
        dm_source_map: Dict[int, int] = {}
        # Map dm_source_id -> list of (col_name, dm_col_id) for join detection
        dm_source_columns: Dict[int, List[tuple[str, int]]] = {}
        # Keep XML nodes to update root flags after link generation.
        t71_rows: Dict[int, ET.Element] = {}

        for src in dm_source_registry:
            dm_src_id = self._next_dm_id()
            dm_source_map[src["source_id"]] = dm_src_id
            row = ET.SubElement(t71, "r", attrib={
                "nid": str(dm_src_id),
                "ndatamodel": str(dm_id),
                "nsourceid": str(src["source_id"]),
                "sname": src["name"],
                "ntype": "1",  # 1 = user source
                "isactive": "true",
                "isroot": "false",
            })
            t71_rows[dm_src_id] = row

        # ---- t72: columns for each dm source ----
        t72 = ET.SubElement(data, "t72", attrib={"sCheck": "0"})
        for src in dm_source_registry:
            dm_src_id = dm_source_map[src["source_id"]]
            col_entries: List[tuple[str, int]] = []
            sample_rows = [row for row in src.get("rows", []) if isinstance(row, dict)][:50]
            for col_idx, col_name in enumerate(src["columns"], start=1):
                dm_col_id = self._next_dm_id()
                values = [row.get(col_name) for row in sample_rows if col_name in row]
                column_kind = self._column_kind(values)
                navigator_column_type = self.NAV_DATAMODEL_TYPE_BY_KIND.get(column_kind, "2")
                # nType: 1 = static column
                ET.SubElement(t72, "r", attrib={
                    "nid": str(dm_col_id),
                    "ndatamodelsource": str(dm_src_id),
                    "ssourcecolumnname": col_name,
                    "scolumnname": col_name,
                    "scolumnnamedescription": "",
                    "soperation": "",
                    "ntype": "1",
                    "nord": str(col_idx),
                    "isactive": "true",
                    "ncolumndatatype": navigator_column_type,
                    "isai": "false",
                })
                col_entries.append((col_name, dm_col_id))
            dm_source_columns[dm_src_id] = col_entries

        # ---- t73 + t74: build meaningful joins between sources ----
        t73 = ET.SubElement(data, "t73", attrib={"sCheck": "0"})
        t74 = ET.SubElement(data, "t74", attrib={"sCheck": "0"})
        self._build_data_model_joins(
            t73, t74, dm_id, dm_source_registry,
            dm_source_columns, dm_source_map, t71_rows,
        )

        # ---- t75: filters (empty for now) ----
        ET.SubElement(data, "t75", attrib={"sCheck": "0"})

        # ---- t100: link data model to subject area ----
        t100 = ET.SubElement(data, "t100", attrib={"sCheck": "0"})
        ET.SubElement(t100, "r", attrib={
            "nid": str(self._next_dm_id()),
            "nsubjectareaid": str(self.subject_area_id),
            "ndatamodelid": str(dm_id),
            "ismainsa": "true",
        })

    @staticmethod
    def _parse_dtlastload(value: Optional[str]) -> datetime:
        if not value:
            return datetime.min
        try:
            return datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return datetime.min

    @classmethod
    def _pick_primary_datamodel_id(cls, root: ET.Element) -> Optional[str]:
        info = root.find("info")
        data = root.find("data")
        if info is None or data is None:
            return None

        model_ids: List[str] = []
        seen: set[str] = set()

        datamodel_info = info.find("datamodel")
        if datamodel_info is not None:
            for row in datamodel_info.findall("r"):
                model_id = row.attrib.get("nID")
                if model_id and model_id not in seen:
                    seen.add(model_id)
                    model_ids.append(model_id)

        t70 = data.find("t70")
        if t70 is not None:
            for row in t70.findall("r"):
                model_id = row.attrib.get("nid")
                if model_id and model_id not in seen:
                    seen.add(model_id)
                    model_ids.append(model_id)

        if not model_ids:
            return None
        if len(model_ids) == 1:
            return model_ids[0]

        source_load_by_id: Dict[str, datetime] = {}
        t12 = data.find("t12")
        if t12 is not None:
            for row in t12.findall("r"):
                source_id = row.attrib.get("nid")
                if not source_id:
                    continue
                source_load_by_id[source_id] = cls._parse_dtlastload(row.attrib.get("dtlastload"))

        source_ids_by_model: Dict[str, List[str]] = {}
        t71 = data.find("t71")
        if t71 is not None:
            for row in t71.findall("r"):
                model_id = row.attrib.get("ndatamodel")
                source_id = row.attrib.get("nsourceid")
                if not model_id or not source_id:
                    continue
                source_ids_by_model.setdefault(model_id, []).append(source_id)

        link_count_by_model: Dict[str, int] = {}
        t73 = data.find("t73")
        if t73 is not None:
            for row in t73.findall("r"):
                model_id = row.attrib.get("ndatamodel")
                if not model_id:
                    continue
                link_count_by_model[model_id] = link_count_by_model.get(model_id, 0) + 1

        best_model = model_ids[0]
        best_score: tuple[int, int, datetime, int] = (-1, -1, datetime.min, -10_000)

        for idx, model_id in enumerate(model_ids):
            source_ids = source_ids_by_model.get(model_id, [])
            latest_load = datetime.min
            for source_id in source_ids:
                latest_load = max(latest_load, source_load_by_id.get(source_id, datetime.min))

            # Prefer richer graph (more links), then wider coverage (sources),
            # then fresher load time; fall back to earlier model order.
            score = (
                link_count_by_model.get(model_id, 0),
                len(source_ids),
                latest_load,
                -idx,
            )
            if score > best_score:
                best_score = score
                best_model = model_id

        return best_model

    @classmethod
    def _normalize_datamodel_sections(cls, root: ET.Element) -> None:
        keep_model_id = cls._pick_primary_datamodel_id(root)
        if not keep_model_id:
            return

        info = root.find("info")
        data = root.find("data")
        if info is None or data is None:
            return

        datamodel_info = info.find("datamodel")
        if datamodel_info is not None:
            for row in list(datamodel_info.findall("r")):
                if row.attrib.get("nID") != keep_model_id:
                    datamodel_info.remove(row)

        t70 = data.find("t70")
        if t70 is not None:
            for row in list(t70.findall("r")):
                if row.attrib.get("nid") != keep_model_id:
                    t70.remove(row)

        t71 = data.find("t71")
        kept_dm_source_ids: set[str] = set()
        if t71 is not None:
            for row in list(t71.findall("r")):
                if row.attrib.get("ndatamodel") != keep_model_id:
                    t71.remove(row)
                    continue
                dm_source_id = row.attrib.get("nid")
                if dm_source_id:
                    kept_dm_source_ids.add(dm_source_id)

        t72 = data.find("t72")
        kept_dm_column_ids: set[str] = set()
        if t72 is not None:
            for row in list(t72.findall("r")):
                dm_source_id = row.attrib.get("ndatamodelsource")
                if dm_source_id not in kept_dm_source_ids:
                    t72.remove(row)
                    continue
                dm_column_id = row.attrib.get("nid")
                if dm_column_id:
                    kept_dm_column_ids.add(dm_column_id)

        t73 = data.find("t73")
        kept_dm_link_ids: set[str] = set()
        if t73 is not None:
            for row in list(t73.findall("r")):
                if row.attrib.get("ndatamodel") != keep_model_id:
                    t73.remove(row)
                    continue
                source_id = row.attrib.get("nsourceid")
                if source_id and source_id not in kept_dm_source_ids:
                    t73.remove(row)
                    continue
                link_id = row.attrib.get("nid")
                if link_id:
                    kept_dm_link_ids.add(link_id)

        t74 = data.find("t74")
        if t74 is not None:
            for row in list(t74.findall("r")):
                link_id = row.attrib.get("ndatamodellink")
                if link_id not in kept_dm_link_ids:
                    t74.remove(row)
                    continue
                left_column_id = row.attrib.get("nleftid")
                right_column_id = row.attrib.get("nrightid")
                if left_column_id and left_column_id not in kept_dm_column_ids:
                    t74.remove(row)
                    continue
                if right_column_id and right_column_id not in kept_dm_column_ids:
                    t74.remove(row)

        t100 = data.find("t100")
        if t100 is not None:
            for row in list(t100.findall("r")):
                if row.attrib.get("ndatamodelid") != keep_model_id:
                    t100.remove(row)
