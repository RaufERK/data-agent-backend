from __future__ import annotations

import base64
import json
import os
import subprocess
from dataclasses import dataclass
from typing import Any, Dict
from urllib.parse import urljoin


@dataclass(frozen=True)
class NavigatorImportConfig:
    base_url: str
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str
    access_login: str


def _run_psql_sql(sql: str, config: NavigatorImportConfig) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-h",
            config.db_host,
            "-p",
            str(config.db_port),
            "-U",
            config.db_user,
            "-d",
            config.db_name,
            "-Atq",
        ],
        input=sql,
        text=True,
        capture_output=True,
        env={**os.environ, "PGPASSWORD": config.db_password},
        check=False,
    )


def _run_json_sql(sql: str, config: NavigatorImportConfig, error_message: str) -> Dict[str, Any]:
    result = _run_psql_sql(sql, config)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or error_message)
    raw = (result.stdout or "").strip().splitlines()
    if not raw:
        raise RuntimeError(error_message)
    return json.loads(raw[-1])


def import_xml_to_navigator(xml_bytes: bytes, config: NavigatorImportConfig) -> Dict[str, Any]:
    xml_b64 = base64.b64encode(xml_bytes).decode("ascii")
    sql = f"""
CREATE TEMP TABLE _tmp_import_out(payload json);
DO $$
DECLARE
    j json := '{{}}'::json;
BEGIN
    CALL arm.setImportSubjectAreaData_v40('{{}}'::json, j, decode('{xml_b64}', 'base64'), 0);
    INSERT INTO _tmp_import_out(payload) VALUES (j);
END $$;
SELECT payload::text FROM _tmp_import_out;
"""
    return _run_json_sql(sql, config, "Navigator import failed")


def ensure_subject_area_access(subject_area_id: int, config: NavigatorImportConfig) -> Dict[str, Any]:
    escaped_login = config.access_login.replace("'", "''")
    sql = f"""
WITH target_user AS (
    SELECT nid
    FROM rm.tuser
    WHERE lower(slogin) = lower('{escaped_login}')
    LIMIT 1
),
ins_usersa AS (
    INSERT INTO rm.tusersubjectarea(
        nuserid,
        nsubjectareaid,
        isowner,
        ismanual,
        ismanualglobalroleaccess,
        isauto,
        isautoglobalroleaccess
    )
    SELECT nid, {subject_area_id}, TRUE, TRUE, TRUE, FALSE, FALSE
    FROM target_user
    ON CONFLICT (nuserid, nsubjectareaid) DO NOTHING
    RETURNING nid
),
existing_usersa AS (
    SELECT nid FROM ins_usersa
    UNION ALL
    SELECT up.nid
    FROM rm.tusersubjectarea up
    JOIN target_user tu ON tu.nid = up.nuserid
    WHERE up.nsubjectareaid = {subject_area_id}
    LIMIT 1
),
ins_env AS (
    INSERT INTO rm.tusersubjectareaenvironment(
        nusersubjectareaid,
        nenvironmentid,
        ismanual,
        isauto
    )
    SELECT nid, 1, TRUE, FALSE
    FROM existing_usersa
    ON CONFLICT DO NOTHING
    RETURNING nusersubjectareaid
)
SELECT json_build_object(
    'user_subject_area_id', (SELECT nid FROM existing_usersa),
    'has_environment', EXISTS(SELECT 1 FROM ins_env) OR EXISTS(
        SELECT 1
        FROM rm.tusersubjectareaenvironment
        WHERE nusersubjectareaid = (SELECT nid FROM existing_usersa)
          AND nenvironmentid = 1
    )
)::text;
"""
    return _run_json_sql(sql, config, "Navigator access grant failed")


def grant_subject_area_source_access(subject_area_id: int, config: NavigatorImportConfig) -> Dict[str, Any]:
    sql = f"""
DO $$
DECLARE
    source_row record;
BEGIN
    FOR source_row IN
        SELECT us.stable
        FROM rme.tsubjectareausersource saus
        JOIN data.tusersource us ON us.nid = saus.nusersourceid
        WHERE saus.nsubjectareaid = {subject_area_id}
          AND us.stable IS NOT NULL
    LOOP
        EXECUTE format(
            'GRANT SELECT ON TABLE src.%I TO navi_app, navi_run, as_admin_read, as_admin',
            source_row.stable
        );
    END LOOP;
END $$;

SELECT json_build_object(
    'granted_source_count',
    (
        SELECT COUNT(*)
        FROM rme.tsubjectareausersource saus
        JOIN data.tusersource us ON us.nid = saus.nusersourceid
        WHERE saus.nsubjectareaid = {subject_area_id}
          AND us.stable IS NOT NULL
    )
)::text;
"""
    return _run_json_sql(sql, config, "Navigator source grant failed")


def query_import_state(subject_area_id: int, dashboard_id: int, config: NavigatorImportConfig) -> Dict[str, Any]:
    sql = f"""
WITH sa AS (
    SELECT nid, sname_ru, sshortname_ru
    FROM rm.tsubjectarea
    WHERE nid = {subject_area_id}
),
sa_dashboard AS (
    SELECT nsubjectareaid, ndashboardid
    FROM rme.tsubjectareadashboard
    WHERE nsubjectareaid = {subject_area_id}
),
sa_sources AS (
    SELECT COUNT(*) AS cnt
    FROM rme.tsubjectareausersource
    WHERE nsubjectareaid = {subject_area_id}
),
sa_connections AS (
    SELECT COUNT(*) AS cnt
    FROM rme.tsubjectareauserconnection
    WHERE nsubjectareaid = {subject_area_id}
),
sa_models AS (
    SELECT COUNT(*) AS cnt
    FROM rme.tsubjectareadatamodel
    WHERE nsubjectareaid = {subject_area_id}
),
dashboard AS (
    SELECT nid, sdashboardtitle_ru, nreportid
    FROM ui.tdashboard
    WHERE nid = {dashboard_id}
)
SELECT json_build_object(
    'subject_area', (SELECT row_to_json(sa) FROM sa),
    'dashboard_link_count', (SELECT COUNT(*) FROM sa_dashboard),
    'linked_dashboard_ids', COALESCE((SELECT json_agg(ndashboardid) FROM sa_dashboard), '[]'::json),
    'subject_area_user_source_count', (SELECT cnt FROM sa_sources),
    'subject_area_connection_count', (SELECT cnt FROM sa_connections),
    'subject_area_data_model_count', (SELECT cnt FROM sa_models),
    'dashboard', (SELECT row_to_json(dashboard) FROM dashboard)
)::text;
"""
    return _run_json_sql(sql, config, "Navigator validation failed")


def resolve_imported_subject_area(
    expected_subject_area_id: int,
    subject_area_name: str,
    config: NavigatorImportConfig,
) -> Dict[str, Any]:
    escaped_name = subject_area_name.replace("'", "''")
    sql = f"""
SELECT json_build_object(
    'nid', nid,
    'sname_ru', sname_ru,
    'sshortname_ru', sshortname_ru
)::text
FROM rm.tsubjectarea
WHERE nid IN ({expected_subject_area_id}, {-expected_subject_area_id})
   OR sname_ru = '{escaped_name}'
ORDER BY CASE WHEN sname_ru = '{escaped_name}' THEN 0 ELSE 1 END, ABS(nid) DESC
LIMIT 1;
"""
    return _run_json_sql(sql, config, "Imported subject area not found")


def resolve_imported_dashboard(subject_area_id: int, config: NavigatorImportConfig) -> Dict[str, Any]:
    sql = f"""
WITH linked AS (
    SELECT ndashboardid
    FROM rme.tsubjectareadashboard
    WHERE nsubjectareaid = {subject_area_id}
    ORDER BY ndashboardid DESC
    LIMIT 1
),
dashboard AS (
    SELECT d.nid, d.sdashboardtitle_ru, d.nreportid
    FROM ui.tdashboard d
    JOIN linked l ON l.ndashboardid = d.nid
)
SELECT json_build_object(
    'linked_dashboard_id', (SELECT ndashboardid FROM linked),
    'dashboard', (SELECT row_to_json(dashboard) FROM dashboard)
)::text;
"""
    return _run_json_sql(sql, config, "Imported dashboard not found")


def query_dashboard_screen(dashboard_id: int, config: NavigatorImportConfig) -> Dict[str, Any]:
    sql = f"""
SELECT json_build_object(
    'screen_id', nid,
    'screen_title', stitle_ru
)::text
FROM ui.tscreen_v30
WHERE ndashboardid = {dashboard_id}
ORDER BY nid
LIMIT 1;
"""
    result = _run_psql_sql(sql, config)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Navigator screen lookup failed")
    raw = (result.stdout or "").strip().splitlines()
    if not raw:
        return {}
    return json.loads(raw[-1])


def build_dashboard_url(dashboard_id: int, screen_id: int | None, config: NavigatorImportConfig) -> str:
    base = config.base_url.rstrip("/") + "/"
    if screen_id is None:
        return urljoin(base, f"gdash/{dashboard_id}")
    return urljoin(base, f"gdash/{dashboard_id}/{screen_id}")
