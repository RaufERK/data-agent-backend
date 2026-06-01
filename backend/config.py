from __future__ import annotations
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LiteProxy — OpenAI-compatible proxy over GigaChat (primary)
    liteproxy_url: str = ""
    liteproxy_api_key: str = ""
    liteproxy_model: str = "GigaChat-2-Max"       # vision model
    liteproxy_text_model: str = "openai/openai/gpt-oss-120b"  # text/chat model
    liteproxy_timeout: int = 120

    # Legacy stubs — not used, kept for backwards compatibility
    cloudru_api_key: str = ""
    cloudru_base_url: str = "https://foundation-models.api.cloud.ru/v1"
    cloudru_model: str = "openai/gpt-oss-120b"
    cloudru_timeout: int = 120
    cloudru_vision_judge_model: str = "openai/gpt-5.1"
    cloudru_vision_judge_timeout: int = 120
    gpt2giga_url: str = "http://localhost:8090/v1"
    gpt2giga_api_key: str = "dummy-local-key"
    gigachat_vision_model: str = "GigaChat-2-Max"
    gpt2giga_timeout: int = 240

    # App
    upload_dir: str = "/tmp/data_agent_uploads"
    max_upload_mb: int = 3072
    quota_upload_files_daily: int = 30
    quota_assistant_questions_daily: int = 100
    quota_dashboard_generations_daily: int = 100
    quota_vision_analyses_daily: int = 30
    vision_worker_count: int = 2
    structured_logs: bool = True
    app_db_host: str = "localhost"
    app_db_port: int = 5432
    app_db_name: str = "data_agent"
    app_db_user: str = "postgres"
    app_db_password: str = ""
    app_db_sslmode: str = "prefer"
    auth_jwt_secret: str = ""
    auth_jwt_ttl_minutes: int = 60 * 24 * 7
    auth_cookie_name: str = "data_agent_auth"
    auth_cookie_secure: bool = False
    auth_cookie_samesite: str = "lax"
    navigator_base_url: str = "https://localhost:8443"
    navigator_db_host: str = "localhost"
    navigator_db_port: int = 5432
    navigator_db_name: str = "navigator"
    navigator_db_user: str = "postgres"
    navigator_db_password: str = ""
    navigator_access_login: str = ""

    # Foresight Analytics Platform demo VM / repository
    foresight_base_url: str = "http://127.0.0.1:8110/fp10.x/"
    foresight_ssh_host: str = "127.0.0.1"
    foresight_ssh_port: int = 2222
    foresight_ssh_user: str = "user"
    foresight_ssh_password: str = ""
    foresight_repo_id: str = "FS_DEMO"
    foresight_repo_name: str = "Демо репозиторий"
    foresight_security_package: str = "STANDARDSECURITYPACKAGE"
    foresight_driver_id: str = "POSTGRES"
    foresight_authentication: int = 1
    foresight_db_server: str = "127.0.0.1:5432"
    foresight_db_name: str = "FS_TRAINING_FORE"
    foresight_db_schema: str = "public"
    foresight_repo_login: str = ""
    foresight_repo_password: str = ""
    foresight_repo_user_os: str = "linux"
    foresight_repo_user_station: str = "rocky-fp10"

    # Visiology demo instance
    visiology_public_base_url: str = "https://demo.visiology.su/v3"
    visiology_api_base_url: str = "https://87.245.142.229/v3"
    visiology_host_header: str = "demo.visiology.su"
    visiology_username: str = ""
    visiology_password: str = ""
    visiology_client_id: str = "visiology_designer"
    visiology_workspace_id: str = "e7568ce1-403d-4d67-8133-6ba03cd3eac1"
    visiology_theme_guid: str = "6a21f8f3fb9744d1832c3be41ab61645"
    visiology_template_workspace_id: str = "4de8029c-7e04-4f6b-9c46-d02b4f493192"
    visiology_template_dashboard_id: str = "30a66974fccd42b9b4a0f3de9d99a8d7"
    visiology_verify_ssl: bool = True
    visiology_timeout: int = 180

    # Yandex DataLens Public API
    datalens_api_base_url: str = "https://api.datalens.tech"
    datalens_public_base_url: str = "https://datalens.yandex.cloud"
    datalens_iam_token: str = ""
    datalens_oauth_token: str = ""
    datalens_org_id: str = ""
    datalens_cloud_id: str = ""
    datalens_collection_id: str = ""
    datalens_timeout: int = 120
    datalens_native_connection_id: str = ""
    datalens_native_connection_workbook_id: str = ""
    datalens_native_connection_rev_id: str = ""
    datalens_native_source_id: str = ""

    # OIDC / SSO (Sberanalytics)
    oidc_issuer: str = ""
    oidc_client: str = ""
    oidc_secret: str = ""
    oidc_verify_ssl: bool = True
    oidc_sync_every_sec: int = 60
    oidc_redirect_uri: str = ""

    cors_origins: list[str] = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:3001",
        "http://127.0.0.1:3001",
        "http://localhost:3002",
        "http://127.0.0.1:3002",
        "http://localhost:3003",
        "http://127.0.0.1:3003",
        "http://localhost:5000",
        "http://127.0.0.1:5000",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
