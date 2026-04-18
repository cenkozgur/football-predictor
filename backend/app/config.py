from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://football:football@localhost:5432/football_predictor"
    redis_url: str = "redis://localhost:6379/0"

    # Optional URL prefix prepended to football-data.co.uk fetches.
    # Set to "https://r.jina.ai/" when behind an ISP block (e.g. Turkey) to route
    # through Jina Reader; leave empty for direct fetches.
    football_data_proxy: str = ""

    jwt_secret: str = "change-me"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440

    environment: str = "development"


@lru_cache
def get_settings() -> Settings:
    return Settings()
