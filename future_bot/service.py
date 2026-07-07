from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from future_bot.config import CommandLine, Settings, load_command_lines, load_source_groups_file
from future_bot.logic import (
    ControlCommand,
    IncomingMessage,
    Post,
    dedupe_posts,
    filter_posts_by_groups,
    flatten_terms,
    format_groups,
    format_numbered_links,
    parse_control_command,
    parse_search_command,
    remove_liked_posts,
    remove_posts_linked_from_ff,
)
from future_bot.storage import NEW_POSTS_TABLE, Storage

LOGGER = logging.getLogger(__name__)
MAX_VK_MESSAGE_LENGTH = 4000
SOURCE_GROUP_CACHE_TTL = timedelta(hours=12)
PROGRESS_UPDATE_INTERVAL = timedelta(minutes=1)


class WallClient(Protocol):
    def iter_wall_posts(self, group: str, since_timestamp: int | None = None) -> Iterable[Post]:
        ...


class MessageClient(Protocol):
    def send_message(self, peer_id: int, message: str) -> object:
        ...


class ChatClient(Protocol):
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


@dataclass(frozen=True)
class _SourcePostsCache:
    source_groups: tuple[str, ...]
    since_timestamp: int
    fetched_at: datetime
    posts: tuple[Post, ...]
    failed_groups: tuple[str, ...]


class FutureBotService:
    def __init__(
        self,
        settings: Settings,
        wall_client: WallClient,
        message_client: MessageClient,
        storage: Storage,
        chat_client: ChatClient | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.wall_client = wall_client
        self.message_client = message_client
        self.storage = storage
        self.chat_client = chat_client
        self._clock = clock
        self._run_lock = threading.Lock()
        self._search_stop_event = threading.Event()
        self._shutdown_event = threading.Event()
        self._source_posts_cache: _SourcePostsCache | None = None
        self._search_executor: ThreadPoolExecutor | None = None

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

    def _now(self) -> datetime:
        if self._clock is not None:
            return self._clock()
        return datetime.now(ZoneInfo(self.settings.timezone))

    def run_once(
        self,
        now: datetime | None = None,
        groups: Sequence[Sequence[str]] | None = None,
        interval_days: int = 1,
        peer_id: int | None = None,
        include_summary: bool = False,
        show_progress: bool = False,
        use_source_cache: bool = False,
    ) -> SyncResult:
        with self._run_lock:
            self._search_stop_event.clear()
            try:
                results = self._run_search(
                    now=now,
                    groups=groups,
                    interval_days=interval_days,
                    peer_id=peer_id,
                    include_summary=include_summary,
                    show_progress=show_progress,
                    use_source_cache=use_source_cache,
                )
            finally:
                self._search_stop_event.clear()
        return _aggregate_results(results, interval_days=interval_days)

    def _resolve_queries(self, groups: Sequence[Sequence[str]] | None) -> list[CommandLine]:
        """Определяет список поисковых команд.

        Явно заданные группы образуют одну команду. Пустой запрос (например
        ``/поиск по () интервал 1д``) и запуск по таймеру разворачиваются в
        перечень команд из файла ``Список слов и хэштегов.txt`` - по одной
        команде на строку с отдельным отчетом.
        """

        normalized = tuple(tuple(term for term in group) for group in (groups or ()) if group)
        if normalized:
            return [CommandLine(raw=format_groups(normalized), groups=normalized)]
        return load_command_lines(self.settings.terms_file)

    def _run_search(
        self,
        now: datetime | None = None,
        groups: Sequence[Sequence[str]] | None = None,
        interval_days: int = 1,
        peer_id: int | None = None,
        include_summary: bool = False,
        show_progress: bool = False,
        use_source_cache: bool = False,
    ) -> list[SyncResult]:
        if interval_days <= 0:
            raise ValueError("Интервал поиска должен быть больше нуля")

        current_time = now or self._now()
        target_peer_id = peer_id or self.settings.target_peer_id
        source_since_timestamp = int((current_time - timedelta(days=interval_days)).timestamp())
        source_groups = load_source_groups_file(self.settings.source_groups_file)
        queries = self._resolve_queries(groups)
        all_keywords = flatten_terms(
            [group for query in queries for group in query.groups]
        )
        progress_started_at = current_time
        progress = (
            _ProgressMessage.start(
                self.message_client,
                target_peer_id,
                _format_progress_message(
                    checked_groups=0,
                    total_groups=len(source_groups),
                    started_at=progress_started_at,
                    current_time=progress_started_at,
                ),
                current_time=progress_started_at,
            )
            if show_progress
            else None
        )

        try:
            ff_full_import = not self.storage.has_ff_posts()
            ff_since = None if ff_full_import else self.storage.get_latest_ff_post_date()
            failed_groups: list[str] = []
            ff_posts = self._collect_wall_posts(self.settings.ff_group, ff_since, failed_groups)
            self.storage.upsert_ff_posts(ff_posts)
            self._prune_old_posts(current_time)
            LOGGER.info("Сохранено постов Формулы Футурологии: %s", len(ff_posts))

            source_posts: list[Post] = []
            checked_groups = 0
            source_failure_start = len(failed_groups)
            cache = (
                self._get_source_posts_cache(
                    source_groups=source_groups,
                    since_timestamp=source_since_timestamp,
                    current_time=current_time,
                )
                if use_source_cache
                else None
            )
            if cache is not None:
                source_posts = [post for post in cache.posts if post.date >= source_since_timestamp]
                failed_groups.extend(cache.failed_groups)
                checked_groups = len(source_groups)
                LOGGER.info(
                    "Используется кеш опроса %s групп от %s",
                    len(source_groups),
                    cache.fetched_at.isoformat(),
                )
            else:
                for group in source_groups:
                    if self.search_stop_requested:
                        return [
                            self._finish_stopped_search(
                                progress=progress,
                                peer_id=target_peer_id,
                                ff_full_import=ff_full_import,
                                ff_posts_seen=len(ff_posts),
                                source_posts_seen=len(dedupe_posts(source_posts)),
                                keywords=all_keywords,
                                interval_days=interval_days,
                                checked_groups=checked_groups,
                                total_groups=len(source_groups),
                                failed_groups=failed_groups,
                            )
                        ]

                    group_posts = self._collect_wall_posts(group, source_since_timestamp, failed_groups)
                    LOGGER.info("Получено постов из %s: %s", group, len(group_posts))
                    source_posts.extend(group_posts)
                    checked_groups += 1
                    if progress is not None:
                        progress_time = self._now()
                        progress.update(
                            _format_progress_message(
                                checked_groups=checked_groups,
                                total_groups=len(source_groups),
                                failed_groups=len(failed_groups),
                                started_at=progress_started_at,
                                current_time=progress_time,
                            ),
                            current_time=progress_time,
                        )

                    if self.search_stop_requested:
                        return [
                            self._finish_stopped_search(
                                progress=progress,
                                peer_id=target_peer_id,
                                ff_full_import=ff_full_import,
                                ff_posts_seen=len(ff_posts),
                                source_posts_seen=len(dedupe_posts(source_posts)),
                                keywords=all_keywords,
                                interval_days=interval_days,
                                checked_groups=checked_groups,
                                total_groups=len(source_groups),
                                failed_groups=failed_groups,
                            )
                        ]

                if use_source_cache:
                    self._source_posts_cache = _SourcePostsCache(
                        source_groups=source_groups,
                        since_timestamp=source_since_timestamp,
                        fetched_at=current_time,
                        posts=tuple(source_posts),
                        failed_groups=tuple(failed_groups[source_failure_start:]),
                    )

            unique_source_posts = dedupe_posts(source_posts)
            ff_links = self.storage.get_ff_links()

            results: list[SyncResult] = []
            messages: list[str] = []
            all_final_posts: list[Post] = []
            for query in queries:
                filtered_posts = filter_posts_by_groups(unique_source_posts, query.groups)
                final_posts = remove_posts_linked_from_ff(filtered_posts, ff_links)
                final_posts = remove_liked_posts(final_posts)
                final_posts = sorted(final_posts, key=lambda post: post.date, reverse=True)
                all_final_posts.extend(final_posts)

                links_message = format_numbered_links(
                    final_posts,
                    empty_message=(
                        f"За последние {interval_days} д. новых постов по запросу "
                        f"«{query.raw}» не найдено."
                    ),
                )
                if include_summary:
                    report = _format_sync_report(
                        keywords_text=format_groups(query.groups) or query.raw,
                        interval_days=interval_days,
                        ff_posts_seen=len(ff_posts),
                        source_posts_seen=len(unique_source_posts),
                        filtered_posts=len(filtered_posts),
                        final_posts=len(final_posts),
                        failed_groups=failed_groups,
                    )
                    message = f"{report}\n\n{links_message}"
                else:
                    message = links_message

                messages.append(message)
                results.append(
                    SyncResult(
                        ff_full_import=ff_full_import,
                        ff_posts_seen=len(ff_posts),
                        source_posts_seen=len(unique_source_posts),
                        filtered_posts=len(filtered_posts),
                        final_posts=len(final_posts),
                        message=message,
                        keywords=flatten_terms(query.groups),
                        interval_days=interval_days,
                        failed_groups=tuple(failed_groups),
                    )
                )

            self.storage.replace_new_posts(dedupe_posts(all_final_posts))

            for index, message in enumerate(messages):
                _finish_message(
                    self.message_client,
                    target_peer_id,
                    message,
                    progress if index == 0 else None,
                )

            self.storage.set_metadata("last_successful_sync_at", current_time.isoformat())
            return results
        except Exception as exc:
            if progress is not None:
                self._finish_unexpected_progress_error(progress, exc)
            raise

    def _prune_old_posts(self, current_time: datetime) -> None:
        """Удаляет устаревшие записи только из базы новых постов.

        База постов ФФ используется для дедупликации и должна только
        дополняться постами группы ФФ — её записи по возрасту не удаляются.
        """

        retention_days = self.settings.post_retention_days
        if retention_days <= 0:
            return
        cutoff = int((current_time - timedelta(days=retention_days)).timestamp())
        removed = self.storage.delete_posts_older_than(cutoff, table=NEW_POSTS_TABLE)
        if removed:
            LOGGER.info(
                "Удалено устаревших постов из базы новых постов (старше %s дней): %s",
                retention_days,
                removed,
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
            groups=command.groups,
            interval_days=command.interval_days,
            peer_id=message.peer_id,
            include_summary=True,
            show_progress=True,
            use_source_cache=True,
        )

    def poll_chat_once(self, now: datetime | None = None, background: bool = False) -> int:
        if self.chat_client is None:
            raise RuntimeError("Для проверки команд нужен клиент чата")

        peer_id = self.settings.target_peer_id
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

            # Игнорируем команды, на которые уже ответила группа (другой экземпляр
            # бота), чтобы несколько запусков не обрабатывали одну команду дважды.
            if self._command_already_answered(message, messages):
                if parse_control_command(message.text) or parse_search_command(message.text):
                    LOGGER.info(
                        "Команда %s пропущена: на нее уже ответила группа",
                        message.sequence_id,
                    )
                continue

            # Команды поиска выполняем в фоне, чтобы поток опроса чата продолжал
            # читать сообщения и мог немедленно распознать «/стоп поиск».
            # Команды управления обрабатываем сразу, синхронно.
            if background and self._is_background_search(message):
                self._submit_search(message, now)
                handled_count += 1
                continue

            if self.handle_chat_message(message, now=now) is not None:
                handled_count += 1

        return handled_count

    @staticmethod
    def _command_already_answered(message: IncomingMessage, messages: Sequence[IncomingMessage]) -> bool:
        """Проверяет, ответила ли уже группа на команду.

        Ответ группы (сообщение от имени сообщества, ``from_id < 0``), пришедший
        позже команды, означает, что команда уже обработана другим экземпляром
        бота, поэтому ее следует пропустить.
        """

        return any(
            other.sequence_id > message.sequence_id and other.from_id < 0
            for other in messages
        )

    def _is_background_search(self, message: IncomingMessage) -> bool:
        if parse_control_command(message.text) is not None:
            return False
        if parse_search_command(message.text) is None:
            return False
        return message.from_id in self.settings.allowed_user_ids

    def _submit_search(self, message: IncomingMessage, now: datetime | None) -> None:
        if self._search_executor is None:
            self._search_executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="ff-search"
            )
        self._search_executor.submit(self._safe_handle, message, now)

    def _safe_handle(self, message: IncomingMessage, now: datetime | None) -> None:
        try:
            self.handle_chat_message(message, now=now)
        except Exception:
            LOGGER.exception("Ошибка при фоновой обработке команды поиска")

    def shutdown_executor(self) -> None:
        executor = self._search_executor
        if executor is not None:
            executor.shutdown(wait=False)
            self._search_executor = None

    def _get_source_posts_cache(
        self,
        source_groups: tuple[str, ...],
        since_timestamp: int,
        current_time: datetime,
    ) -> _SourcePostsCache | None:
        cache = self._source_posts_cache
        if cache is None:
            return None
        if cache.source_groups != source_groups:
            return None
        if current_time - cache.fetched_at >= SOURCE_GROUP_CACHE_TTL:
            return None
        if cache.since_timestamp > since_timestamp:
            return None
        return cache

    def _finish_unexpected_progress_error(self, progress: "_ProgressMessage", exc: Exception) -> None:
        message = _format_unexpected_error_message(exc)
        try:
            progress.finish(message)
        except Exception:
            LOGGER.exception("Не удалось обновить статус после непредвиденной ошибки поиска")

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


def _aggregate_results(results: Sequence[SyncResult], interval_days: int) -> SyncResult:
    """Сводит отчеты по нескольким запросам в один результат.

    Возвращается вызывающему коду (планировщик, ``__main__``), тогда как
    пользователю в чат отправляется отдельный отчет по каждому запросу.
    """

    if not results:
        return SyncResult(
            ff_full_import=False,
            ff_posts_seen=0,
            source_posts_seen=0,
            filtered_posts=0,
            final_posts=0,
            message="",
            keywords=(),
            interval_days=interval_days,
        )
    if len(results) == 1:
        return results[0]

    first = results[0]
    return SyncResult(
        ff_full_import=first.ff_full_import,
        ff_posts_seen=first.ff_posts_seen,
        source_posts_seen=first.source_posts_seen,
        filtered_posts=sum(result.filtered_posts for result in results),
        final_posts=sum(result.final_posts for result in results),
        message="\n\n".join(result.message for result in results),
        keywords=flatten_terms([result.keywords for result in results]),
        interval_days=interval_days,
        failed_groups=first.failed_groups,
        stopped=any(result.stopped for result in results),
    )


def _format_sync_report(
    keywords_text: str,
    interval_days: int,
    ff_posts_seen: int,
    source_posts_seen: int,
    filtered_posts: int,
    final_posts: int,
    failed_groups: Sequence[str] = (),
) -> str:
    lines = [
        "Поиск выполнен.",
        f"Ключевые слова: {keywords_text}.",
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
    started_at: datetime | None = None,
    current_time: datetime | None = None,
) -> str:
    percent = 100 if total_groups == 0 else int(checked_groups / total_groups * 100)
    lines = [
        "Поиск выполняется.",
        f"Проверено групп: {checked_groups} из {total_groups} ({percent}%).",
        f"Примерное окончание: {_format_estimated_finish(checked_groups, total_groups, started_at, current_time)}.",
    ]
    if failed_groups:
        lines.append(f"Групп с ошибками: {failed_groups}.")
    return "\n".join(lines)


def _format_estimated_finish(
    checked_groups: int,
    total_groups: int,
    started_at: datetime | None,
    current_time: datetime | None,
) -> str:
    if current_time is None:
        return "рассчитывается"
    if total_groups == 0:
        return current_time.strftime("%H:%M")
    if checked_groups <= 0 or started_at is None:
        return "рассчитывается"

    elapsed_seconds = max((current_time - started_at).total_seconds(), 1.0)
    seconds_per_group = elapsed_seconds / checked_groups
    remaining_seconds = max(total_groups - checked_groups, 0) * seconds_per_group
    estimated_finish = current_time + timedelta(seconds=remaining_seconds)
    return estimated_finish.strftime("%H:%M")


def _format_unexpected_error_message(exc: Exception) -> str:
    details = f"{exc.__class__.__name__}: {exc}"
    return f"Непредвиденная ошибка.\n{details[-300:]}"


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
    def __init__(
        self,
        message_client: MessageClient,
        peer_id: int,
        message_id: int | None,
        last_update_at: datetime,
    ) -> None:
        self.message_client = message_client
        self.peer_id = peer_id
        self.message_id = message_id
        self.last_update_at = last_update_at

    @classmethod
    def start(
        cls,
        message_client: MessageClient,
        peer_id: int,
        message: str,
        current_time: datetime,
    ) -> "_ProgressMessage":
        response = message_client.send_message(peer_id, message)
        return cls(message_client, peer_id, _extract_message_id(response), current_time)

    def update(self, message: str, current_time: datetime, force: bool = False) -> bool:
        edit_message = getattr(self.message_client, "edit_message", None)
        if self.message_id is None or edit_message is None:
            return False
        if not force and current_time - self.last_update_at < PROGRESS_UPDATE_INTERVAL:
            return False
        edit_message(self.peer_id, self.message_id, message)
        self.last_update_at = current_time
        return True

    def finish(self, message: str) -> None:
        chunks = split_message(message)
        edit_message = getattr(self.message_client, "edit_message", None)
        if self.message_id is not None and edit_message is not None:
            edit_message(self.peer_id, self.message_id, chunks[0])
        else:
            self.message_client.send_message(self.peer_id, chunks[0])

        for chunk in chunks[1:]:
            self.message_client.send_message(self.peer_id, chunk)
