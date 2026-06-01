# Data Agent Backend

FastAPI backend для Data Agent. Этот репозиторий также содержит Docker Compose для локального запуска полного проекта: PostgreSQL + backend + frontend.

## Локальный запуск без Docker

```bash
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
cp .env.example .env
./start_backend.sh
```

Backend будет доступен на `http://localhost:8000`.

## Локальный запуск всего проекта через Docker

Положите папки рядом:

```text
tot/
├── data-agent-frontend/
└── data-agent-backend/
```

Затем:

```bash
cd data-agent-backend
cp .env.example .env
docker compose up --build
```

Frontend будет доступен на `http://localhost:5000`.

Проверка API:

```bash
curl http://localhost:5000/api/health
```

## CD-контур

Для бизнес-инкубатора добавлен пример `docker-compose.cd.yml`. Он использует готовые Docker images из переменных окружения, сеть `proxy`, traefik labels и `STATE.MD`.

Минимально для CD нужно заполнить в `.env`:

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

Запуск CD-compose:

```bash
docker compose -f docker-compose.cd.yml up -d
```

## Docker

Backend image:

```bash
docker build -t data-agent-backend .
```

В Docker backend не зависит от frontend `dist`; frontend раздается отдельным nginx-контейнером.


----
