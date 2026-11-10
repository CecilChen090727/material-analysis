# @author   : linhao.chen01
# @time     : 2026/11/4 10:35

from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.ext.asyncio import create_async_engine


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parent / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: SecretStr


def create_database_engine():
    settings = DatabaseSettings()
    return create_async_engine(
        settings.database_url.get_secret_value(), pool_pre_ping=True
    )
