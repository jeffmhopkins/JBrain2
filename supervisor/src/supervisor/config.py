"""Environment-driven configuration for the supervisor."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings sourced from environment variables.

    SUPERVISOR_TOKEN has no default, so an absent variable refuses to start. An
    EMPTY one (compose's `${SUPERVISOR_TOKEN}` when the key is unset) is accepted and
    the bearer becomes `Bearer `, which the api sends too: refusing it would stop the
    supervisor, and with it Ops → Update and the api watchdog, on any box whose .env
    lacks the key, with no terminal to repair it. install.sh always mints one; the gap
    is recorded in the security plan (S4).
    """

    model_config = SettingsConfigDict(case_sensitive=False)

    supervisor_token: str
    compose_project: str = "jbrain"
    self_service: str = "supervisor"
    project_dir: str = "/opt/jbrain2"
