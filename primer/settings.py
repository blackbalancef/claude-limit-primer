"""config.json (`Config`) + .env secrets (`Secrets`)."""

import json
from typing import Any

from pydantic import BaseModel, ConfigDict
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from primer.paths import CONFIG_PATH, ENV_PATH
from primer.state import write_json_atomic


class Secrets(BaseSettings):
    """TELEGRAM_TOKEN / TELEGRAM_CHAT_ID from .env (secrets never live in config.json)."""

    model_config = SettingsConfigDict(env_file=ENV_PATH, env_file_encoding="utf-8", extra="ignore")

    telegram_token: str = ""
    telegram_chat_id: str = ""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],  # noqa: ARG003 - signature fixed by BaseSettings
        init_settings: PydanticBaseSettingsSource,  # noqa: ARG003
        env_settings: PydanticBaseSettingsSource,  # noqa: ARG003
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,  # noqa: ARG003
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Read secrets ONLY from the .env file (old load_env parity).

        Process environment variables are deliberately ignored: a stray
        exported TELEGRAM_CHAT_ID/TELEGRAM_TOKEN must not rebind the
        single-chat lock or override the token.
        """
        return (dotenv_settings,)


class Config(BaseModel):
    """Non-secret settings persisted in config.json (schema identical to the old file)."""

    model_config = ConfigDict(extra="allow", coerce_numbers_to_str=True)

    telegram_token: str = ""  # from @BotFather (prefer .env)
    telegram_chat_id: str = ""  # auto-captured on first message if empty
    tz: str = "UTC"  # your timezone, e.g. Europe/Moscow
    model: str = "claude-haiku-4-5-20251001"
    cycle_minutes: int = 300  # 5-hour window
    margin_minutes: int = 3  # prime this many minutes AFTER the reset
    prompt: str = "Reply with exactly one word: pong"
    claude_timeout_secs: int = 120
    failure_retry_minutes: int = 10
    notify_on_prime: bool = True
    notify_on_failure: bool = True


def load_config() -> Config:
    data: dict[str, Any] = {}
    if CONFIG_PATH.exists():
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    cfg = Config.model_validate(data)
    # .env overrides config (secrets live here, never in config.json)
    secrets = Secrets()
    if secrets.telegram_token:
        cfg.telegram_token = secrets.telegram_token
    if secrets.telegram_chat_id:
        cfg.telegram_chat_id = secrets.telegram_chat_id
    return cfg


def save_config(cfg: Config) -> None:
    """Persist config.json, never writing back secrets that came from .env."""
    secrets = Secrets()
    out = cfg.model_copy(deep=True)
    if secrets.telegram_token:
        out.telegram_token = ""
    if secrets.telegram_chat_id:
        out.telegram_chat_id = ""
    write_json_atomic(CONFIG_PATH, out.model_dump())
