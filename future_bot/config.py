from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from urllib.parse import urlparse

from future_bot.logic import parse_query_groups

DEFAULT_SOURCE_GROUPS_FILE = Path("Список групп.txt")
DEFAULT_TERMS_FILE = Path("Список слов и хэштегов.txt")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class SearchTerms:
    keywords: tuple[str, ...]
    hashtags: tuple[str, ...]


@dataclass(frozen=True)
class CommandLine:
    """Одна строка файла команд: исходный текст и разобранные группы слов."""

    raw: str
    groups: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class Settings:
    vk_group_token: str
    vk_user_token: str
    vk_message_token: str
    database_path: Path = Path("data/future_bot.sqlite3")
    ff_group: str = "world_of_futuristica"
    source_groups_file: Path = DEFAULT_SOURCE_GROUPS_FILE
    terms_file: Path = DEFAULT_TERMS_FILE
    target_peer_id: int = 2_000_000_015
    target_chat_title: str = "Аналитика и прогнозы"
    allowed_user_ids: tuple[int, ...] = (199592366, 1849091)
    command_poll_interval_seconds: float = 10.0
    post_retention_days: int = 10
    schedule_time: time = time(3, 0)
    timezone: str = "Europe/Moscow"
    vk_api_version: str = "5.199"

    @classmethod
    def from_env(cls, env_file: str | Path = ".env") -> "Settings":
        env = load_env_file(Path(env_file))
        env.update(os.environ)

        group_token = env.get("VK_GROUP_TOKEN", "").strip()
        user_token = env.get("VK_USER_TOKEN", "").strip()
        message_token = env.get("VK_MESSAGE_TOKEN", "").strip() or group_token
        missing = [
            name
            for name, value in (
                ("VK_GROUP_TOKEN", group_token),
                ("VK_USER_TOKEN", user_token),
            )
            if not value
        ]
        if missing:
            raise ConfigError(f"Не заданы обязательные переменные окружения: {', '.join(missing)}")

        return cls(
            vk_group_token=group_token,
            vk_user_token=user_token,
            vk_message_token=message_token,
            database_path=Path(env.get("FFBOT_DATABASE_PATH", "data/future_bot.sqlite3")),
            ff_group=normalize_group_identifier(
                env.get("FFBOT_FF_GROUP", "https://vk.ru/world_of_futuristica")
            ),
            source_groups_file=Path(env.get("FFBOT_SOURCE_GROUPS_FILE", DEFAULT_SOURCE_GROUPS_FILE)),
            terms_file=Path(env.get("FFBOT_TERMS_FILE", DEFAULT_TERMS_FILE)),
            target_peer_id=int(env.get("FFBOT_TARGET_PEER_ID", "2000000015")),
            target_chat_title=env.get("FFBOT_TARGET_CHAT_TITLE", "Аналитика и прогнозы"),
            allowed_user_ids=parse_int_csv(env.get("FFBOT_ALLOWED_USER_IDS", "199592366,1849091")),
            command_poll_interval_seconds=float(env.get("FFBOT_COMMAND_POLL_INTERVAL_SECONDS", "10")),
            post_retention_days=parse_positive_int(
                env.get("FFBOT_POST_RETENTION_DAYS", "10"), "FFBOT_POST_RETENTION_DAYS"
            ),
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


def load_source_groups_file(path: Path) -> tuple[str, ...]:
    groups = tuple(normalize_group_identifier(value) for value in read_list_file(path))
    if not groups:
        raise ConfigError(f"Файл со списком групп пуст: {path}")
    return groups


def load_terms_file(path: Path) -> SearchTerms:
    keywords: list[str] = []
    hashtags: list[str] = []
    for value in read_list_file(path, allow_hash_items=True):
        if value.startswith("#"):
            hashtag = f"#{value.lstrip('#')}"
            hashtags.append(hashtag)
        else:
            keywords.append(value)
            hashtags.append(f"#{value.lstrip('#')}")

    if not keywords and not hashtags:
        raise ConfigError(f"Файл со списком слов и хэштегов пуст: {path}")
    return SearchTerms(keywords=tuple(dedupe_keep_order(keywords)), hashtags=tuple(dedupe_keep_order(hashtags)))


def load_command_lines(path: Path) -> list[CommandLine]:
    """Читает файл команд: каждая непустая строка - отдельная команда.

    В строке запятая разделяет группы (логическое ИЛИ), а ``+`` объединяет
    слова в одну группу (логическое И). Строки без слов пропускаются.
    """

    if not path.exists():
        raise ConfigError(f"Файл не найден: {path}")

    commands: list[CommandLine] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        groups = parse_query_groups(line)
        if groups:
            commands.append(CommandLine(raw=line, groups=groups))

    if not commands:
        raise ConfigError(f"Файл со списком слов и хэштегов пуст: {path}")
    return commands


def read_list_file(path: Path, allow_hash_items: bool = False) -> list[str]:
    if not path.exists():
        raise ConfigError(f"Файл не найден: {path}")

    items: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if not allow_hash_items and line.startswith("#"):
            continue
        for raw_item in line.split(","):
            item = raw_item.strip().strip("\"'")
            if item:
                items.append(item)
    return items


def dedupe_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def parse_positive_int(value: str, name: str) -> int:
    try:
        result = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Некорректное значение {name}: {value!r}. Ожидается целое число.") from exc
    if result <= 0:
        raise ConfigError(f"Значение {name} должно быть больше нуля, получено {result}.")
    return result


def parse_int_csv(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in split_csv(value))
    except ValueError as exc:
        raise ConfigError(f"Некорректный список числовых идентификаторов: {value!r}") from exc


def normalize_group_identifier(value: str) -> str:
    raw = value.strip().strip("\"'").removeprefix("@")
    parsed = urlparse(raw)
    if parsed.netloc:
        raw = parsed.path.strip("/").split("/", 1)[0]
    elif "/" in raw:
        raw = raw.rstrip("/").rsplit("/", 1)[-1]

    if not raw:
        raise ConfigError("Идентификатор группы VK не может быть пустым")
    return raw


def parse_hhmm(value: str) -> time:
    try:
        hour_text, minute_text = value.split(":", 1)
        return time(int(hour_text), int(minute_text))
    except ValueError as exc:
        raise ConfigError(f"Некорректное значение FFBOT_SCHEDULE_TIME: {value!r}. Используйте HH:MM.") from exc
