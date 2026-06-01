# Backend Code Review

Дата обзора: 2026-06-01

## Краткая оценка

Backend выглядит как рабочий MVP, который быстро вырос в сложный продуктовый сервис. Он уже умеет загружать табличные данные, хранить пользовательские сессии, отвечать через LLM/SQL, генерировать дашборды, анализировать скриншоты и экспортировать результат в несколько BI-систем.

Технически это монолитный FastAPI-сервис с PostgreSQL для метаданных и DuckDB-файлами для пользовательских данных в сессиях. Основная ценность проекта уже реализована, но код находится на стадии, когда перед production-эксплуатацией нужно закрыть безопасность, управление ресурсами и поддерживаемость.

Оценка:

- Для MVP: 6/10.
- Для production: 4/10.

Главный вывод: проект можно развивать, но сначала стоит сделать короткий технический стабилизационный этап.

## Что сделано хорошо

- Есть понятная FastAPI-точка входа и отдельные роутеры для auth, sessions, chat, quality, image, export и admin.
- Метаданные вынесены в PostgreSQL: пользователи, сессии, загрузки, история чата, квоты.
- Данные пользователя изолируются по сессиям в DuckDB.
- Есть базовая ролевая модель: user, supertester, admin.
- Пароли хешируются через PBKDF2-SHA256.
- Есть квоты на загрузки, вопросы ассистенту, генерации дашбордов и vision-анализ.
- Есть structured logging с request context.
- Docker и compose уже подготовлены.
- Доменная часть ценная: SQL-agent, dashboard-agent, vision pipeline, export builders, интеграции с DataLens, Visiology, Foresight и Navigator.

## Критичные риски

### 1. Секреты и пароли в коде

В репозитории есть реальные или похожие на реальные секреты:

- `backend/config.py` содержит `oidc_secret`, дефолтные пароли и demo credentials.
- `backend/seed_users.py` содержит email и пароли пользователей, включая admin.
- `scripts/e2e_visiology.py` содержит пароль Visiology.

Что сделать:

- Убрать секреты из кода.
- Перенести значения в `.env` или секреты CI/CD.
- Ротировать OIDC secret и все пароли, которые могли попасть в git.
- Сделать fail-fast, если в production используется `AUTH_JWT_SECRET=change-me-in-production`.

### 2. Raw SQL endpoint без защиты

В `backend/routers/chat.py` есть endpoint `/{session_id}/query`, который выполняет пользовательский SQL напрямую в DuckDB. В LLM-пайплайне есть guard на read-only SQL, но raw endpoint его не использует.

Риск:

- Пользователь может выполнить не только `SELECT`, но и потенциально опасные операции DuckDB: `COPY`, `ATTACH`, `INSTALL`, `LOAD`, `DROP`, тяжёлые запросы и DoS по CPU/памяти.

Что сделать:

- Либо удалить endpoint из публичного API.
- Либо разрешить его только admin.
- Либо применить тот же read-only guard, что в `sql_agent.py`.

### 3. SQL injection через имена таблиц

В нескольких местах `table_name` подставляется в SQL через f-string:

- `backend/routers/upload.py`
- `backend/services/quality.py`
- `backend/services/cleaning.py`
- `backend/services/schema_analyzer.py`

Риск:

- Через специально сформированное имя таблицы можно выйти из quoted identifier и выполнить дополнительные DuckDB-команды.

Что сделать:

- Ввести единый `quote_ident(name: str)`.
- Валидировать имена таблиц по whitelist, например `[a-zA-Zа-яА-Я0-9_ -]+`, либо нормализовать до безопасного slug.
- Запретить прямое использование `f'"{table_name}"'`.

### 4. OIDC реализован небезопасно

В `backend/routers/oidc.py`:

- `verify=False` при HTTP-запросах к OIDC provider.
- `state` генерируется, но не сохраняется и не проверяется на callback.
- Нет полноценной проверки ID token через JWKS.
- `redirect_uri` может строиться из `X-Forwarded-*` заголовков.

Риск:

- MITM при обмене token/code.
- Login CSRF.
- Подмена redirect_uri при слабой настройке proxy/IdP.

Что сделать:

- Включить TLS verification.
- Сохранять `state` в httpOnly cookie или server-side session и проверять на callback.
- Проверять ID token через metadata/JWKS.
- Доверять `X-Forwarded-*` только за trusted proxy или использовать явный `OIDC_REDIRECT_URI`.

## Высокие риски

### 5. Export-интеграции доступны всем залогиненным пользователям

`backend/routers/export.py` защищён только `get_current_user`, без `require_admin` или отдельной роли.

Риск:

- Любой user может запускать публикацию в DataLens/Visiology.
- Любой user может запускать Foresight clone/publish через SSH/Playwright.
- Любой user может видеть status endpoints с инфраструктурными деталями.

Что сделать:

- Ввести отдельную роль, например `integrator` или `publisher`.
- Или временно закрыть весь `/api/export/*` через `require_admin`.
- Status endpoints тоже ограничить ролью.

### 6. Утечка DuckDB-соединений

`backend/services/session_store.py` открывает DuckDB connection на каждый `get_conn()`, но фактически не закрывает его. `close_thread_connections()` чистит thread-local storage, но `get_conn()` туда соединения не кладёт.

Риск:

- Рост числа открытых файлов.
- Локи DuckDB.
- Нестабильность под параллельной нагрузкой.

Что сделать:

- Сделать context manager для DuckDB-соединений.
- Или реально хранить connection в thread-local и закрывать его в middleware.
- Добавить простой нагрузочный smoke-test на repeated upload/preview/chat.

### 7. Foresight-интеграция слишком тяжёлая для API-процесса

`backend/foresight_service.py` содержит около 2000 строк и смешивает:

- SSH через `pexpect`.
- Playwright.
- embedded Python scripts.
- Metabase/Foresight operations.
- dashboard clone/publish logic.

Риск:

- Один тяжёлый export может подвесить API worker.
- Трудно тестировать.
- Трудно сопровождать и безопасно менять.

Что сделать:

- Вынести Foresight операции в background job.
- Разделить файл на подпакет: `ssh_client`, `metabase_scripts`, `playwright_publish`, `service`.
- Ограничить доступ через RBAC.

## Средние риски

### 8. Нет нормальных миграций БД

`backend/services/app_db.py` создаёт таблицы через `CREATE TABLE IF NOT EXISTS` на startup.

Риск:

- При изменении схемы будет сложно безопасно обновлять production.
- Нет истории изменений.
- Нет rollback/upgrade процесса.

Что сделать:

- Ввести Alembic.
- Или минимум `schema_version` таблицу и последовательные SQL migrations.

### 9. In-process очередь vision jobs

`backend/services/vision_jobs.py` хранит jobs в памяти процесса.

Риск:

- Restart теряет jobs.
- Нельзя горизонтально масштабировать backend.
- Тяжёлый vision-анализ конкурирует с API за ресурсы.

Что сделать:

- На первом этапе хранить job status в PostgreSQL.
- Дальше вынести vision worker в отдельный процесс.
- Для production рассмотреть Redis/RQ/Celery/Arq или простой Postgres-backed queue.

### 10. Слабые production defaults

Примеры:

- `AUTH_COOKIE_SECURE=false`.
- `auth_jwt_secret="change-me-in-production"`.
- `POSTGRES_PASSWORD=postgres` в локальном compose.
- CORS разрешает localhost regex с credentials.

Что сделать:

- Разделить dev/prod настройки.
- В production проверять обязательные env-переменные.
- Cookie secure включать по умолчанию для prod.

### 11. Ошибки могут отдавать внутренние детали клиенту

Некоторые endpoints возвращают `HTTPException(500, f"... {exc}")`.

Риск:

- Клиент получает SQL errors, пути, детали внешних API.

Что сделать:

- В логах оставлять `logger.exception`.
- Клиенту отдавать короткий безопасный текст.
- Добавить error code или request_id.

### 12. Тестирование недостаточно для текущей сложности

`test_backend.py` больше похож на ручной интеграционный скрипт:

- hardcoded path к файлам;
- не portable;
- не учитывает auth-cookie;
- нет pytest/CI;
- нет `.github/workflows`.

Что сделать:

- Добавить `pytest` smoke tests.
- Проверить auth/session/upload/preview/chat/export guard.
- Добавить ruff.
- Добавить CI хотя бы на lint + tests + Docker build.

## Архитектурные замечания

### 1. Несколько роутеров используют один prefix `/sessions`

`upload.py`, `chat.py`, `quality.py` все регистрируют routes под `/sessions`.

Это работает, но усложняет навигацию и повышает риск конфликтов маршрутов.

Что сделать:

- Либо оставить, но явно документировать карту API.
- Либо собрать единый `sessions` package с разделением по файлам внутри.

### 2. Fat routers

В роутерах есть бизнес-логика:

- parsing Excel/CSV;
- quality summary;
- orchestration dashboard/chat;
- vitrina load.

Что сделать:

- Постепенно выносить в сервисы.
- Роутер должен принимать input, проверять auth, вызывать service и возвращать response.

### 3. Дублирование schema/chart/json helpers

Замеченные дубли:

- `_build_schema_str` в `sql_agent.py` и `dashboard_agent.py`.
- chart type mapping в нескольких местах.
- `_strip_json` / JSON repair в разных сервисах.
- `_slug` в export/integrations.
- repeated payload validation в `export.py`.

Что сделать:

- Создать маленькие shared helpers:
  - `services/schema_context.py`
  - `utils/chart_utils.py` как единственный источник mapping
  - `utils/text_utils.py` для slug
  - `utils/llm_json.py` для JSON extraction/repair

### 4. Слишком большие файлы

Самые рискованные файлы:

- `backend/foresight_service.py` — около 2056 строк.
- `backend/services/synth.py` — около 1229 строк.
- `backend/services/visiology_client.py` — около 1068 строк.
- `backend/services/datalens_client.py` — около 890 строк.
- `backend/services/dashboard_agent.py` — около 553 строк.
- `backend/services/model_advisor.py` — около 548 строк.

Что сделать:

- Не переписывать сразу.
- Начать с выделения фасадов и внутренних helper-модулей по мере изменений.
- Сначала покрыть smoke/unit tests вокруг публичного поведения.

## Приоритетный план работ

### Этап 1. Срочная безопасность

Оценка: 1-2 дня.

- Удалить или закрыть raw SQL endpoint.
- Ввести безопасное quoting/валидацию table names.
- Закрыть `/api/export/*` через admin или publisher role.
- Убрать секреты из кода.
- Ротировать секреты, которые уже были в git.
- Включить `AUTH_COOKIE_SECURE=true` для production.

### Этап 2. Auth/OIDC hardening

Оценка: 1-2 дня.

- Включить TLS verification для OIDC.
- Реализовать проверку `state`.
- Проверять ID token.
- Убрать доверие к произвольным `X-Forwarded-*`.
- Добавить rate limiting на login.

### Этап 3. Ресурсы и стабильность

Оценка: 2-4 дня.

- Починить жизненный цикл DuckDB connections.
- Перенести health endpoint выше catch-all или убрать backend SPA fallback.
- Перестать отдавать raw exception text клиенту.
- Добавить минимальный pytest smoke suite.
- Добавить ruff и CI.

### Этап 4. Production-готовность

Оценка: 1-2 недели.

- Ввести миграции БД.
- Вынести vision/Foresight в background jobs.
- Разделить `foresight_service.py`.
- Унифицировать schema/chart/json helpers.
- Добавить job persistence в PostgreSQL.
- Добавить observability: duration metrics, job status, structured error codes.

## Итог

Проект уже содержит сильную доменную реализацию и может быть хорошей базой для продукта. Но сейчас он выглядит как быстро развивавшийся MVP: фич много, интеграции сложные, а инфраструктурная защита и сопровождение отстают.

Реально важные первые действия:

1. Закрыть SQL/security holes.
2. Убрать секреты.
3. Ограничить export-интеграции ролями.
4. Починить DuckDB connections.
5. Добавить минимальные автотесты и CI.

После этого проект станет существенно безопаснее и предсказуемее без большого переписывания архитектуры.
