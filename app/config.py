from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://taskflow:taskflow@localhost:5432/taskflow"
    redis_url: str = "redis://localhost:6379/0"
    retry_backoff_seconds: float = 2
    worker_stale_seconds: int = 15
    worker_poll_seconds: float = 1
    worker_name: str = ""
    max_payload_bytes: int = 65536
    app_name: str = "Taskflow API"
    app_version: str = "0.1.0"


@lru_cache
def get_settings() -> Settings:
    return Settings()
