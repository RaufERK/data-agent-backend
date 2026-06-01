Тема: переменные окружения для деплоя Data Agent Backend

Добрый день.

Для сборки и запуска backend нужны переменные окружения из `.env.example`.

По инструкции бизнес-инкубатора:

```text
Bitbucket CI:
Должен содержать рабочий Dockerfile и код.
Рекомендуется сразу использовать SberOSC как источник базовых образов и зависимостей.

Bitbucket CD:
Должен содержать .env и docker-compose.yml.

Не допускается хранение секретов в репозиториях.
Все необходимые секреты добавляются в процессе сборки.
```

## Минимально нужны для сборки и запуска

```env
POSTGRES_PASSWORD=<POSTGRES_PASSWORD>
APP_DB_PASSWORD=<POSTGRES_PASSWORD>
AUTH_JWT_SECRET=<AUTH_JWT_SECRET>
PIP_INDEX_URL=https://token:<SBEROSC_TOKEN>@sberworks.ru/osc/repo/pypi/simple
```

`PIP_INDEX_URL` должен указывать на корпоративный SberOSC PyPI. Без него Docker build не должен ходить в публичный `pypi.org`.

По документации SberOSC для PyPI используется:

```text
https://token:TOKEN@sberworks.ru/osc/repo/pypi/simple
```

`TOKEN` нужно заменить на токен из профиля SberOSC или на технический токен для CI.

## Если используется SSO/OIDC

```env
OIDC_ISSUER=<OIDC_ISSUER>
OIDC_CLIENT=<OIDC_CLIENT>
OIDC_SECRET=<OIDC_SECRET>
OIDC_REDIRECT_URI=<OIDC_REDIRECT_URI>
OIDC_VERIFY_SSL=true
```

## Если используется AI-чат/генерация

```env
LITEPROXY_URL=<LITEPROXY_URL>
LITEPROXY_API_KEY=<LITEPROXY_API_KEY>
LITEPROXY_MODEL=GigaChat-2-Max
LITEPROXY_TEXT_MODEL=openai/openai/gpt-oss-120b
```

Альтернативно, если используется GPT2Giga:

```env
GPT2GIGA_URL=<GPT2GIGA_URL>
GPT2GIGA_API_KEY=<GPT2GIGA_API_KEY>
GIGACHAT_VISION_MODEL=GigaChat-2-Max
```

## Если используются BI-интеграции

DataLens:

```env
DATALENS_OAUTH_TOKEN=<DATALENS_OAUTH_TOKEN>
DATALENS_CLOUD_ID=<DATALENS_CLOUD_ID>
DATALENS_COLLECTION_ID=<DATALENS_COLLECTION_ID>
DATALENS_NATIVE_CONNECTION_ID=<DATALENS_NATIVE_CONNECTION_ID>
DATALENS_NATIVE_CONNECTION_WORKBOOK_ID=<DATALENS_NATIVE_CONNECTION_WORKBOOK_ID>
```

Foresight:

```env
FORESIGHT_BASE_URL=<FORESIGHT_BASE_URL>
FORESIGHT_REPO_LOGIN=<FORESIGHT_REPO_LOGIN>
FORESIGHT_REPO_PASSWORD=<FORESIGHT_REPO_PASSWORD>
FORESIGHT_SSH_HOST=<FORESIGHT_SSH_HOST>
FORESIGHT_SSH_PORT=2222
FORESIGHT_SSH_USER=<FORESIGHT_SSH_USER>
FORESIGHT_SSH_PASSWORD=<FORESIGHT_SSH_PASSWORD>
```

Visiology:

```env
VISIOLOGY_USERNAME=<VISIOLOGY_USERNAME>
VISIOLOGY_PASSWORD=<VISIOLOGY_PASSWORD>
VISIOLOGY_VERIFY_SSL=true
```

## Вывод

`.env.example` в CI-репозитории является шаблоном и чеклистом.

Настоящий `.env` должен быть подготовлен для CD-окружения или передан через механизм секретов/переменных сборки. Реальные пароли, токены и client secret не должны храниться в backend-коде.
