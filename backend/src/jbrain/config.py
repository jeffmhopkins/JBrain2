from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # env_ignore_empty: an unset compose var arrives as "" (e.g. ${FOO:-});
    # treat that as absent so it falls back to the default rather than failing to
    # parse — load-bearing for the wipe one-shot's off-by-default bool guard.
    model_config = SettingsConfigDict(
        env_prefix="JBRAIN_", env_file=".env", extra="ignore", env_ignore_empty=True
    )

    database_url: str = "postgresql+asyncpg://jbrain_app:jbrain_app@localhost:5432/jbrain"
    supervisor_url: str = "http://supervisor:9000"
    supervisor_token: str = ""
    session_cookie: str = "jbrain_session"
    blob_dir: str = "/data/blobs"
    backups_dir: str = "/data/backups"
    embed_url: str = "http://embed:80"
    embed_model: str = "BAAI/bge-small-en-v1.5"
    # Pinned, owner-configured base URLs for the egress connectors (#9). Free,
    # no-auth NLM services; the egress guard fills only typed slots, never a URL.
    rxnav_url: str = "https://rxnav.nlm.nih.gov"
    medlineplus_url: str = "https://connect.medlineplus.gov"
    # Cookies require HTTPS in production; tests and local dev run plain HTTP.
    secure_cookies: bool = True

    # One-time install reset (W3.3 cutover): when set, the `jbrain.install_wipe`
    # one-shot drops + rebuilds the schema, clears blob/backup storage, and
    # enables the v3 pipeline — then writes a sentinel so it NEVER runs twice.
    # Off by default; a deliberate, destructive opt-in for a fresh install.
    wipe_on_first_deploy: bool = False

    anthropic_api_key: str = ""
    xai_api_key: str = ""
    # Future-GPU escape hatch: any OpenAI-compatible server (Ollama default).
    local_llm_url: str = "http://localhost:11434/v1"
    # The model name the `local` provider spec resolves to (local:<model>) — the
    # local server's served model. A plain default so the spec is always concrete.
    local_llm_model: str = "local"
    # JSON object of per-task "provider:model" overrides, merged over the
    # adapter defaults — see jbrain.llm.router.TASK_DEFAULTS.
    llm_tasks: dict[str, str] = {}
    # JSON object of capability-tier "provider:model" overrides (high/low/vision),
    # merged over jbrain.llm.router.TIER_DEFAULTS. A prompt file declares the tier
    # it needs (strength:); the router resolves that tier to a model here — unless
    # the task is explicitly pinned in llm_tasks, which wins.
    llm_tiers: dict[str, str] = {}
    # "provider:model" -> $/M tokens, applied at query time over llm_usage —
    # docs/ANALYSIS.md "Cost estimates" (grok-4.3 rates, xAI docs June 2026).
    llm_prices: dict[str, dict[str, float]] = {
        "xai:grok-4.3": {"input_per_m": 1.25, "output_per_m": 2.50}
    }


def get_settings() -> Settings:
    return Settings()
