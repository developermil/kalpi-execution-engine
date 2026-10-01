from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime config from env (SPEC §8). Secrets are SecretStr so they never print."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./data/kalpi.db"
    fernet_key: SecretStr = SecretStr("")
    api_keys: SecretStr = SecretStr("")
    webhook_secret: SecretStr = SecretStr("")
    default_webhook_url: str = ""
    max_qty_per_order: int = Field(default=100_000, gt=0)
    max_concurrency: int = Field(default=5, gt=0)
    lease_ttl_s: int = Field(default=30, gt=0)
    poll_interval_s: float = Field(default=1.0, gt=0)
    recheck_interval_s: float = Field(default=30.0, gt=0)
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
