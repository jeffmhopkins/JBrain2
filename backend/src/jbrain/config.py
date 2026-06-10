from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="JBRAIN_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://jbrain_app:jbrain_app@localhost:5432/jbrain"
    supervisor_url: str = "http://supervisor:9000"
    supervisor_token: str = ""
    session_cookie: str = "jbrain_session"
    blob_dir: str = "/data/blobs"
    backups_dir: str = "/data/backups"
    embed_url: str = "http://embed:80"
    embed_model: str = "BAAI/bge-small-en-v1.5"
    # Cookies require HTTPS in production; tests and local dev run plain HTTP.
    secure_cookies: bool = True

    anthropic_api_key: str = ""
    xai_api_key: str = ""
    # Future-GPU escape hatch: any OpenAI-compatible server (Ollama default).
    local_llm_url: str = "http://localhost:11434/v1"
    # JSON object of per-task "provider:model" overrides, merged over the
    # adapter defaults — see jbrain.llm.router.TASK_DEFAULTS.
    llm_tasks: dict[str, str] = {}


def get_settings() -> Settings:
    return Settings()
