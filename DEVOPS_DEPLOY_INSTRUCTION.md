# Инструкция для DevOps: Data Agent

## Назначение

Этот документ описывает, как подключать Data Agent к CI/CD после разделения проекта на frontend и backend.

Исходная общая инструкция бизнес-инкубатора остается актуальной как регламент. Этот документ — проектная инструкция именно для Data Agent.

## Репозитории

Проект разделен на два репозитория, потому что frontend и backend имеют разные стеки и разные Dockerfile:

| Репозиторий | Назначение | Стек | Dockerfile |
| --- | --- | --- | --- |
| `data-agent-frontend` | Web UI | React, TypeScript, Vite, nginx | `Dockerfile` |
| `data-agent-backend` | API и общий deploy-контур | Python 3.12, FastAPI, PostgreSQL | `Dockerfile` |

Рекомендуемые имена репозиториев по правилам бизнес-инкубатора:

- `data-agent-frontend`
- `data-agent-backend`

## Что собирается в CI

### Frontend image

Репозиторий: `data-agent-frontend`

Сборка:

```bash
docker build -t <registry>/data-agent-frontend:<tag> .
```

Что делает image:

- устанавливает npm-зависимости;
- собирает React/Vite через `npm run build`;
- кладет статику в nginx;
- проксирует `/api/*` в backend через `nginx.conf`.

Контейнерный порт: `80`.

### Backend image

Репозиторий: `data-agent-backend`

Сборка:

```bash
docker build -t <registry>/data-agent-backend:<tag> .
```

Что делает image:

- устанавливает Python-зависимости из `backend/requirements.txt`;
- устанавливает системные зависимости для Playwright/Tesseract;
- запускает FastAPI через uvicorn.

Контейнерный порт: `8000`.

Healthcheck endpoint:

```text
/api/health
```

## Что должно быть в CD-контуре

CD-контур должен использовать файл:

```text
docker-compose.cd.yml
```

В этом же репозитории уже есть:

- `docker-compose.cd.yml` — compose для бизнес-инкубатора;
- `.env.example` — пример переменных окружения;
- `STATE.MD` — статус доставки, сейчас `ENABLED`.

Для запуска в CD нужно создать `.env` на основе `.env.example`.

## Обязательные переменные окружения

Минимальный набор:

```env
FRONTEND_DOCKER_IMAGE=<registry>/data-agent-frontend
FRONTEND_DOCKER_IMAGE_TAG=<tag>
BACKEND_DOCKER_IMAGE=<registry>/data-agent-backend
BACKEND_DOCKER_IMAGE_TAG=<tag>

FRONTEND_HOST=<domain>

POSTGRES_DB=data_agent
POSTGRES_USER=<postgres-user>
POSTGRES_PASSWORD=<postgres-password>

AUTH_JWT_SECRET=<strong-secret>
```

Для LLM и BI-интеграций заполняются только реально используемые переменные:

```env
LITEPROXY_URL=
LITEPROXY_API_KEY=
LITEPROXY_MODEL=GigaChat-2-Max
LITEPROXY_TEXT_MODEL=openai/openai/gpt-oss-120b

GPT2GIGA_URL=
GPT2GIGA_API_KEY=
GIGACHAT_VISION_MODEL=GigaChat-2-Max

DATALENS_OAUTH_TOKEN=
DATALENS_CLOUD_ID=
DATALENS_COLLECTION_ID=

FORESIGHT_BASE_URL=
FORESIGHT_REPO_LOGIN=
FORESIGHT_REPO_PASSWORD=
FORESIGHT_SSH_HOST=

VISIOLOGY_USERNAME=
VISIOLOGY_PASSWORD=
```

Секреты нельзя хранить в git. Их нужно передавать через CI/CD secrets или защищенный `.env` CD-репозитория.

## Сервисы в Docker Compose

`docker-compose.cd.yml` поднимает три сервиса:

| Сервис | Container name | Назначение |
| --- | --- | --- |
| `postgres` | `incubare-data-agent-postgres` | PostgreSQL база приложения |
| `backend` | `incubare-data-agent-backend` | FastAPI API |
| `frontend` | `incubare-data-agent-frontend` | nginx + React UI + proxy `/api` |

Схема:

```text
Browser
  -> frontend nginx:80
    -> React static files
    -> /api/* -> backend:8000
  -> backend
    -> postgres:5432
```

## Сети

В CD-compose используются две сети:

| Сеть | Назначение |
| --- | --- |
| `proxy` | внешняя стандартная сеть бизнес-инкубатора для Traefik |
| `incubare-data-agent_network` | внутренняя сеть backend/postgres |

Сеть `proxy` должна существовать на сервере:

```yaml
networks:
  proxy:
    name: proxy
    external: true
```

## Traefik

Публично наружу выводится только frontend.

Frontend labels:

```yaml
labels:
  - "traefik.enable=true"
  - "traefik.http.services.incubare-data-agent-frontend.loadbalancer.server.port=80"
  - "traefik.http.routers.incubare-data-agent-frontend.rule=Host(`${FRONTEND_HOST:?error}`)"
  - "traefik.http.routers.incubare-data-agent-frontend.tls=true"
```

Backend не нужно публиковать наружу отдельным доменом: frontend nginx проксирует API-запросы на backend внутри Docker-сети.

## Проверка после деплоя

После запуска:

```bash
docker compose -f docker-compose.cd.yml ps
```

Все сервисы должны быть в состоянии `healthy`.

Проверка снаружи:

```bash
curl https://<domain>/api/health
```

Ожидаемый ответ:

```json
{"status":"ok","service":"data-agent"}
```

Проверка UI:

- открыть `https://<domain>`;
- убедиться, что загружается frontend;
- пройти регистрацию или логин;
- загрузить тестовый файл;
- проверить, что запросы `/api/*` не уходят в ошибку.

## STATE.MD

Для CD нужен файл:

```text
STATE.MD
```

Сейчас значение:

```text
ENABLED
```

Если нужно временно отключить доставку:

```text
DISABLED
```

## Что важно не делать

- Не хранить `.env`, `.npmrc`, `pip.conf`, `uv.toml` и секреты в git.
- Не хранить registry tokens в Dockerfile.
- Не публиковать backend наружу без необходимости.
- Не объединять frontend и backend обратно в один Docker image.

## Что передать DevOps для подключения

1. Ссылки на репозитории:
   - `data-agent-frontend`
   - `data-agent-backend`
2. Dockerfile в каждом репозитории.
3. `docker-compose.cd.yml`.
4. `.env.example`.
5. `STATE.MD`.
6. Домен для frontend: `<domain>`.
7. Список секретов, которые нужно завести в CI/CD.
8. Почты для уведомлений.

## Короткое описание для письма

```text
Проект: Data Agent
Стек: React/Vite frontend, Python FastAPI backend, PostgreSQL
Репозитории:
- data-agent-frontend
- data-agent-backend

Frontend image: nginx, порт 80
Backend image: uvicorn/FastAPI, порт 8000
CD compose: docker-compose.cd.yml
Публичный сервис: frontend через Traefik
Healthcheck: /api/health
```
