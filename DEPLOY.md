# Развертывание Data Agent после разделения

## Структура

Для локального запуска папки должны лежать рядом:

```text
tot/
├── data-agent-frontend/
└── data-agent-backend/
```

Frontend и backend собираются отдельными Dockerfile. Общий запуск лежит в `data-agent-backend/docker-compose.yml`.

## Быстрый локальный запуск

```bash
cd data-agent-backend
cp .env.example .env
docker compose up --build
```

Приложение будет доступно на `http://localhost:5000`.

Проверка API через frontend nginx:

```bash
curl http://localhost:5000/api/health
```

## Архитектура

```text
Browser -> nginx frontend:80 -> React SPA
                            -> /api/* -> FastAPI backend:8000 -> PostgreSQL:5432
```

## Порты

| Сервис | Порт | Назначение |
| --- | --- | --- |
| frontend | `5000:80` | Web UI и API proxy |
| backend | внутренний `8000` | FastAPI API |
| postgres | внутренний `5432` | База приложения |

Порт frontend меняется через `FRONTEND_PORT` в `.env`.

## CD для бизнес-инкубатора

Для CD-контура подготовлен `docker-compose.cd.yml`.

Он учитывает требования инструкции:

- `container_name` и `hostname`;
- images через env-переменные;
- `healthcheck`;
- сеть `proxy`;
- traefik labels для frontend;
- `STATE.MD` со статусом `ENABLED`.

Минимальные переменные:

```env
FRONTEND_DOCKER_IMAGE=
FRONTEND_DOCKER_IMAGE_TAG=
BACKEND_DOCKER_IMAGE=
BACKEND_DOCKER_IMAGE_TAG=
FRONTEND_HOST=
POSTGRES_DB=
POSTGRES_USER=
POSTGRES_PASSWORD=
AUTH_JWT_SECRET=
```

Запуск:

```bash
docker compose -f docker-compose.cd.yml up -d
```

## Полезные команды

```bash
docker compose logs -f backend
docker compose logs -f frontend
docker compose build backend
docker compose up -d --force-recreate backend
docker compose down
docker compose down -v
```

## Важные замечания

- `.env`, `.npmrc`, `pip.conf`, `uv.toml` и секреты не коммитим.
- Backend больше не зависит от frontend `dist`.
- Frontend ходит в backend через `/api`.
- Для production/CD секреты нужно передавать через переменные окружения или секреты CI/CD.
