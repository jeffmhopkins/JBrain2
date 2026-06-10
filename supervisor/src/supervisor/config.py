"""Environment-driven configuration for the supervisor."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings sourced from environment variables.

    SUPERVISOR_TOKEN has no default on purpose: a supervisor without a token
    must refuse to start rather than run unauthenticated.
    """

    model_config = SettingsConfigDict(case_sensitive=False)

    supervisor_token: str
    compose_project: str = "jbrain"
    self_service: str = "supervisor"
