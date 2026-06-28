from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from future_bot.config import Settings
from future_bot.logic import (
    IncomingMessage,
    Post,
    dedupe_posts,
    filter_posts_by_terms,
    format_numbered_links,
    parse_search_command,
    remove_posts_linked_from_ff,
)
from future_bot.storage import Storage

LOGGER = logging.getLogger(__name__)


class WallClient(Protocol):
    def iter_wall_posts(self, group: str, since_timestamp: int | None = None) -> Iterable[Post]:
        ...


class MessageClient(Protocol):
    def send_message(self, peer_id: int, message: str) -> object:
        ...


class ChatClient(Protocol):
    def find_conversation_peer_id(self, title: str) -> int | None:
        ...

    def iter_recent_messages(self, peer_id: int, count: int = 50) -> Iterable[IncomingMessage]:
        ...


@dataclass(frozen=True)
class SyncResult:
    ff_full_import: bool
    ff_posts_seen: int
    source_posts_seen: int
    filtered_posts: int
    final_posts: int
    message: str
    keywords: tuple[str, ...]
    interval_days: int


class FutureBotService:
    def __init__(
        self,
        settings: Settings,
        wall_client: WallClient,
        message_client: MessageClient,
        storage: Storage,
        chat_client: ChatClient | None = None,
    ) -> None:
        self.settings = settings
        self.wall_client = wall_client
        self.message_client = message_client
        self.storage = storage
        self.chat_client = chat_client

    def run_once(
        self,
        now: datetime | None = None,
        keywords: Sequence[str] | None = None,
        hashtags: Sequence[str] | None = None,
        interval_days: int = 1,
        peer_id: int | None = None,
        include_summary: bool = False,
    ) -> SyncResult:
        if interval_days <= 0:
            raise ValueError("Интервал поиска должен быть больше нуля")

        current_time = now or datetime.now(ZoneInfo(self.settings.timezone))
        source_since_timestamp = int((current_time - timedelta(days=interval_days)).timestamp())

        ff_full_import = not self.storage.has_ff_posts()
        ff_since = None if ff_full_import else self.storage.get_latest_ff_post_date()
        ff_posts = list(self.wall_client.iter_wall_posts(self.settings.ff_group, ff_since))
        self.storage.upsert_ff_posts(ff_posts)
        LOGGER.info("Сохранено постов Формулы Футурологии: %s", len(ff_posts))

        source_posts: list[Post] = []
        for group in self.settings.source_groups:
            group_posts = list(self.wall_client.iter_wall_posts(group, source_since_timestamp))
            LOGGER.info("Получено постов из %s: %s", group, len(group_posts))
            source_posts.extend(group_posts)

        effective_keywords = tuple(keywords or self.settings.keywords)
        effective_hashtags = tuple(hashtags or self.settings.hashtags)
        unique_source_posts = dedupe_posts(source_posts)
        filtered_posts = filter_posts_by_terms(
            unique_source_posts,
            keywords=effective_keywords,
            hashtags=effective_hashtags,
        )
        final_posts = remove_posts_linked_from_ff(filtered_posts, self.storage.get_ff_links())
        final_posts = sorted(final_posts, key=lambda post: post.date, reverse=True)
        self.storage.replace_new_posts(final_posts)

        links_message = format_numbered_links(
            final_posts,
            empty_message=f"За последние {interval_days} д. новых постов по заданным критериям не найдено.",
        )
        if include_summary:
            message = _format_sync_report(
                keywords=effective_keywords,
                interval_days=interval_days,
                ff_posts_seen=len(ff_posts),
                source_posts_seen=len(unique_source_posts),
                filtered_posts=len(filtered_posts),
                final_posts=len(final_posts),
            )
            message = f"{message}\n\n{links_message}"
        else:
            message = links_message

        self.message_client.send_message(peer_id or self.settings.target_peer_id, message)
        self.storage.set_metadata("last_successful_sync_at", current_time.isoformat())

        return SyncResult(
            ff_full_import=ff_full_import,
            ff_posts_seen=len(ff_posts),
            source_posts_seen=len(unique_source_posts),
            filtered_posts=len(filtered_posts),
            final_posts=len(final_posts),
            message=message,
            keywords=effective_keywords,
            interval_days=interval_days,
        )

    def handle_chat_message(
        self,
        message: IncomingMessage,
        now: datetime | None = None,
    ) -> SyncResult | None:
        command = parse_search_command(message.text)
        if command is None:
            return None

        if message.from_id not in self.settings.allowed_user_ids:
            LOGGER.info(
                "Команда поиска отклонена: пользователь %s не входит в список разрешенных",
                message.from_id,
            )
            self.message_client.send_message(
                message.peer_id,
                "Команда доступна только разрешенным пользователям.",
            )
            return None

        LOGGER.info(
            "Получена команда поиска от пользователя %s в чате %s: слова=%s, интервал=%s д.",
            message.from_id,
            message.peer_id,
            ", ".join(command.keywords),
            command.interval_days,
        )
        return self.run_once(
            now=now,
            keywords=command.keywords,
            hashtags=command.hashtags,
            interval_days=command.interval_days,
            peer_id=message.peer_id,
            include_summary=True,
        )

    def poll_chat_once(self, now: datetime | None = None) -> int:
        if self.chat_client is None:
            raise RuntimeError("Для проверки команд нужен клиент чата")

        peer_id = self.resolve_target_peer_id()
        metadata_key = f"last_processed_message_sequence:{peer_id}"
        last_processed = int(self.storage.get_metadata(metadata_key) or "0")
        messages = sorted(
            self.chat_client.iter_recent_messages(peer_id, count=50),
            key=lambda item: item.sequence_id,
        )

        handled_count = 0
        max_sequence_id = last_processed
        for message in messages:
            if message.sequence_id <= last_processed:
                continue
            max_sequence_id = max(max_sequence_id, message.sequence_id)
            if self.handle_chat_message(message, now=now) is not None:
                handled_count += 1

        if max_sequence_id > last_processed:
            self.storage.set_metadata(metadata_key, str(max_sequence_id))
        return handled_count

    def resolve_target_peer_id(self) -> int:
        if self.chat_client is None:
            return self.settings.target_peer_id

        peer_id = self.chat_client.find_conversation_peer_id(self.settings.target_chat_title)
        if peer_id is None:
            LOGGER.info(
                "Чат %r не найден через messages.getConversations, используется peer_id %s",
                self.settings.target_chat_title,
                self.settings.target_peer_id,
            )
            return self.settings.target_peer_id

        return peer_id


def _format_sync_report(
    keywords: Sequence[str],
    interval_days: int,
    ff_posts_seen: int,
    source_posts_seen: int,
    filtered_posts: int,
    final_posts: int,
) -> str:
    return "\n".join(
        [
            "Поиск выполнен.",
            f"Ключевые слова: {', '.join(keywords)}.",
            f"Интервал: {interval_days} д.",
            f"Постов ФФ загружено: {ff_posts_seen}.",
            f"Постов источников проверено: {source_posts_seen}.",
            f"После фильтра по словам: {filtered_posts}.",
            f"Итоговых ссылок: {final_posts}.",
        ]
    )
