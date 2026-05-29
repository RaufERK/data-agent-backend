#!/usr/bin/env python3
"""
Build a demo Data Agent dashboard .pefx for Foresight import.

.pefx format rules (learned from real Foresight exports):
  - content.xml lists ALL objects (existing + new) as pef{N} nodes
  - Existing repo objects: listed in content.xml with real keys, NO pef{N}.xml in ZIP
  - New objects: listed in content.xml AND have pef{N}.xml in ZIP
  - KEYSD for existing stable objects = their actual keysd value
  - KEYSD = 12916 for new/recently-created objects

Object layout:
  pef1  F_EXAMPLES (1654, existing, no zip file)  -- parent folder
  pef2  F_DB_CONNS (14679, existing, no zip file) -- DB connections folder
  pef3  DB_REPO (554, existing, no zip file)       -- PostgreSQL connection
  pef4  DA_FOLDER (new, zip pef4.xml)              -- our demo folder
  pef5  DA_TABLE_FACTS (new, zip pef5.xml)         -- SQL fact table
  pef6  DA_CUBE_MAIN (new, zip pef6.xml)           -- standard cube
  pef7  DA_SRC_AND_FLD (new, zip pef7.xml)         -- source fields
  pef8  DA_NUM_ROWS (new, zip pef8.xml)            -- num rows
  pef9  DA_DASHBOARD (new, zip pef9.xml)           -- info panel

Usage:
    python3 scripts/build_demo_pefx.py [--output path/to/output.pefx]
"""
from __future__ import annotations

import argparse
import base64
import zipfile
from pathlib import Path


def sql_to_b64(sql: str) -> str:
    """Encode SQL as UTF-16 LE base64 (Foresight S64 format), split at 64 chars."""
    encoded = base64.b64encode(sql.encode("utf-16-le")).decode("ascii")
    lines = [encoded[i : i + 64] for i in range(0, len(encoded), 64)]
    return "\n".join(lines)


SQL_FACTS = (
    "SELECT key, period, category, revenue, expenses, profit, "
    "margin_pct, orders, customers, avg_order "
    "FROM t_data_agent_demo"
)

FT_STR = "1"
FT_INT = "2"
FT_NUM = "6"

_NODE_ATTRS = (
    'TYPE="1" LBL="" ENB="1" ICS="1" UPP="16" UTC="0" UTB="3" UTU="0" '
    'UT="0" UPO="0" UM="3" RCH="0" PK="" PD="FALSE" BM="0" URS="FALSE" '
    'AERU="FALSE" FUI="FALSE" FKI="FALSE" RAC="FALSE" IF="FALSE" ATO="0" '
    'KNN="FALSE" SCRSHT="FALSE" FLDS_ONLY="" AT="0"'
)
_PRMR = '<PRMR_KEYS_ONLY P4STMFMT="TEXT" S="" T="N"><X>IlvkrIAQrUuNROUm8H9uDAIAAAAA</X></PRMR_KEYS_ONLY>'


def _node(pef: str, key: int, obj_id: str, obj_name: str, obj_class: int,
          parent: int, keysd: int, tms: str = "01.01.2026 00:00:00",
          internal: bool = False, do: str = "") -> str:
    int_attr = ' INTERNAL=""' if internal else ""
    do_tag = f"<DO>{do}</DO>" if do else ""
    return (
        f'<NODE FILE_NAME_TAG="{pef}" {_NODE_ATTRS} OI="{obj_id}" ON="">'
        f'<FOLDER/>'
        f'<OBJECT KEY="{key}" ID="{obj_id}" KEYSD="{keysd}" NAME="{obj_name}" '
        f'DESCRIPTION="" MBSOURCE="" VER="1" CLASS="{obj_class}" PARENT="{parent}" '
        f'ElementDependenciesTracking="0" TMS="{tms}"{int_attr}/>'
        f'{do_tag}'
        f'{_PRMR}'
        f'</NODE>'
    )


def build_content_xml() -> str:
    # Existing objects — real keys, real keysd, NO zip file for these pef numbers
    # They appear in content.xml so Foresight knows the dependency chain
    n_f_examples = _node("pef1", 1654,  "F_EXAMPLES", "Примеры данных и объектов",
                          0, 0, keysd=4017, tms="09.10.2025 17:53:47")
    n_f_db_conns  = _node("pef2", 14679, "F_DB_CONNS", "Подключения к базам данных",
                          0, 1654, keysd=12916)
    n_db_repo     = _node("pef3", 554,   "DB_REPO", "Подключение к базе данных репозитория",
                          513, 14679, keysd=3663, internal=True)

    # New objects — dummy keys (Foresight reassigns them), keysd=12916, WITH zip files
    n_da_folder   = _node("pef4", 90010, "DA_FOLDER", "Data Agent Demo",
                          0, 1654, keysd=12916)
    n_da_facts    = _node("pef5", 90001, "DA_TABLE_FACTS", "Data Agent: Таблица фактов",
                          770, 90010, keysd=12916, internal=True,
                          do='<D K="554"/>')
    n_da_cube     = _node("pef6", 90002, "DA_CUBE_MAIN", "Data Agent: Основной куб",
                          1281, 90010, keysd=12916, internal=True,
                          do='<D K="90001"/><D K="90003"/><D K="90004"/>')
    n_da_srcfld   = _node("pef7", 90003, "DA_SRC_AND_FLD", "Поля источника",
                          1039, 90002, keysd=12916, internal=True)
    n_da_numrows  = _node("pef8", 90004, "DA_NUM_ROWS", "Номера строк",
                          1040, 90002, keysd=12916, internal=True)
    n_da_dash     = _node("pef9", 90011, "DA_DASHBOARD", "Data Agent: Аналитический дашборд",
                          9216, 90010, keysd=12916, internal=True,
                          do='<D K="90002"/>')

    nodes = (n_f_examples + n_f_db_conns + n_db_repo
             + n_da_folder + n_da_facts + n_da_cube
             + n_da_srcfld + n_da_numrows + n_da_dash)

    return (
        '<METABASE_UPDATE_TAG><UPDATE><LINK V="0"><ODATA/></LINK>'
        '<CONTENT ROR="3" AO="36" UTC="0" UTB="2" UT="24" '
        'DESC="Data Agent Demo Dashboard" LC="0" ULVF="1" IAA="0" ARA="0" PRTDC="0">'
        '<CPC>'
        '<CP K="1" I="CREATEUSERNAME" N="ITEM"><VA T="S" V="FP_ADMIN"/></CP>'
        '<CP K="2" I="CREATEUSERSID" N="ITEM"><VA T="S" V="PS-1-1"/></CP>'
        '<CP K="3" I="CREATEWORKSTATION" N="ITEM"><VA T="S" V="rocky-fp10"/></CP>'
        '<CP K="4" I="CREATEMETABASE" N="ITEM"><VA T="S" V=""/></CP>'
        '<CP K="5" I="CREATEPLATFORMVERSION" N="ITEM"><VA T="S" V="Релиз 10.8.132.0 LTS x64"/></CP>'
        '<CP K="6" I="CREATETIMESTAMP" N="ITEM"><VA T="D" V="46150.0"/></CP>'
        '</CPC>'
        f'<PARENTS_ROOT_TAG LBL="" ENB="1"><FOLDER>{nodes}</FOLDER></PARENTS_ROOT_TAG>'
        '</CONTENT></UPDATE></METABASE_UPDATE_TAG>'
    )


_EMPTY_OBJ = '<ALL><PARAMS NUL="1"/><OBJECT><R><L V="0"><OD CM="0" PLVERSION="10.8.132.0" EDITLICENSE=""/></L><O/></R>\n</OBJECT></ALL>'


def build_table_facts_xml(sql: str) -> str:
    s64 = sql_to_b64(sql)
    return (
        f'<ALL><PARAMS NUL="1"/><OBJECT><R>'
        f'<L V="0">'
        f'<L O="554" C="513" PAR="14679" II="FALSE">'
        f'<O I="DB_REPO" N="Подключение к базе данных репозитория" D="" S="FALSE"/>'
        f'</L>'
        f'<OD CM="0" PLVERSION="10.8.132.0" EDITLICENSE=""/>'
        f'</L>'
        f'<O><Q SI="0" TRIP="0"><N N=""/><DL DL="1"/>'
        f'<FC>'
        f'<F K="1" I="FIELD 1" N="key" T="{FT_INT}" DD="0" S="0" P="0" M="0" R="0" DF="-1" CS="1" SS="0"/>'
        f'<F K="2" I="FIELD 2" N="period" T="{FT_STR}" DD="0" S="16777215" P="0" M="0" R="0" DF="-1" CS="1" SS="0"/>'
        f'<F K="3" I="FIELD 3" N="category" T="{FT_STR}" DD="0" S="16777215" P="0" M="0" R="0" DF="-1" CS="1" SS="0"/>'
        f'<F K="4" I="FIELD 4" N="revenue" T="{FT_NUM}" DD="0" S="0" P="2" M="0" R="0" DF="-1" CS="1" SS="0"/>'
        f'<F K="5" I="FIELD 5" N="expenses" T="{FT_NUM}" DD="0" S="0" P="2" M="0" R="0" DF="-1" CS="1" SS="0"/>'
        f'<F K="6" I="FIELD 6" N="profit" T="{FT_NUM}" DD="0" S="0" P="2" M="0" R="0" DF="-1" CS="1" SS="0"/>'
        f'<F K="7" I="FIELD 7" N="margin_pct" T="{FT_NUM}" DD="0" S="0" P="2" M="0" R="0" DF="-1" CS="1" SS="0"/>'
        f'<F K="8" I="FIELD 8" N="orders" T="{FT_INT}" DD="0" S="0" P="0" M="0" R="0" DF="-1" CS="1" SS="0"/>'
        f'<F K="9" I="FIELD 9" N="customers" T="{FT_INT}" DD="0" S="0" P="0" M="0" R="0" DF="-1" CS="1" SS="0"/>'
        f'<F K="10" I="FIELD 10" N="avg_order" T="{FT_NUM}" DD="0" S="0" P="2" M="0" R="0" DF="-1" CS="1" SS="0"/>'
        f'</FC>'
        f'<T C="1" SAP="0" NW=""><S64 D=""><![CDATA[\n{s64}\n]]></S64></T>'
        f'<STO AD="1"/></Q></O>'
        f'</R>\n</OBJECT></ALL>'
    )


def build_cube_xml() -> str:
    return (
        '<ALL><PARAMS NUL="1"/><OBJECT><R>'
        '<L V="0">'
        '<L O="90001" C="770" PAR="90010" II="TRUE">'
        '<O I="DA_TABLE_FACTS" N="Data Agent: Таблица фактов" D="" S="FALSE"/>'
        '</L>'
        '<OD CM="0" PLVERSION="10.8.132.0" EDITLICENSE=""/>'
        '</L>'
        '<O DL="0" FC="0" CCQ="0" CLCSQ="1" CLDCSQ="1" CHQ="1" CHDCSQ="1" CLQS="1" CLFQS="1" '
        'NOFQ="0" ECSQ="1" CSN="Стандартный куб" DRL="0" MD="0" INL="0" SNF="1" UCC="0" DDCS="0" '
        'DDCSTH="2" UCIT="0" RCCL="1" FFAOA="0" FCQ="0" RFCQ="0" RBCQ="0" RCACH="0" PRCACH="0" '
        'JCS="0" UCSR="0" AICS="0" CDSQ="0" GDS="0" MSDS="0">'
        '<FACTS>'
        '<F K="4" I="FIELD 4" N="Выручка" FBN="revenue"/>'
        '<F K="5" I="FIELD 5" N="Расходы" FBN="expenses"/>'
        '<F K="6" I="FIELD 6" N="Прибыль" FBN="profit"/>'
        '<F K="7" I="FIELD 7" N="Маржа %" FBN="margin_pct"/>'
        '<F K="8" I="FIELD 8" N="Заказы" FBN="orders"/>'
        '<F K="9" I="FIELD 9" N="Клиенты" FBN="customers"/>'
        '<F K="10" I="FIELD 10" N="Средний чек" FBN="avg_order"/>'
        '</FACTS>'
        '<DIM>'
        '<DDIM K="2" I="FIELD 2" N="Период" FBN="period" SDIM="1" SDIML="1" MDIM="0" '
        'SLCA="0" SLCB="0" CLCA="0" LCUQ="0" LCQ="0" LCAQ="0" LCBQ="0"><EL/></DDIM>'
        '<DDIM K="3" I="FIELD 3" N="Категория" FBN="category" SDIM="1" SDIML="1" MDIM="0" '
        'SLCA="0" SLCB="0" CLCA="0" LCUQ="0" LCQ="0" LCAQ="0" LCBQ="0"><EL/></DDIM>'
        '</DIM>'
        '</O>'
        '</R>\n</OBJECT></ALL>'
    )


def build_src_fields_xml() -> str:
    return (
        '<ALL><PARAMS NUL="1"/><OBJECT><R>'
        '<L V="0"><OD CM="0" PLVERSION="10.8.132.0" EDITLICENSE=""/></L>'
        '<O><FIELDS>'
        '<F K="1" N="key" T="2" S="0" P="0"/>'
        '<F K="2" N="period" T="1" S="100" P="0"/>'
        '<F K="3" N="category" T="1" S="100" P="0"/>'
        '<F K="4" N="revenue" T="6" S="0" P="2"/>'
        '<F K="5" N="expenses" T="6" S="0" P="2"/>'
        '<F K="6" N="profit" T="6" S="0" P="2"/>'
        '<F K="7" N="margin_pct" T="6" S="0" P="2"/>'
        '<F K="8" N="orders" T="2" S="0" P="0"/>'
        '<F K="9" N="customers" T="2" S="0" P="0"/>'
        '<F K="10" N="avg_order" T="6" S="0" P="2"/>'
        '</FIELDS></O>'
        '</R>\n</OBJECT></ALL>'
    )


def build_num_rows_xml() -> str:
    return (
        '<ALL><PARAMS NUL="1"/><OBJECT><R>'
        '<L V="0"><OD CM="0" PLVERSION="10.8.132.0" EDITLICENSE=""/></L>'
        '<O><NUMROWS ROWCOL="1" WKSKEY="-1"/></O>'
        '</R>\n</OBJECT></ALL>'
    )


def build_dashboard_xml() -> str:
    return (
        '<ALL><PARAMS NUL="1"/><OBJECT><R>'
        '<L V="0">'
        '<L O="90002" C="1281" PAR="90010" II="TRUE">'
        '<O I="DA_CUBE_MAIN" N="Data Agent: Основной куб" D="" S="FALSE"/>'
        '</L>'
        '<OD CM="0" PLVERSION="10.8.132.0" EDITLICENSE=""/>'
        '</L>'
        '<O DELAYEDLOAD="TRUE" DAEAL="1" IS_A_S="1" DSID="DS0" GDP="TRUE" '
        'ExecCombined="FALSE" COCD="FALSE" '
        'VS="{&quot;version&quot;:&quot;3&quot;,&quot;isDataAndFieldsVisible&quot;:true}" AP="1">'
        '<DashboardMd VER="1"><AA/></DashboardMd>'
        '<Sources>'
        '<Source K="1" I="ITEM" N="Data Agent Main">'
        '<Layer SemObject="1"/><Exprs/><Rels/>'
        '<FieldsMd>'
        '<FieldMd DK="90002" FK="2"/>'
        '<FieldMd DK="90002" FK="3"/>'
        '<FieldMd DK="90002" FK="4"/>'
        '<FieldMd DK="90002" FK="5"/>'
        '<FieldMd DK="90002" FK="6"/>'
        '<FieldMd DK="90002" FK="7"/>'
        '<FieldMd DK="90002" FK="8"/>'
        '</FieldsMd>'
        '<FieldsChanged/>'
        '</Source>'
        '</Sources>'
        '<SyncGroups/>'
        '<MetaModels>'
        '<MetaModel K="1" I="ITEM" N="ITEM" NU="TRUE" NUV="FALSE" AU="TRUE" FC="FALSE" QSA="" SourceKey="1">'
        '<Fields_0/>'
        '<Fields_1>'
        '<Field K="2" I="FIELD 2" N="Период" AG="1" FN="" V="1" DK="-1" FK="2" FT="0" O="1"/>'
        '<Field K="3" I="FIELD 3" N="Категория" AG="1" FN="" V="1" DK="-1" FK="3" FT="0" O="2"/>'
        '</Fields_1>'
        '<Fields_2>'
        '<Field K="4" I="FIELD 4" N="Выручка" AG="2" FN="SUM" V="1" DK="-1" FK="4" FT="0" O="1"/>'
        '<Field K="5" I="FIELD 5" N="Расходы" AG="2" FN="SUM" V="1" DK="-1" FK="5" FT="0" O="2"/>'
        '<Field K="6" I="FIELD 6" N="Прибыль" AG="2" FN="SUM" V="1" DK="-1" FK="6" FT="0" O="3"/>'
        '<Field K="7" I="FIELD 7" N="Маржа %" AG="2" FN="AVG" V="1" DK="-1" FK="7" FT="0" O="4"/>'
        '<Field K="8" I="FIELD 8" N="Заказы" AG="2" FN="SUM" V="1" DK="-1" FK="8" FT="0" O="5"/>'
        '</Fields_2>'
        '</MetaModel>'
        '</MetaModels>'
        '<Widgets>'
        '<Widget K="1" T="1" X="0" Y="0" W="8" H="3" V="1" I="Выручка по периодам и категориям" DSID="DS0">'
        '<TableReport>'
        '<MM K="1"><Rows><D FK="2"/></Rows><Cols><D FK="3"/></Cols><Vals><V FK="4" A="SUM"/></Vals></MM>'
        '</TableReport>'
        '</Widget>'
        '<Widget K="2" T="4" X="8" Y="0" W="4" H="3" V="1" I="Выручка по категориям" DSID="DS0">'
        '<Chart CT="Pie">'
        '<MM K="1"><Rows><D FK="3"/></Rows><Vals><V FK="4" A="SUM"/></Vals></MM>'
        '</Chart>'
        '</Widget>'
        '<Widget K="3" T="4" X="0" Y="3" W="8" H="3" V="1" I="Динамика по периодам" DSID="DS0">'
        '<Chart CT="Bar">'
        '<MM K="1"><Rows><D FK="2"/></Rows><Cols/><Vals><V FK="4" A="SUM"/><V FK="6" A="SUM"/></Vals></MM>'
        '</Chart>'
        '</Widget>'
        '<Widget K="4" T="5" X="8" Y="3" W="4" H="1" V="1" I="Общая выручка" DSID="DS0">'
        '<Indicator><MM K="1"><Vals><V FK="4" A="SUM"/></Vals></MM></Indicator>'
        '</Widget>'
        '<Widget K="5" T="5" X="8" Y="4" W="4" H="1" V="1" I="Общая прибыль" DSID="DS0">'
        '<Indicator><MM K="1"><Vals><V FK="6" A="SUM"/></Vals></MM></Indicator>'
        '</Widget>'
        '<Widget K="6" T="5" X="8" Y="5" W="4" H="1" V="1" I="Всего заказов" DSID="DS0">'
        '<Indicator><MM K="1"><Vals><V FK="8" A="SUM"/></Vals></MM></Indicator>'
        '</Widget>'
        '</Widgets>'
        '</O>'
        '</R>\n</OBJECT></ALL>'
    )


def build_pefx(output_path: Path) -> None:
    """Assemble the .pefx file.

    content.xml has pef1–pef9.
    Only pef4–pef9 (new objects) have corresponding XML files in the ZIP.
    pef1–pef3 (existing objects) appear in content.xml only — Foresight
    resolves them by key from the live repository.
    """
    files: dict[str, str] = {
        "content.xml": build_content_xml(),
        # New objects only:
        "pef4.xml": _EMPTY_OBJ,                      # DA_FOLDER (class 0)
        "pef5.xml": build_table_facts_xml(SQL_FACTS), # DA_TABLE_FACTS (class 770)
        "pef6.xml": build_cube_xml(),                 # DA_CUBE_MAIN (class 1281)
        "pef7.xml": build_src_fields_xml(),           # DA_SRC_AND_FLD (class 1039)
        "pef8.xml": build_num_rows_xml(),             # DA_NUM_ROWS (class 1040)
        "pef9.xml": build_dashboard_xml(),            # DA_DASHBOARD (class 9216)
    }

    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content.encode("utf-8"))

    print(f"Created: {output_path} ({output_path.stat().st_size / 1024:.1f} KB)")
    print(f"ZIP entries: {list(files.keys())}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Data Agent demo dashboard .pefx")
    parser.add_argument(
        "--output", type=Path,
        default=Path("data_agent_demo_dashboard.pefx"),
    )
    args = parser.parse_args()
    build_pefx(args.output)


if __name__ == "__main__":
    main()
