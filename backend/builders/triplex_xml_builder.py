"""XML metadata builders mixin for _TriplexExportBuilder."""
from __future__ import annotations

from .triplex_xml_datamodel import _XmlDataModelMixin
from .triplex_xml_widgets import _XmlWidgetBuilderMixin

from pathlib import Path
from typing import Dict, List
import xml.etree.ElementTree as ET



class _XmlBuilderMixin(_XmlDataModelMixin, _XmlWidgetBuilderMixin):
    """Methods that build the d-section, report tables, and data model."""

    def _embedded_structure_updates(self) -> List[tuple[int, str]]:
        if getattr(self, "DEFAULT_DB_VERSION", "") == "03.136.00":
            return [(136, "SELECT 1;")]
        return []

    def _build_d_section(self, parent: ET.Element) -> None:
        """Build the <d> definitions section with table structure metadata."""
        d = ET.SubElement(parent, "d")
        jts_el = ET.SubElement(d, "jts")
        jts_path = Path(__file__).with_name("navigator_jts.json")
        try:
            jts_el.text = jts_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            pass
        for n, schema, table, is_pk in self.TABLE_DEFINITIONS:
            attrs: Dict[str, str] = {
                "n": str(n),
                "s": schema,
                "ss": table,
            }
            if is_pk:
                attrs["b"] = "true"
            ET.SubElement(d, "td", attrib=attrs)
        for version, sql in self._embedded_structure_updates():
            sd_el = ET.SubElement(d, "sd", attrib={"nversion": str(version)})
            sd_el.text = sql

    def _build_report_tables(
        self,
        data: ET.Element,
        report_id: int,
        report_name: str,
        columns: List[str],
        rows: List[dict],
    ) -> None:
        """Build analytical report tables t39..t46 for a single report/table."""
        header_id = self._next_id()
        # t39 - ao.treport
        t39 = data.find("t39")
        if t39 is None:
            t39 = ET.SubElement(data, "t39", attrib={"sCheck": "67"})
        ET.SubElement(t39, "r", attrib={
            "nid": str(report_id),
            "sname": report_name,
        })

        # t41 - ao.treportsettings (default settings)
        t41 = data.find("t41")
        if t41 is None:
            t41 = ET.SubElement(data, "t41", attrib={"sCheck": "64"})
        settings_defaults = [
            ("3", "1"),    # nRowsPerPage
            ("14", "0"),   # isAutoWidth
            ("15", "0"),   # isStriped
            ("19", "100"), # nMaxRows
        ]
        for settings_id, svalue in settings_defaults:
            ET.SubElement(t41, "r", attrib={
                "nreportid": str(report_id),
                "nsettingsid": settings_id,
                "svalue": svalue,
                "nid": str(self._next_id()),
            })
        # t42 - ao.theader
        t42 = data.find("t42")
        if t42 is None:
            t42 = ET.SubElement(data, "t42", attrib={"sCheck": "72"})
        ET.SubElement(t42, "r", attrib={
            "nid": str(header_id),
            "nreportid": str(report_id),
            "sname": "Основной",
            "isactive": "true",
            "nord": "0",
        })

        # t43 - ao.theadercolumns
        t43 = data.find("t43")
        if t43 is None:
            t43 = ET.SubElement(data, "t43", attrib={"sCheck": "118"})
        column_ids: List[int] = []
        for col_idx, col_name in enumerate(columns, start=1):
            col_id = self._next_id()
            column_ids.append(col_id)
            ET.SubElement(t43, "r", attrib={
                "nheaderid": str(header_id),
                "nid": str(col_id),
                "sname": col_name,
                "ndatacolumnid": str(col_idx),
                "nord": str(col_idx),
            })

        # t44 - ao.tcolumnsettings (column type settings)
        t44 = data.find("t44")
        if t44 is None:
            t44 = ET.SubElement(data, "t44", attrib={"sCheck": "79"})
        sample_rows = [r for r in rows if isinstance(r, dict)][:20]
        for col_idx, col_name in enumerate(columns):
            col_id = column_ids[col_idx] if col_idx < len(column_ids) else self._next_id()
            values = [r.get(col_name) for r in sample_rows if col_name in r]
            col_kind = self._column_kind(values)
            # settingsid=51 -> column type (s=string, n=number, d=datetime)
            type_code = "s"
            if col_kind == "number":
                type_code = "n"
            elif col_kind == "datetime":
                type_code = "d"
            ET.SubElement(t44, "r", attrib={
                "nheaderid": str(header_id),
                "ncolumnid": str(col_id),
                "nsettingsid": "51",
                "svalue": type_code,
                "nid": str(self._next_id()),
            })

    # Columns that are never useful as join keys
    _MEASURE_COLS: set[str] = {
        "value", "values", "amount", "total", "sum", "count",
        "avg", "min", "max", "metric_value", "measure",
        "id", "note", "unit", "series", "x", "y",
    }
    # Columns that make good join keys (dimension/identity columns)
    _PREFERRED_JOIN_COLS: set[str] = {
        "metric_code", "category", "period", "name", "metric_name",
        "date", "month", "year", "region", "department", "product",
    }
