# Data Agent Backend

## Summary

Backend part of Data Agent: Python FastAPI API with PostgreSQL persistence and BI integrations.

## Goals

- Keep backend independent from frontend build artifacts.
- Expose API under `/api/*`.
- Run production through Docker and uvicorn.
- Keep deploy configuration clear for local Docker Compose and business incubator CD.

## Tech Stack

- Python 3.12
- FastAPI
- uvicorn
- PostgreSQL
- pydantic-settings
- Playwright, pandas, DuckDB and BI integration clients

## Directories

- `backend/` — application package, routers, services, builders and config.
- `scripts/` — operational/eval scripts.
- `Dockerfile` — production backend image.
- `docker-compose.yml` — local full-stack launch with sibling frontend repo.
- `docker-compose.cd.yml` — CD-oriented compose example with images and traefik labels.

## Coding Rules

- Functional style where practical.
- No frontend code or npm dependencies in this repo.
- Do not commit `.env`, `pip.conf`, `uv.toml`, secrets or runtime artifacts.
- Keep Docker secrets outside Dockerfile and pass them through CI/CD secrets.
