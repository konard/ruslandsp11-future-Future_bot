from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from urllib.parse import urlparse


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    vk_group_token: str
    vk_user_token: str
    vk_message_token: str
    database_path: Path = Path("data/future_bot.sqlite3")
    ff_group: str = "world_of_futuristica"
    source_groups: tuple[str, ...] = ("eofru", "asimovonline")
    keywords: tuple[str, ...] = ("Технология",)
    hashtags: tuple[str, ...] = ("#Технология",)
    target_peer_id: int = 2_000_000_170
    schedule_time: time = time(3, 0)
    timezone: str = "Europe/Moscow"
    vk_api_version: str = "5.199"

    @classmethod
    def from_env(cls, env_file: str | Path = ".env") -> "Settings":
        env = load_env_file(Path(env_file))
        env.update(os.environ)

        group_token = env.get("VK_GROUP_TOKEN", "").strip()
        user_token = env.get("VK_USER_TOKEN", "").strip()
        message_token = env.get("VK_MESSAGE_TOKEN", "").strip() or user_token or group_token
        missing = [
            name
            for name, value in (
                ("VK_GROUP_TOKEN", group_token),
                ("VK_USER_TOKEN", user_token),
            )
            if not value
        ]
        if missing:
            raise ConfigError(f"Missing required environment variables: {', '.join(missing)}")

        return cls(
            vk_group_token=group_token,
            vk_user_token=user_token,
            vk_message_token=message_token,
            database_path=Path(env.get("FFBOT_DATABASE_PATH", "data/future_bot.sqlite3")),
            ff_group=normalize_group_identifier(
                env.get("FFBOT_FF_GROUP", "https://vk.ru/world_of_futuristica")
            ),
            source_groups=tuple(
                normalize_group_identifier(value)
                for value in split_csv(
                    env.get("FFBOT_SOURCE_GROUPS", "https://vk.ru/eofru,https://vk.ru/asimovonline")
                )
            ),
            keywords=tuple(split_csv(env.get("FFBOT_KEYWORDS", "Технология"))),
            hashtags=tuple(split_csv(env.get("FFBOT_HASHTAGS", "#Технология"))),
            target_peer_id=int(env.get("FFBOT_TARGET_PEER_ID", "2000000170")),
            schedule_time=parse_hhmm(env.get("FFBOT_SCHEDULE_TIME", "03:00")),
            timezone=env.get("FFBOT_TIMEZONE", "Europe/Moscow"),
            vk_api_version=env.get("VK_API_VERSION", "5.199"),
        )


def load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            values[key] = value
    return values


def split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def normalize_group_identifier(value: str) -> str:
    raw = value.strip().strip("\"'").removeprefix("@")
    parsed = urlparse(raw)
    if parsed.netloc:
        raw = parsed.path.strip("/").split("/", 1)[0]
    elif "/" in raw:
        raw = raw.rstrip("/").rsplit("/", 1)[-1]

    if not raw:
        raise ConfigError("VK group identifier cannot be empty")
    return raw


def parse_hhmm(value: str) -> time:
    try:
        hour_text, minute_text = value.split(":", 1)
        return time(int(hour_text), int(minute_text))
    except ValueError as exc:
        raise ConfigError(f"Invalid FFBOT_SCHEDULE_TIME value: {value!r}. Use HH:MM.") from exc
