"""User-level smoke test for the main Data Agent flow.

Requires running services:
  - frontend: http://127.0.0.1:3001
  - backend behind /api proxy

The script intentionally uses the UI, not direct API calls, so it catches
navigation regressions and user-visible raw errors.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
BASE_URL = os.environ.get("E2E_FRONTEND_URL", "http://127.0.0.1:3001/")
EMAIL = os.environ.get("E2E_EMAIL") or f"e2e-smoke-{int(time.time())}@example.com"
PASSWORD = os.environ.get("E2E_PASSWORD", "codex_admin_qa_2026")
REGISTER_NEW_USER = "E2E_EMAIL" not in os.environ
OUT = Path(os.environ.get("E2E_OUT", "/tmp/data_agent_e2e_user_smoke"))


def _click_button(page: Page, pattern: str, timeout: int = 5_000) -> bool:
    try:
        page.get_by_role("button", name=re.compile(pattern, re.I)).first.click(timeout=timeout)
        return True
    except Exception:
        try:
            page.get_by_text(re.compile(pattern, re.I)).first.click(timeout=timeout)
            return True
        except Exception:
            return False


def _body(page: Page) -> str:
    return page.locator("body").inner_text(timeout=5_000)


def _snapshot(page: Page, name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(OUT / f"{name}.png"), full_page=True)
    (OUT / f"{name}.txt").write_text(_body(page), encoding="utf-8")


def _collect_unnamed_buttons(page: Page) -> list[dict[str, str]]:
    return page.locator("button:visible, [role='button']:visible").evaluate_all(
        """els => els
          .map((el, index) => {
            const text = (el.innerText || el.textContent || '').trim();
            const label = el.getAttribute('aria-label') || '';
            const labelledBy = el.getAttribute('aria-labelledby') || '';
            const title = el.getAttribute('title') || '';
            return {
              index,
              tag: el.tagName.toLowerCase(),
              role: el.getAttribute('role') || '',
              text,
              label,
              labelledBy,
              title,
              className: String(el.className || '').slice(0, 120),
            };
          })
          .filter(item => !item.text && !item.label && !item.labelledBy && !item.title)"""
    )


def _goto_step(page: Page, step: str) -> None:
    y_by_step = {"upload": 38, "data": 84, "model": 132, "dashboard": 180}
    page.mouse.click(32, y_by_step[step])
    page.wait_for_timeout(1_000)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {"checks": {}, "errors": [], "bad_responses": []}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 950}, accept_downloads=True)
        page = context.new_page()
        page.on("pageerror", lambda exc: result["errors"].append(str(exc)))
        page.on(
            "response",
            lambda response: result["bad_responses"].append({"status": response.status, "url": response.url})
            if response.status >= 400 and ("127.0.0.1:3001" in response.url or "127.0.0.1:8000" in response.url)
            else None,
        )

        page.goto(BASE_URL, wait_until="networkidle", timeout=30_000)
        if REGISTER_NEW_USER:
            if not _click_button(page, "Создать аккаунт"):
                raise RuntimeError("Register mode button not found")
        page.get_by_label("Email").fill(EMAIL)
        page.get_by_label("Пароль").fill(PASSWORD)
        if not _click_button(page, "Создать аккаунт" if REGISTER_NEW_USER else "Войти"):
            raise RuntimeError("Auth submit button not found")
        page.wait_for_timeout(2_000)
        page.evaluate("localStorage.setItem('genbi:onboarding:v1','done')")
        page.reload(wait_until="networkidle", timeout=30_000)
        unnamed_buttons: list[dict[str, str]] = []

        files = [
            ROOT / "public/datasets/crm_requests_export.xlsx",
            ROOT / "public/datasets/access_matrix.xlsx",
            ROOT / "public/datasets/org_structure.xlsx",
            ROOT / "public/datasets/organizations_registry.xlsx",
        ]
        page.locator("input[type='file']").first.set_input_files([str(path) for path in files])
        page.get_by_role("button", name=re.compile("Анализировать данные", re.I)).wait_for(timeout=30_000)
        _goto_step(page, "upload")
        _snapshot(page, "01_uploaded")
        unnamed_buttons.extend(_collect_unnamed_buttons(page))
        result["checks"]["upload_has_analyze"] = "Анализировать данные" in _body(page)

        if not _click_button(page, "Анализировать данные"):
            raise RuntimeError("Analyze data button not found")
        page.get_by_text(re.compile("Качество данных", re.I)).wait_for(timeout=90_000)
        _snapshot(page, "02_quality")
        unnamed_buttons.extend(_collect_unnamed_buttons(page))
        quality_text = _body(page)
        result["checks"]["quality_loaded"] = "Качество данных" in quality_text and "17" in quality_text

        chat = page.get_by_placeholder(re.compile("Спросите", re.I)).first
        chat.fill("Какие главные проблемы качества данных ты видишь? Дай краткий список.")
        page.keyboard.press("Enter")
        page.wait_for_timeout(8_000)
        _snapshot(page, "03_chat")
        unnamed_buttons.extend(_collect_unnamed_buttons(page))
        chat_text = _body(page)
        result["checks"]["chat_quality_answer"] = "Нашёл" in chat_text and "проблем качества" in chat_text
        result["checks"]["chat_no_raw_sql_error"] = "NotFoundError" not in chat_text and "Не удалось сгенерировать SQL" not in chat_text

        _goto_step(page, "dashboard")
        page.wait_for_timeout(2_500)
        if _click_button(page, "Сгенерировать", timeout=2_000):
            page.wait_for_timeout(45_000)
        _snapshot(page, "04_dashboard")
        unnamed_buttons.extend(_collect_unnamed_buttons(page))
        dashboard_text = _body(page)
        result["checks"]["dashboard_direct"] = "Экспорт" in dashboard_text and "Выбор модели данных" not in dashboard_text
        export_opened = _click_button(page, "Экспорт")
        page.wait_for_timeout(500)
        export_text = _body(page)
        result["checks"]["export_menu"] = export_opened and all(label in export_text for label in ("PNG", "SVG", "PDF"))
        page.keyboard.press("Escape")

        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(1_000)
        _snapshot(page, "05_mobile")
        unnamed_buttons.extend(_collect_unnamed_buttons(page))
        result["checks"]["mobile_chat_collapsed"] = "ИИ-ассистент" not in _body(page)[:400]
        result["unnamed_buttons"] = unnamed_buttons
        result["checks"]["visible_buttons_named"] = len(unnamed_buttons) == 0

        browser.close()

    blocking_bad = [
        item for item in result["bad_responses"]
        if not (item["status"] == 401 and item["url"].endswith("/api/auth/me"))
    ]
    ok = all(result["checks"].values()) and not result["errors"] and not blocking_bad
    result["ok"] = ok
    result["blocking_bad_responses"] = blocking_bad
    (OUT / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PlaywrightTimeoutError as exc:
        print(f"Playwright timeout: {exc}", file=sys.stderr)
        raise SystemExit(1)
