from __future__ import annotations

import logging
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from future_bot.config import Settings, load_source_groups_file, load_terms_file
from future_bot.logic import (
    ControlCommand,
    IncomingMessage,
    Post,
    dedupe_posts,
    filter_posts_by_terms,
    format_numbered_links,
    parse_control_command,
    parse_search_command,
    remove_posts_linked_from_ff,
)
from future_bot.storage import Storage

LOGGER = logging.getLogger(__name__)
MAX_VK_MESSAGE_LENGTH = 4000


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
    failed_groups: tuple[str, ...] = ()
    stopped: bool = False


@dataclass(frozen=True)
class ControlResult:
    command: ControlCommand
    message: str


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
        self._run_lock = threading.Lock()
        self._search_stop_event = threading.Event()
        self._shutdown_event = threading.Event()

    @property
    def search_stop_requested(self) -> bool:
        return self._search_stop_event.is_set()

    @property
    def shutdown_requested(self) -> bool:
        return self._shutdown_event.is_set()

    def request_stop_search(self) -> None:
        self._search_stop_event.set()

    def request_shutdown(self) -> None:
        self._shutdown_event.set()

    def wait_for_shutdown(self, timeout: float | None = None) -> bool:
        return self._shutdown_event.wait(timeout)

    def run_once(
        self,
        now: datetime | None = None,
        keywords: Sequence[str] | None = None,
        hashtags: Sequence[str] | None = None,
        interval_days: int = 1,
        peer_id: int | None = None,
        include_summary: bool = False,
        show_progress: bool = False,
    ) -> SyncResult:
        with self._run_lock:
            self._search_stop_event.clear()
            try:
                return self._run_once(
                    now=now,
                    keywords=keywords,
                    hashtags=hashtags,
                    interval_days=interval_days,
                    peer_id=peer_id,
                    include_summary=include_summary,
                    show_progress=show_progress,
                )
            finally:
                self._search_stop_event.clear()

    def _run_once(
        self,
        now: datetime | None = None,
        keywords: Sequence[str] | None = None,
        hashtags: Sequence[str] | None = None,
        interval_days: int = 1,
        peer_id: int | None = None,
        include_summary: bool = False,
        show_progress: bool = False,
    ) -> SyncResult:
        if interval_days <= 0:
            raise ValueError("Интервал поиска должен быть больше нуля")

        current_time = now or datetime.now(ZoneInfo(self.settings.timezone))
        target_peer_id = peer_id or self.settings.target_peer_id
        source_since_timestamp = int((current_time - timedelta(days=interval_days)).timestamp())
        source_groups = load_source_groups_file(self.settings.source_groups_file)
        progress = (
            _ProgressMessage.start(
                self.message_client,
                target_peer_id,
                _format_progress_message(checked_groups=0, total_groups=len(source_groups)),
            )
            if show_progress
            else None
        )

        effective_keywords = tuple(keywords or ())
        effective_hashtags = tuple(hashtags or ())
        if effective_keywords and hashtags is None:
            effective_hashtags = tuple(
                f"#{keyword.lstrip('#')}" for keyword in effective_keywords if keyword.lstrip("#")
            )
        if not effective_keywords and not effective_hashtags:
            terms = load_terms_file(self.settings.terms_file)
            effective_keywords = terms.keywords
            effective_hashtags = terms.hashtags

        ff_full_import = not self.storage.has_ff_posts()
        ff_since = None if ff_full_import else self.storage.get_latest_ff_post_date()
        failed_groups: list[str] = []
        ff_posts = self._collect_wall_posts(self.settings.ff_group, ff_since, failed_groups)
        self.storage.upsert_ff_posts(ff_posts)
        LOGGER.info("Сохранено постов Формулы Футурологии: %s", len(ff_posts))

        source_posts: list[Post] = []
        checked_groups = 0
        for group in source_groups:
            if self.search_stop_requested:
                return self._finish_stopped_search(
                    progress=progress,
                    peer_id=target_peer_id,
                    ff_full_import=ff_full_import,
                    ff_posts_seen=len(ff_posts),
                    source_posts_seen=len(dedupe_posts(source_posts)),
                    keywords=effective_keywords,
                    interval_days=interval_days,
                    checked_groups=checked_groups,
                    total_groups=len(source_groups),
                    failed_groups=failed_groups,
                )

            group_posts = self._collect_wall_posts(group, source_since_timestamp, failed_groups)
            LOGGER.info("Получено постов из %s: %s", group, len(group_posts))
            source_posts.extend(group_posts)
            checked_groups += 1
            if progress is not None:
                progress.update(
                    _format_progress_message(
                        checked_groups=checked_groups,
                        total_groups=len(source_groups),
                        failed_groups=len(failed_groups),
                    )
                )

            if self.search_stop_requested:
                return self._finish_stopped_search(
                    progress=progress,
                    peer_id=target_peer_id,
                    ff_full_import=ff_full_import,
                    ff_posts_seen=len(ff_posts),
                    source_posts_seen=len(dedupe_posts(source_posts)),
                    keywords=effective_keywords,
                    interval_days=interval_days,
                    checked_groups=checked_groups,
                    total_groups=len(source_groups),
                    failed_groups=failed_groups,
                )

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
                keywords=effective_keywords or effective_hashtags,
                interval_days=interval_days,
                ff_posts_seen=len(ff_posts),
                source_posts_seen=len(unique_source_posts),
                filtered_posts=len(filtered_posts),
                final_posts=len(final_posts),
                failed_groups=failed_groups,
            )
            message = f"{message}\n\n{links_message}"
        else:
            message = links_message

        _finish_message(self.message_client, target_peer_id, message, progress)
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
            failed_groups=tuple(failed_groups),
        )

    def handle_chat_message(
        self,
        message: IncomingMessage,
        now: datetime | None = None,
    ) -> SyncResult | ControlResult | None:
        control_command = parse_control_command(message.text)
        command = parse_search_command(message.text)
        if command is None and control_command is None:
            return None

        if message.from_id not in self.settings.allowed_user_ids:
            LOGGER.info("Команда отклонена: пользователь %s не входит в список разрешенных", message.from_id)
            self.message_client.send_message(
                message.peer_id,
                "Команда доступна только разрешенным пользователям.",
            )
            return None

        if control_command is not None:
            return self._handle_control_command(message, control_command)

        if command is None:
            return None

        LOGGER.info(
            "Получена команда поиска от пользователя %s в чате %s: слова=%s, интервал=%s д.",
            message.from_id,
            message.peer_id,
            ", ".join(command.keywords) if command.keywords else "из файла",
            command.interval_days,
        )
        return self.run_once(
            now=now,
            keywords=command.keywords,
            hashtags=command.hashtags,
            interval_days=command.interval_days,
            peer_id=message.peer_id,
            include_summary=True,
            show_progress=True,
        )

    def poll_chat_once(self, now: datetime | None = None) -> int:
        if self.chat_client is None:
            raise RuntimeError("Для проверки команд нужен клиент чата")

        peer_id = self.resolve_target_peer_id()
        metadata_key = f"last_processed_message_sequence:{peer_id}"
        last_processed_value = self.storage.get_metadata(metadata_key)
        messages = sorted(
            self.chat_client.iter_recent_messages(peer_id, count=50),
            key=lambda item: item.sequence_id,
        )

        if last_processed_value is None:
            max_sequence_id = max((message.sequence_id for message in messages), default=0)
            self.storage.set_metadata(metadata_key, str(max_sequence_id))
            if max_sequence_id:
                LOGGER.info("История чата %s отмечена как уже прочитанная до сообщения %s", peer_id, max_sequence_id)
            return 0

        last_processed = int(last_processed_value)
        handled_count = 0
        for message in messages:
            if message.sequence_id <= last_processed:
                continue

            self.storage.set_metadata(metadata_key, str(message.sequence_id))
            last_processed = message.sequence_id
            if self.handle_chat_message(message, now=now) is not None:
                handled_count += 1

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

    def _collect_wall_posts(
        self,
        group: str,
        since_timestamp: int | None,
        failed_groups: list[str],
    ) -> list[Post]:
        try:
            return list(self.wall_client.iter_wall_posts(group, since_timestamp))
        except Exception:
            LOGGER.exception("Группа %s пропущена из-за ошибки загрузки стены", group)
            failed_groups.append(group)
            return []

    def _handle_control_command(
        self,
        message: IncomingMessage,
        command: ControlCommand,
    ) -> ControlResult:
        if command.action == "shutdown":
            self.request_stop_search()
            self.request_shutdown()
            response = "Остановка программы запрошена."
        else:
            self.request_stop_search()
            response = "Остановка поиска запрошена."

        LOGGER.info("Получена команда управления %s от пользователя %s", command.action, message.from_id)
        self.message_client.send_message(message.peer_id, response)
        return ControlResult(command=command, message=response)

    def _finish_stopped_search(
        self,
        progress: "_ProgressMessage | None",
        peer_id: int,
        ff_full_import: bool,
        ff_posts_seen: int,
        source_posts_seen: int,
        keywords: tuple[str, ...],
        interval_days: int,
        checked_groups: int,
        total_groups: int,
        failed_groups: Sequence[str],
    ) -> SyncResult:
        message = f"Поиск остановлен.\nПроверено групп: {checked_groups} из {total_groups}."
        _finish_message(self.message_client, peer_id, message, progress)
        return SyncResult(
            ff_full_import=ff_full_import,
            ff_posts_seen=ff_posts_seen,
            source_posts_seen=source_posts_seen,
            filtered_posts=0,
            final_posts=0,
            message=message,
            keywords=keywords,
            interval_days=interval_days,
            failed_groups=tuple(failed_groups),
            stopped=True,
        )


def _format_sync_report(
    keywords: Sequence[str],
    interval_days: int,
    ff_posts_seen: int,
    source_posts_seen: int,
    filtered_posts: int,
    final_posts: int,
    failed_groups: Sequence[str] = (),
) -> str:
    lines = [
        "Поиск выполнен.",
        f"Ключевые слова: {', '.join(keywords)}.",
        f"Интервал: {interval_days} д.",
        f"Постов ФФ загружено: {ff_posts_seen}.",
        f"Постов источников проверено: {source_posts_seen}.",
        f"После фильтра по словам: {filtered_posts}.",
        f"Итоговых ссылок: {final_posts}.",
    ]
    if failed_groups:
        lines.append(f"Групп с ошибками: {len(failed_groups)} ({', '.join(failed_groups)}).")
    return "\n".join(lines)


def _format_progress_message(
    checked_groups: int,
    total_groups: int,
    failed_groups: int = 0,
) -> str:
    percent = 100 if total_groups == 0 else int(checked_groups / total_groups * 100)
    lines = [
        "Поиск выполняется.",
        f"Проверено групп: {checked_groups} из {total_groups} ({percent}%).",
    ]
    if failed_groups:
        lines.append(f"Групп с ошибками: {failed_groups}.")
    return "\n".join(lines)


def split_message(message: str, max_length: int | None = None) -> list[str]:
    limit = MAX_VK_MESSAGE_LENGTH if max_length is None else max_length
    if limit <= 0:
        raise ValueError("Максимальная длина сообщения должна быть больше нуля")

    if len(message) <= limit:
        return [message]

    chunks: list[str] = []
    current = ""
    for line in message.splitlines():
        if len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            while len(line) > limit:
                chunks.append(line[:limit])
                line = line[limit:]

        candidate = line if not current else f"{current}\n{line}"
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = line

    if current:
        chunks.append(current)
    return chunks or [""]


def _finish_message(
    message_client: MessageClient,
    peer_id: int,
    message: str,
    progress: "_ProgressMessage | None" = None,
) -> None:
    if progress is not None:
        progress.finish(message)
        return

    for chunk in split_message(message):
        message_client.send_message(peer_id, chunk)


def _extract_message_id(response: object) -> int | None:
    if isinstance(response, int):
        return response
    if isinstance(response, Mapping):
        for key in ("message_id", "conversation_message_id", "id"):
            if response.get(key) is not None:
                return int(response[key])
    return None


class _ProgressMessage:
    def __init__(self, message_client: MessageClient, peer_id: int, message_id: int | None) -> None:
        self.message_client = message_client
        self.peer_id = peer_id
        self.message_id = message_id

    @classmethod
    def start(cls, message_client: MessageClient, peer_id: int, message: str) -> "_ProgressMessage":
        response = message_client.send_message(peer_id, message)
        return cls(message_client, peer_id, _extract_message_id(response))

    def update(self, message: str) -> None:
        edit_message = getattr(self.message_client, "edit_message", None)
        if self.message_id is None or edit_message is None:
            return
        edit_message(self.peer_id, self.message_id, message)

    def finish(self, message: str) -> None:
        chunks = split_message(message)
        edit_message = getattr(self.message_client, "edit_message", None)
        if self.message_id is not None and edit_message is not None:
            edit_message(self.peer_id, self.message_id, chunks[0])
        else:
            self.message_client.send_message(self.peer_id, chunks[0])

        for chunk in chunks[1:]:
            self.message_client.send_message(self.peer_id, chunk)
