from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _parse_int_list(value: Any) -> tuple[int, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, int):
        return (value,)
    if isinstance(value, (list, tuple, set)):
        return tuple(int(item) for item in value if str(item).strip())
    text = str(value).replace(";", ",").replace(" ", ",")
    return tuple(int(item.strip()) for item in text.split(",") if item.strip())


class Settings(BaseSettings):
    finkit_login: str = ""
    finkit_password: str = ""
    finkit_base_url: str = "https://finkit.by"
    finkit_offers_url: str = "https://finkit.by/app/invest-manually"
    finkit_api_base_url: str = "https://api-p2p.finkit.by"

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_allowed_user_ids: tuple[int, ...] = Field(default_factory=tuple)

    default_score_threshold: float = 65
    score_compare_mode: str = "gte"
    check_interval_seconds: int = 30

    database_path: str = "data/bot.sqlite3"
    playwright_state_path: str = "data/playwright_state.json"
    playwright_headless: bool = True

    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @field_validator("telegram_allowed_user_ids", mode="before")
    @classmethod
    def parse_allowed_user_ids(cls, value: Any) -> tuple[int, ...]:
        return _parse_int_list(value)

    @field_validator("score_compare_mode")
    @classmethod
    def normalize_compare_mode(cls, value: str) -> str:
        normalized = value.lower().strip()
        return "gte" if normalized == "gte" else "gt"

    @property
    def telegram_chat_id_int(self) -> int | None:
        if not str(self.telegram_chat_id).strip():
            return None
        try:
            return int(str(self.telegram_chat_id).strip())
        except ValueError:
            return None

    @property
    def database_file(self) -> Path:
        return Path(self.database_path)

    @property
    def playwright_state_file(self) -> Path:
        return Path(self.playwright_state_path)


@lru_cache
def get_settings() -> Settings:
    return Settings()
