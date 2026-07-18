import threading
from datetime import datetime, timedelta, timezone

import pytest

from future_bot.config import Settings
from future_bot.logic import IncomingMessage, Post
from future_bot.service import FutureBotService
from future_bot.storage import Storage


class FakeWallClient:
    def __init__(self, posts_by_group):
        self.posts_by_group = posts_by_group
        self.calls = []

    def iter_wall_posts(self, group, since_timestamp=None):
        self.calls.append((group, since_timestamp))
        return iter(self.posts_by_group.get(group, ()))


class FailingWallClient(FakeWallClient):
    def __init__(self, posts_by_group, failures_by_group):
        super().__init__(posts_by_group)
        self.failures_by_group = failures_by_group

    def iter_wall_posts(self, group, since_timestamp=None):
        self.calls.append((group, since_timestamp))
        if group in self.failures_by_group:
            raise RuntimeError(self.failures_by_group[group])
        return iter(self.posts_by_group.get(group, ()))


class FakeMessageClient:
    def __init__(self):
        self.sent = []
        self.edits = []
        self.next_message_id = 1

    def send_message(self, peer_id, message):
        self.sent.append((peer_id, message))
        message_id = self.next_message_id
        self.next_message_id += 1
        return message_id

    def edit_message(self, peer_id, message_id, message):
        self.edits.append((peer_id, message_id, message))


class FakeChatClient:
    def __init__(self, peer_id, messages):
        self.peer_id = peer_id
        self.messages = messages
        self.history_calls = []

    def iter_recent_messages(self, peer_id, count=50):
        self.history_calls.append((peer_id, count))
        return iter(self.messages)


class FakeClock:
    def __init__(self, *values):
        self.values = list(values)
        self.last_value = values[-1]

    def __call__(self):
        if self.values:
            self.last_value = self.values.pop(0)
        return self.last_value


def write_runtime_lists(tmp_path, groups, terms=("Технология",)):
    groups_file = tmp_path / "Список групп.txt"
    terms_file = tmp_path / "Список слов и хэштегов.txt"
    groups_file.write_text("\n".join(groups), encoding="utf-8")
    terms_file.write_text("\n".join(terms), encoding="utf-8")
    return groups_file, terms_file


def test_run_once_builds_ff_database_filters_dedupes_and_sends_digest(tmp_path):
    groups_file, terms_file = write_runtime_lists(
        tmp_path,
        ("https://vk.ru/eofru", "https://vk.ru/asimovonline"),
    )
    settings = Settings(
        vk_group_token="group-token",
        vk_user_token="user-token",
        vk_message_token="user-token",
        database_path=tmp_path / "future_bot.sqlite3",
        ff_group="world_of_futuristica",
        source_groups_file=groups_file,
        terms_file=terms_file,
        target_peer_id=2_000_000_170,
        timezone="UTC",
    )
    ff_post = Post(
        owner_id=-10,
        post_id=1,
        source_group="world_of_futuristica",
        date=int(datetime(2026, 6, 27, 3, 0, tzinfo=timezone.utc).timestamp()),
        text="Уже опубликовано",
        links=("https://vk.com/wall-20_1",),
    )
    duplicate_source_post = Post(
        owner_id=-20,
        post_id=1,
        source_group="eofru",
        date=200,
        text="Новая технология",
    )
    relevant_source_post = Post(
        owner_id=-30,
        post_id=2,
        source_group="asimovonline",
        date=300,
        text="Свежий материал #технология",
    )
    irrelevant_source_post = Post(
        owner_id=-30,
        post_id=3,
        source_group="asimovonline",
        date=301,
        text="Свежий материал без ключевых слов",
    )
    wall_client = FakeWallClient(
        {
            "world_of_futuristica": [ff_post],
            "eofru": [duplicate_source_post],
            "asimovonline": [relevant_source_post, irrelevant_source_post],
        }
    )
    message_client = FakeMessageClient()

    service = FutureBotService(settings, wall_client, message_client, Storage(settings.database_path))
    result = service.run_once(now=datetime(2026, 6, 28, 3, 0, tzinfo=timezone.utc))

    assert result.ff_full_import is True
    assert result.ff_posts_seen == 1
    assert result.source_posts_seen == 3
    assert result.filtered_posts == 2
    assert result.final_posts == 1
    assert message_client.sent == [
        (2_000_000_170, "1. https://vk.com/wall-30_2"),
    ]
    assert [post.source_url for post in Storage(settings.database_path).list_new_posts()] == [
        "https://vk.com/wall-30_2",
    ]
    assert wall_client.calls[0] == ("world_of_futuristica", None)


def test_run_once_excludes_posts_already_liked_by_user_account(tmp_path):
    groups_file, terms_file = write_runtime_lists(
        tmp_path,
        ("eofru",),
        ("Технология",),
    )
    settings = Settings(
        vk_group_token="group-token",
        vk_user_token="user-token",
        vk_message_token="user-token",
        database_path=tmp_path / "future_bot.sqlite3",
        ff_group="world_of_futuristica",
        source_groups_file=groups_file,
        terms_file=terms_file,
        target_peer_id=2_000_000_170,
        timezone="UTC",
    )
    liked_post = Post(
        owner_id=-30,
        post_id=1,
        source_group="eofru",
        date=200,
        text="Новая технология",
        liked=True,
    )
    not_liked_post = Post(
        owner_id=-30,
        post_id=2,
        source_group="eofru",
        date=300,
        text="Свежая технология",
        liked=False,
    )
    wall_client = FakeWallClient(
        {
            "world_of_futuristica": [],
            "eofru": [liked_post, not_liked_post],
        }
    )
    message_client = FakeMessageClient()

    service = FutureBotService(settings, wall_client, message_client, Storage(settings.database_path))
    result = service.run_once(now=datetime(2026, 6, 28, 3, 0, tzinfo=timezone.utc))

    assert result.final_posts == 1
    assert message_client.sent == [
        (2_000_000_170, "1. https://vk.com/wall-30_2"),
    ]


def test_run_once_refreshes_ff_posts_from_latest_stored_date(tmp_path):
    groups_file, terms_file = write_runtime_lists(tmp_path, ("eofru",))
    settings = Settings(
        vk_group_token="group-token",
        vk_user_token="user-token",
        vk_message_token="group-token",
        database_path=tmp_path / "future_bot.sqlite3",
        ff_group="world_of_futuristica",
        source_groups_file=groups_file,
        terms_file=terms_file,
        timezone="UTC",
    )
    storage = Storage(settings.database_path)
    storage.upsert_ff_posts(
        [
            Post(
                owner_id=-10,
                post_id=1,
                source_group="world_of_futuristica",
                date=100,
                text="Старый пост ФФ",
            )
        ]
    )
    wall_client = FakeWallClient({"world_of_futuristica": [], "eofru": []})
    message_client = FakeMessageClient()

    service = FutureBotService(settings, wall_client, message_client, storage)
    result = service.run_once(now=datetime(2026, 6, 28, 3, 0, tzinfo=timezone.utc))

    assert result.ff_full_import is False
    assert wall_client.calls[0] == ("world_of_futuristica", 100)


def test_run_once_skips_source_group_errors_and_reports_them(tmp_path):
    groups_file, terms_file = write_runtime_lists(
        tmp_path,
        ("closedgroup", "eofru"),
        ("Технология",),
    )
    settings = Settings(
        vk_group_token="group-token",
        vk_user_token="user-token",
        vk_message_token="group-token",
        database_path=tmp_path / "future_bot.sqlite3",
        ff_group="world_of_futuristica",
        source_groups_file=groups_file,
        terms_file=terms_file,
        timezone="UTC",
    )
    source_post = Post(owner_id=-20, post_id=1, source_group="eofru", date=300, text="Технология")
    wall_client = FailingWallClient(
        {"world_of_futuristica": [], "eofru": [source_post]},
        {"closedgroup": "Ошибка VK API wall.get: 15 Access denied: wall is disabled"},
    )
    message_client = FakeMessageClient()
    service = FutureBotService(settings, wall_client, message_client, Storage(settings.database_path))

    result = service.run_once(now=datetime(2026, 6, 28, 3, 0, tzinfo=timezone.utc), include_summary=True)

    assert result.source_posts_seen == 1
    assert result.final_posts == 1
    assert result.failed_groups == ("closedgroup",)
    assert message_client.sent == [
        (
            2_000_000_015,
            "Поиск выполнен.\n"
            "Ключевые слова: Технология.\n"
            "Интервал: 1 д.\n"
            "Постов ФФ загружено: 0.\n"
            "Постов источников проверено: 1.\n"
            "После фильтра по словам: 1.\n"
            "Итоговых ссылок: 1.\n"
            "Групп с ошибками: 1 (closedgroup).\n\n"
            "1. https://vk.com/wall-20_1",
        )
    ]


def test_run_once_reloads_groups_and_terms_files_for_each_activation(tmp_path):
    groups_file, terms_file = write_runtime_lists(tmp_path, ("eofru",), ("Технология",))
    settings = Settings(
        vk_group_token="group-token",
        vk_user_token="user-token",
        vk_message_token="group-token",
        database_path=tmp_path / "future_bot.sqlite3",
        ff_group="world_of_futuristica",
        source_groups_file=groups_file,
        terms_file=terms_file,
        timezone="UTC",
    )
    wall_client = FakeWallClient(
        {
            "world_of_futuristica": [],
            "eofru": [Post(owner_id=-20, post_id=1, source_group="eofru", date=300, text="Технология")],
            "asimovonline": [
                Post(owner_id=-30, post_id=2, source_group="asimovonline", date=400, text="ИИ")
            ],
        }
    )
    message_client = FakeMessageClient()
    service = FutureBotService(settings, wall_client, message_client, Storage(settings.database_path))

    first = service.run_once(now=datetime(2026, 6, 28, 3, 0, tzinfo=timezone.utc))
    groups_file.write_text("https://vk.ru/asimovonline\n", encoding="utf-8")
    terms_file.write_text("ИИ\n", encoding="utf-8")
    second = service.run_once(now=datetime(2026, 6, 29, 3, 0, tzinfo=timezone.utc))

    assert first.keywords == ("Технология",)
    assert second.keywords == ("ИИ",)
    assert ("eofru", 1782529200) in wall_client.calls
    assert ("asimovonline", 1782615600) in wall_client.calls
    assert message_client.sent == [
        (2_000_000_015, "1. https://vk.com/wall-20_1"),
        (2_000_000_015, "1. https://vk.com/wall-30_2"),
    ]


def test_handle_allowed_chat_search_command_uses_chat_peer_and_interval(tmp_path):
    groups_file, terms_file = write_runtime_lists(tmp_path, ("eofru",), ("ИИ",))
    settings = Settings(
        vk_group_token="group-token",
        vk_user_token="user-token",
        vk_message_token="group-token",
        database_path=tmp_path / "future_bot.sqlite3",
        ff_group="world_of_futuristica",
        source_groups_file=groups_file,
        terms_file=terms_file,
        target_peer_id=2_000_000_015,
        allowed_user_ids=(199592366,),
        timezone="UTC",
    )
    now = datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc)
    source_post = Post(
        owner_id=-20,
        post_id=5,
        source_group="eofru",
        date=300,
        text="Новая технология будущего",
    )
    wall_client = FakeWallClient(
        {
            "world_of_futuristica": [],
            "eofru": [source_post],
        }
    )
    message_client = FakeMessageClient()
    service = FutureBotService(settings, wall_client, message_client, Storage(settings.database_path))

    result = service.handle_chat_message(
        IncomingMessage(
            peer_id=2_000_000_015,
            from_id=199592366,
            text="/поиск по (Технологии, Технология) интервал 5д",
            date=1,
            conversation_message_id=10,
        ),
        now=now,
    )

    expected_since = int((now.replace(tzinfo=timezone.utc).timestamp())) - 5 * 24 * 60 * 60
    assert result is not None
    assert result.interval_days == 5
    assert result.keywords == ("Технологии", "Технология")
    assert wall_client.calls[-1] == ("eofru", expected_since)
    assert message_client.sent == [
        (
            2_000_000_015,
            "Поиск выполняется.\n"
            "Проверено групп: 0 из 1 (0%).\n"
            "Примерное окончание: рассчитывается.",
        )
    ]
    assert message_client.edits[-1] == (
        2_000_000_015,
        1,
        "Поиск выполнен.\n"
        "Ключевые слова: Технологии, Технология.\n"
        "Интервал: 5 д.\n"
        "Постов ФФ загружено: 0.\n"
        "Постов источников проверено: 1.\n"
        "После фильтра по словам: 1.\n"
        "Итоговых ссылок: 1.\n\n"
        "1. https://vk.com/wall-20_5",
    )


def test_empty_chat_search_command_uses_terms_file(tmp_path):
    groups_file, terms_file = write_runtime_lists(tmp_path, ("eofru",), ("ИИ", "#роботы"))
    settings = Settings(
        vk_group_token="group-token",
        vk_user_token="user-token",
        vk_message_token="group-token",
        database_path=tmp_path / "future_bot.sqlite3",
        ff_group="world_of_futuristica",
        source_groups_file=groups_file,
        terms_file=terms_file,
        target_peer_id=2_000_000_015,
        allowed_user_ids=(199592366,),
        timezone="UTC",
    )
    source_post = Post(
        owner_id=-20,
        post_id=5,
        source_group="eofru",
        date=300,
        text="Свежий обзор #роботы",
    )
    wall_client = FakeWallClient({"world_of_futuristica": [], "eofru": [source_post]})
    message_client = FakeMessageClient()
    service = FutureBotService(settings, wall_client, message_client, Storage(settings.database_path))

    result = service.handle_chat_message(
        IncomingMessage(
            peer_id=2_000_000_015,
            from_id=199592366,
            text="/поиск по () интервал 5д",
            date=1,
            conversation_message_id=10,
        ),
        now=datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc),
    )

    # Каждая строка файла - отдельная команда с отдельным отчетом.
    assert result is not None
    assert result.keywords == ("ИИ", "#роботы")
    assert result.final_posts == 1
    assert wall_client.calls[-1][0] == "eofru"
    # Первый отчет (строка «ИИ») отправлен правкой сообщения о прогрессе.
    assert "Ключевые слова: ИИ." in message_client.edits[-1][2]
    # Второй отчет (строка «#роботы») содержит найденную ссылку.
    second_report = message_client.sent[-1][1]
    assert "Ключевые слова: #роботы." in second_report
    assert "1. https://vk.com/wall-20_5" in second_report


def test_handle_chat_search_command_denies_unlisted_user(tmp_path):
    groups_file, terms_file = write_runtime_lists(tmp_path, ("eofru",))
    settings = Settings(
        vk_group_token="group-token",
        vk_user_token="user-token",
        vk_message_token="group-token",
        database_path=tmp_path / "future_bot.sqlite3",
        source_groups_file=groups_file,
        terms_file=terms_file,
        allowed_user_ids=(199592366,),
        timezone="UTC",
    )
    wall_client = FakeWallClient({})
    message_client = FakeMessageClient()
    service = FutureBotService(settings, wall_client, message_client, Storage(settings.database_path))

    result = service.handle_chat_message(
        IncomingMessage(
            peer_id=2_000_000_015,
            from_id=123,
            text="/поиск по (Технология) интервал 5д",
            date=1,
            conversation_message_id=10,
        )
    )

    assert result is None
    assert wall_client.calls == []
    assert message_client.sent == [
        (2_000_000_015, "Команда доступна только разрешенным пользователям.")
    ]


def test_poll_chat_once_uses_configured_peer_id_and_processes_new_command(tmp_path):
    groups_file, terms_file = write_runtime_lists(tmp_path, ("eofru",))
    settings = Settings(
        vk_group_token="group-token",
        vk_user_token="user-token",
        vk_message_token="group-token",
        database_path=tmp_path / "future_bot.sqlite3",
        ff_group="world_of_futuristica",
        source_groups_file=groups_file,
        terms_file=terms_file,
        target_peer_id=2_000_000_099,
        allowed_user_ids=(199592366,),
        timezone="UTC",
    )
    old_message = IncomingMessage(
        peer_id=2_000_000_099,
        from_id=199592366,
        text="/поиск по (Технология) интервал 1д",
        date=1,
        conversation_message_id=7,
    )
    new_message = IncomingMessage(
        peer_id=2_000_000_099,
        from_id=199592366,
        text="/поиск по (Технология) интервал 1д",
        date=2,
        conversation_message_id=8,
    )
    chat_client = FakeChatClient(
        peer_id=2_000_000_123,
        messages=[old_message],
    )
    wall_client = FakeWallClient({"world_of_futuristica": [], "eofru": []})
    message_client = FakeMessageClient()
    service = FutureBotService(
        settings,
        wall_client,
        message_client,
        Storage(settings.database_path),
        chat_client=chat_client,
    )

    first_poll_count = service.poll_chat_once(now=datetime(2026, 6, 28, 3, 0, tzinfo=timezone.utc))
    chat_client.messages = [old_message, new_message]
    handled_count = service.poll_chat_once(now=datetime(2026, 6, 28, 3, 1, tzinfo=timezone.utc))

    assert first_poll_count == 0
    assert handled_count == 1
    assert chat_client.history_calls == [(2_000_000_099, 50), (2_000_000_099, 50)]
    assert message_client.sent[0][0] == 2_000_000_099
    assert Storage(settings.database_path).get_metadata(
        "last_processed_message_sequence:2000000099"
    ) == "8"


def test_chat_search_reuses_recent_source_group_scan_when_groups_unchanged(tmp_path):
    groups_file, terms_file = write_runtime_lists(tmp_path, ("eofru",), ("Технология",))
    settings = Settings(
        vk_group_token="group-token",
        vk_user_token="user-token",
        vk_message_token="group-token",
        database_path=tmp_path / "future_bot.sqlite3",
        ff_group="world_of_futuristica",
        source_groups_file=groups_file,
        terms_file=terms_file,
        allowed_user_ids=(199592366,),
        timezone="UTC",
    )
    first_post = Post(
        owner_id=-20,
        post_id=1,
        source_group="eofru",
        date=int(datetime(2026, 6, 28, 11, 0, tzinfo=timezone.utc).timestamp()),
        text="Технология",
    )
    second_post = Post(
        owner_id=-20,
        post_id=2,
        source_group="eofru",
        date=int(datetime(2026, 6, 28, 17, 0, tzinfo=timezone.utc).timestamp()),
        text="Технология",
    )
    wall_client = FakeWallClient({"world_of_futuristica": [], "eofru": [first_post]})
    message_client = FakeMessageClient()
    service = FutureBotService(settings, wall_client, message_client, Storage(settings.database_path))

    service.handle_chat_message(
        IncomingMessage(
            peer_id=2_000_000_015,
            from_id=199592366,
            text="/поиск по (Технология) интервал 1д",
            date=1,
            conversation_message_id=10,
        ),
        now=datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc),
    )
    wall_client.posts_by_group["eofru"] = [second_post]
    result = service.handle_chat_message(
        IncomingMessage(
            peer_id=2_000_000_015,
            from_id=199592366,
            text="/поиск по (Технология) интервал 1д",
            date=2,
            conversation_message_id=11,
        ),
        now=datetime(2026, 6, 28, 18, 0, tzinfo=timezone.utc),
    )

    source_calls = [call for call in wall_client.calls if call[0] == "eofru"]
    assert len(source_calls) == 1
    assert result is not None
    # Пост из кеша уже был выдан первым поиском и записан в базу новых постов,
    # поэтому повторно он в отчёт не попадает.
    assert result.final_posts == 0
    assert "не найдено" in message_client.edits[-1][2]
    assert [post.source_url for post in Storage(settings.database_path).list_new_posts()] == [
        "https://vk.com/wall-20_1",
    ]


def test_chat_search_refreshes_recent_group_scan_when_group_file_changes(tmp_path):
    groups_file, terms_file = write_runtime_lists(tmp_path, ("eofru",), ("Технология",))
    settings = Settings(
        vk_group_token="group-token",
        vk_user_token="user-token",
        vk_message_token="group-token",
        database_path=tmp_path / "future_bot.sqlite3",
        ff_group="world_of_futuristica",
        source_groups_file=groups_file,
        terms_file=terms_file,
        allowed_user_ids=(199592366,),
        timezone="UTC",
    )
    wall_client = FakeWallClient(
        {
            "world_of_futuristica": [],
            "eofru": [Post(owner_id=-20, post_id=1, source_group="eofru", date=300, text="Технология")],
            "asimovonline": [
                Post(owner_id=-30, post_id=2, source_group="asimovonline", date=400, text="Технология")
            ],
        }
    )
    message_client = FakeMessageClient()
    service = FutureBotService(settings, wall_client, message_client, Storage(settings.database_path))

    service.handle_chat_message(
        IncomingMessage(
            peer_id=2_000_000_015,
            from_id=199592366,
            text="/поиск по (Технология) интервал 1д",
            date=1,
            conversation_message_id=10,
        ),
        now=datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc),
    )
    groups_file.write_text("asimovonline\n", encoding="utf-8")
    service.handle_chat_message(
        IncomingMessage(
            peer_id=2_000_000_015,
            from_id=199592366,
            text="/поиск по (Технология) интервал 1д",
            date=2,
            conversation_message_id=11,
        ),
        now=datetime(2026, 6, 28, 18, 0, tzinfo=timezone.utc),
    )

    assert ("eofru", 1782561600) in wall_client.calls
    assert ("asimovonline", 1782583200) in wall_client.calls


def test_poll_chat_once_marks_command_processed_before_search_errors(tmp_path):
    groups_file, terms_file = write_runtime_lists(tmp_path, ("eofru",))
    settings = Settings(
        vk_group_token="group-token",
        vk_user_token="user-token",
        vk_message_token="group-token",
        database_path=tmp_path / "future_bot.sqlite3",
        ff_group="world_of_futuristica",
        source_groups_file=groups_file,
        terms_file=terms_file,
        target_peer_id=2_000_000_099,
        allowed_user_ids=(199592366,),
        timezone="UTC",
    )
    message = IncomingMessage(
        peer_id=2_000_000_099,
        from_id=199592366,
        text="/поиск по (Технология) интервал 1д",
        date=1,
        conversation_message_id=7,
    )
    chat_client = FakeChatClient(peer_id=2_000_000_099, messages=[message])
    wall_client = FailingWallClient(
        {"world_of_futuristica": []},
        {"eofru": "boom"},
    )
    message_client = FakeMessageClient()
    storage = Storage(settings.database_path)
    storage.set_metadata("last_processed_message_sequence:2000000099", "6")
    service = FutureBotService(
        settings,
        wall_client,
        message_client,
        storage,
        chat_client=chat_client,
    )

    handled_count = service.poll_chat_once(now=datetime(2026, 6, 28, 3, 0, tzinfo=timezone.utc))

    assert handled_count == 1
    assert storage.get_metadata("last_processed_message_sequence:2000000099") == "7"


def test_chat_search_edits_progress_message_and_splits_long_final_report(tmp_path, monkeypatch):
    groups_file, terms_file = write_runtime_lists(tmp_path, ("eofru",), ("Технология",))
    settings = Settings(
        vk_group_token="group-token",
        vk_user_token="user-token",
        vk_message_token="group-token",
        database_path=tmp_path / "future_bot.sqlite3",
        ff_group="world_of_futuristica",
        source_groups_file=groups_file,
        terms_file=terms_file,
        allowed_user_ids=(199592366,),
        timezone="UTC",
    )
    posts = [
        Post(owner_id=-20, post_id=index, source_group="eofru", date=300 + index, text="Технология")
        for index in range(1, 6)
    ]
    wall_client = FakeWallClient({"world_of_futuristica": [], "eofru": posts})
    message_client = FakeMessageClient()
    service = FutureBotService(settings, wall_client, message_client, Storage(settings.database_path))
    monkeypatch.setattr("future_bot.service.MAX_VK_MESSAGE_LENGTH", 180)

    result = service.handle_chat_message(
        IncomingMessage(
            peer_id=2_000_000_015,
            from_id=199592366,
            text="/поиск по (Технология) интервал 1д",
            date=1,
            conversation_message_id=10,
        ),
        now=datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc),
    )

    assert result is not None
    assert message_client.sent[0] == (
        2_000_000_015,
        "Поиск выполняется.\n"
        "Проверено групп: 0 из 1 (0%).\n"
        "Примерное окончание: рассчитывается.",
    )
    assert "Поиск выполнен." in message_client.edits[-1][2]
    assert len(message_client.sent) > 1
    assert all(len(message) <= 180 for _, message in message_client.sent[1:])
    assert all(len(message) <= 180 for _, _, message in message_client.edits)


def test_chat_search_updates_progress_once_per_minute_with_estimated_finish(tmp_path):
    groups_file, terms_file = write_runtime_lists(
        tmp_path,
        ("first", "second", "third"),
        ("Технология",),
    )
    settings = Settings(
        vk_group_token="group-token",
        vk_user_token="user-token",
        vk_message_token="group-token",
        database_path=tmp_path / "future_bot.sqlite3",
        ff_group="world_of_futuristica",
        source_groups_file=groups_file,
        terms_file=terms_file,
        allowed_user_ids=(199592366,),
        timezone="UTC",
    )
    wall_client = FakeWallClient(
        {
            "world_of_futuristica": [],
            "first": [Post(owner_id=-20, post_id=1, source_group="first", date=300, text="Технология")],
            "second": [Post(owner_id=-30, post_id=2, source_group="second", date=301, text="Технология")],
            "third": [Post(owner_id=-40, post_id=3, source_group="third", date=302, text="Технология")],
        }
    )
    message_client = FakeMessageClient()
    service = FutureBotService(
        settings,
        wall_client,
        message_client,
        Storage(settings.database_path),
        clock=FakeClock(
            datetime(2026, 6, 28, 12, 0, 30, tzinfo=timezone.utc),
            datetime(2026, 6, 28, 12, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 6, 28, 12, 1, 30, tzinfo=timezone.utc),
        ),
    )

    service.handle_chat_message(
        IncomingMessage(
            peer_id=2_000_000_015,
            from_id=199592366,
            text="/поиск по (Технология) интервал 1д",
            date=1,
            conversation_message_id=10,
        ),
        now=datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc),
    )

    progress_edits = [edit for edit in message_client.edits if "Поиск выполняется." in edit[2]]
    assert len(progress_edits) == 1
    assert progress_edits[0][2] == (
        "Поиск выполняется.\n"
        "Проверено групп: 2 из 3 (66%).\n"
        "Примерное окончание: 12:01."
    )
    assert "Примерное окончание: рассчитывается." in message_client.sent[0][1]


def test_progress_message_reports_unexpected_error_before_reraising(tmp_path):
    groups_file, terms_file = write_runtime_lists(tmp_path, ("eofru",), ("Технология",))
    settings = Settings(
        vk_group_token="group-token",
        vk_user_token="user-token",
        vk_message_token="group-token",
        database_path=tmp_path / "future_bot.sqlite3",
        ff_group="world_of_futuristica",
        source_groups_file=groups_file,
        terms_file=terms_file,
        allowed_user_ids=(199592366,),
        timezone="UTC",
    )
    wall_client = FakeWallClient(
        {
            "world_of_futuristica": [],
            "eofru": [Post(owner_id=-20, post_id=1, source_group="eofru", date=300, text="Технология")],
        }
    )
    message_client = FakeMessageClient()
    storage = Storage(settings.database_path)

    def fail_add_new_posts(posts):
        raise RuntimeError("database write exploded")

    storage.add_new_posts = fail_add_new_posts
    service = FutureBotService(settings, wall_client, message_client, storage)

    with pytest.raises(RuntimeError, match="database write exploded"):
        service.handle_chat_message(
            IncomingMessage(
                peer_id=2_000_000_015,
                from_id=199592366,
                text="/поиск по (Технология) интервал 1д",
                date=1,
                conversation_message_id=10,
            ),
            now=datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc),
        )

    assert message_client.edits[-1] == (
        2_000_000_015,
        1,
        "Непредвиденная ошибка.\nRuntimeError: database write exploded",
    )


def test_allowed_stop_commands_request_search_stop_and_shutdown(tmp_path):
    groups_file, terms_file = write_runtime_lists(tmp_path, ("eofru",))
    settings = Settings(
        vk_group_token="group-token",
        vk_user_token="user-token",
        vk_message_token="group-token",
        database_path=tmp_path / "future_bot.sqlite3",
        source_groups_file=groups_file,
        terms_file=terms_file,
        allowed_user_ids=(199592366,),
        timezone="UTC",
    )
    wall_client = FakeWallClient({})
    message_client = FakeMessageClient()
    service = FutureBotService(settings, wall_client, message_client, Storage(settings.database_path))

    stop_result = service.handle_chat_message(
        IncomingMessage(
            peer_id=2_000_000_015,
            from_id=199592366,
            text="/стоп поиск",
            date=1,
            conversation_message_id=10,
        )
    )
    shutdown_result = service.handle_chat_message(
        IncomingMessage(
            peer_id=2_000_000_015,
            from_id=199592366,
            text="/стоп программа",
            date=2,
            conversation_message_id=11,
        )
    )

    assert stop_result is not None
    assert shutdown_result is not None
    assert service.search_stop_requested is True
    assert service.shutdown_requested is True
    assert message_client.sent == [
        (2_000_000_015, "Остановка поиска запрошена."),
        (2_000_000_015, "Остановка программы запрошена."),
    ]


def test_run_once_never_prunes_ff_posts_by_age(tmp_path):
    groups_file, terms_file = write_runtime_lists(tmp_path, ("eofru",), ("Технология",))
    settings = Settings(
        vk_group_token="group-token",
        vk_user_token="user-token",
        vk_message_token="group-token",
        database_path=tmp_path / "future_bot.sqlite3",
        ff_group="world_of_futuristica",
        source_groups_file=groups_file,
        terms_file=terms_file,
        post_retention_days=10,
        timezone="UTC",
    )
    now = datetime(2026, 6, 28, 3, 0, tzinfo=timezone.utc)
    stale_date = int((now - timedelta(days=20)).timestamp())
    fresh_date = int((now - timedelta(days=2)).timestamp())
    storage = Storage(settings.database_path)
    storage.upsert_ff_posts(
        [
            Post(
                owner_id=-10,
                post_id=1,
                source_group="world_of_futuristica",
                date=stale_date,
                text="Старый пост",
                links=("https://vk.com/wall-1_1",),
            ),
            Post(
                owner_id=-10,
                post_id=2,
                source_group="world_of_futuristica",
                date=fresh_date,
                text="Свежий пост",
                links=("https://vk.com/wall-2_2",),
            ),
        ]
    )
    wall_client = FakeWallClient({"world_of_futuristica": [], "eofru": []})
    message_client = FakeMessageClient()
    service = FutureBotService(settings, wall_client, message_client, storage)

    service.run_once(now=now)

    assert storage.get_ff_links() == {
        "https://vk.com/wall-1_1",
        "https://vk.com/wall-2_2",
    }
    assert storage.get_latest_ff_post_date() == fresh_date


def test_run_once_prunes_new_posts_older_than_retention(tmp_path):
    groups_file, terms_file = write_runtime_lists(tmp_path, ("eofru",), ("Технология",))
    settings = Settings(
        vk_group_token="group-token",
        vk_user_token="user-token",
        vk_message_token="group-token",
        database_path=tmp_path / "future_bot.sqlite3",
        ff_group="world_of_futuristica",
        source_groups_file=groups_file,
        terms_file=terms_file,
        post_retention_days=10,
        timezone="UTC",
    )
    now = datetime(2026, 6, 28, 3, 0, tzinfo=timezone.utc)
    stale_date = int((now - timedelta(days=20)).timestamp())
    storage = Storage(settings.database_path)
    storage.replace_new_posts(
        [
            Post(
                owner_id=-20,
                post_id=1,
                source_group="eofru",
                date=stale_date,
                text="Старый оставшийся пост",
                links=("https://vk.com/wall-20_1",),
            ),
        ]
    )
    wall_client = FakeWallClient({"world_of_futuristica": [], "eofru": []})
    message_client = FakeMessageClient()
    service = FutureBotService(settings, wall_client, message_client, storage)

    service._prune_old_posts(now)

    assert storage.list_new_posts() == []


def test_timer_search_runs_each_terms_file_line_as_separate_report(tmp_path):
    groups_file, terms_file = write_runtime_lists(
        tmp_path,
        ("eofru",),
        ("ИИ", "антилопы"),
    )
    settings = Settings(
        vk_group_token="group-token",
        vk_user_token="user-token",
        vk_message_token="group-token",
        database_path=tmp_path / "future_bot.sqlite3",
        ff_group="world_of_futuristica",
        source_groups_file=groups_file,
        terms_file=terms_file,
        target_peer_id=2_000_000_050,
        timezone="UTC",
    )
    wall_client = FakeWallClient(
        {
            "world_of_futuristica": [],
            "eofru": [
                Post(owner_id=-20, post_id=1, source_group="eofru", date=300, text="Развитие ИИ"),
                Post(owner_id=-30, post_id=2, source_group="eofru", date=301, text="Стадо антилопы"),
            ],
        }
    )
    message_client = FakeMessageClient()
    service = FutureBotService(settings, wall_client, message_client, Storage(settings.database_path))

    result = service.run_once(
        now=datetime(2026, 6, 28, 3, 0, tzinfo=timezone.utc),
        include_summary=True,
    )

    assert result.keywords == ("ИИ", "антилопы")
    assert result.final_posts == 2
    reports = [message for _, message in message_client.sent]
    assert len(reports) == 2
    assert any("Ключевые слова: ИИ." in report and "wall-20_1" in report for report in reports)
    assert any("Ключевые слова: антилопы." in report and "wall-30_2" in report for report in reports)


def test_poll_chat_skips_command_already_answered_by_group(tmp_path):
    groups_file, terms_file = write_runtime_lists(tmp_path, ("eofru",))
    settings = Settings(
        vk_group_token="group-token",
        vk_user_token="user-token",
        vk_message_token="group-token",
        database_path=tmp_path / "future_bot.sqlite3",
        ff_group="world_of_futuristica",
        source_groups_file=groups_file,
        terms_file=terms_file,
        target_peer_id=2_000_000_099,
        allowed_user_ids=(199592366,),
        timezone="UTC",
    )
    command = IncomingMessage(
        peer_id=2_000_000_099,
        from_id=199592366,
        text="/поиск по (Технология) интервал 1д",
        date=1,
        conversation_message_id=7,
    )
    group_reply = IncomingMessage(
        peer_id=2_000_000_099,
        from_id=-555,
        text="Поиск выполнен.",
        date=2,
        conversation_message_id=8,
    )
    chat_client = FakeChatClient(peer_id=2_000_000_099, messages=[command, group_reply])
    wall_client = FakeWallClient({"world_of_futuristica": [], "eofru": []})
    message_client = FakeMessageClient()
    storage = Storage(settings.database_path)
    storage.set_metadata("last_processed_message_sequence:2000000099", "6")
    service = FutureBotService(
        settings,
        wall_client,
        message_client,
        storage,
        chat_client=chat_client,
    )

    handled_count = service.poll_chat_once(now=datetime(2026, 6, 28, 3, 0, tzinfo=timezone.utc))

    assert handled_count == 0
    assert wall_client.calls == []
    assert storage.get_metadata("last_processed_message_sequence:2000000099") == "8"


def test_background_poll_handles_stop_without_waiting_for_running_search(tmp_path):
    groups_file, terms_file = write_runtime_lists(tmp_path, ("eofru",), ("Технология",))
    settings = Settings(
        vk_group_token="group-token",
        vk_user_token="user-token",
        vk_message_token="group-token",
        database_path=tmp_path / "future_bot.sqlite3",
        ff_group="world_of_futuristica",
        source_groups_file=groups_file,
        terms_file=terms_file,
        target_peer_id=2_000_000_099,
        allowed_user_ids=(199592366,),
        timezone="UTC",
    )
    release = threading.Event()

    class BlockingWallClient(FakeWallClient):
        def iter_wall_posts(self, group, since_timestamp=None):
            release.wait(timeout=5)
            return super().iter_wall_posts(group, since_timestamp)

    search = IncomingMessage(
        peer_id=2_000_000_099,
        from_id=199592366,
        text="/поиск по (Технология) интервал 1д",
        date=1,
        conversation_message_id=7,
    )
    stop = IncomingMessage(
        peer_id=2_000_000_099,
        from_id=199592366,
        text="/стоп поиск",
        date=2,
        conversation_message_id=8,
    )
    chat_client = FakeChatClient(peer_id=2_000_000_099, messages=[search, stop])
    wall_client = BlockingWallClient({"world_of_futuristica": [], "eofru": []})
    message_client = FakeMessageClient()
    storage = Storage(settings.database_path)
    storage.set_metadata("last_processed_message_sequence:2000000099", "6")
    service = FutureBotService(
        settings,
        wall_client,
        message_client,
        storage,
        chat_client=chat_client,
    )

    try:
        handled_count = service.poll_chat_once(
            now=datetime(2026, 6, 28, 3, 0, tzinfo=timezone.utc),
            background=True,
        )

        # Команда поиска выполняется в фоне, поэтому «/стоп поиск» обработана
        # немедленно, не дожидаясь завершения поиска.
        assert handled_count == 2
        assert service.search_stop_requested is True
        assert any(message == "Остановка поиска запрошена." for _, message in message_client.sent)
    finally:
        release.set()
        executor = service._search_executor
        if executor is not None:
            executor.shutdown(wait=True)


def test_run_once_stops_before_next_source_group_when_stop_requested(tmp_path):
    groups_file, terms_file = write_runtime_lists(tmp_path, ("first", "second"), ("Технология",))
    settings = Settings(
        vk_group_token="group-token",
        vk_user_token="user-token",
        vk_message_token="group-token",
        database_path=tmp_path / "future_bot.sqlite3",
        ff_group="world_of_futuristica",
        source_groups_file=groups_file,
        terms_file=terms_file,
        timezone="UTC",
    )

    class StopAfterFirstGroupWallClient(FakeWallClient):
        def iter_wall_posts(self, group, since_timestamp=None):
            posts = super().iter_wall_posts(group, since_timestamp)
            if group == "first":
                service.request_stop_search()
            return posts

    message_client = FakeMessageClient()
    wall_client = StopAfterFirstGroupWallClient(
        {
            "world_of_futuristica": [],
            "first": [Post(owner_id=-20, post_id=1, source_group="first", date=300, text="Технология")],
            "second": [Post(owner_id=-30, post_id=2, source_group="second", date=301, text="Технология")],
        }
    )
    service = FutureBotService(settings, wall_client, message_client, Storage(settings.database_path))

    result = service.run_once(now=datetime(2026, 6, 28, 3, 0, tzinfo=timezone.utc), show_progress=True)

    assert result.stopped is True
    assert result.final_posts == 0
    assert ("second", 1782529200) not in wall_client.calls
    assert Storage(settings.database_path).list_new_posts() == []
    assert message_client.edits[-1][2] == "Поиск остановлен.\nПроверено групп: 1 из 2."


class FakeLikesClient:
    def __init__(self, likers_by_post=None, failing_posts=()):
        self.likers_by_post = likers_by_post or {}
        self.failing_posts = set(failing_posts)
        self.calls = []

    def get_post_likers(self, owner_id, post_id, count=1000):
        self.calls.append((owner_id, post_id))
        if (owner_id, post_id) in self.failing_posts:
            raise RuntimeError("likes.getList недоступен")
        return set(self.likers_by_post.get((owner_id, post_id), ()))


def make_settings(tmp_path, groups_file, terms_file, **overrides):
    values = dict(
        vk_group_token="group-token",
        vk_user_token="",
        vk_service_token="service-token",
        vk_message_token="group-token",
        database_path=tmp_path / "future_bot.sqlite3",
        ff_group="world_of_futuristica",
        source_groups_file=groups_file,
        terms_file=terms_file,
        target_peer_id=2_000_000_170,
        allowed_user_ids=(199592366,),
        timezone="UTC",
    )
    values.update(overrides)
    return Settings(**values)


def test_run_once_excludes_posts_liked_by_chat_admins_via_likes_get_list(tmp_path):
    groups_file, terms_file = write_runtime_lists(tmp_path, ("eofru",), ("Технология",))
    settings = make_settings(tmp_path, groups_file, terms_file, admin_user_ids=(1849091,))
    liked_by_admin = Post(owner_id=-30, post_id=1, source_group="eofru", date=200, text="Новая технология")
    liked_by_stranger = Post(owner_id=-30, post_id=2, source_group="eofru", date=300, text="Свежая технология")
    wall_client = FakeWallClient({"world_of_futuristica": [], "eofru": [liked_by_admin, liked_by_stranger]})
    likes_client = FakeLikesClient({(-30, 1): {1849091, 777}, (-30, 2): {777}})
    message_client = FakeMessageClient()

    service = FutureBotService(
        settings,
        wall_client,
        message_client,
        Storage(settings.database_path),
        likes_client=likes_client,
    )
    result = service.run_once(now=datetime(2026, 6, 28, 3, 0, tzinfo=timezone.utc))

    assert result.final_posts == 1
    assert message_client.sent == [(2_000_000_170, "1. https://vk.com/wall-30_2")]
    assert sorted(likes_client.calls) == [(-30, 1), (-30, 2)]


def test_run_once_keeps_post_when_likes_get_list_fails(tmp_path):
    groups_file, terms_file = write_runtime_lists(tmp_path, ("eofru",), ("Технология",))
    settings = make_settings(tmp_path, groups_file, terms_file, admin_user_ids=(1849091,))
    post = Post(owner_id=-30, post_id=1, source_group="eofru", date=200, text="Новая технология")
    wall_client = FakeWallClient({"world_of_futuristica": [], "eofru": [post]})
    likes_client = FakeLikesClient(failing_posts=[(-30, 1)])
    message_client = FakeMessageClient()

    service = FutureBotService(
        settings,
        wall_client,
        message_client,
        Storage(settings.database_path),
        likes_client=likes_client,
    )
    result = service.run_once(now=datetime(2026, 6, 28, 3, 0, tzinfo=timezone.utc))

    assert result.final_posts == 1
    assert message_client.sent == [(2_000_000_170, "1. https://vk.com/wall-30_1")]


def test_run_once_reports_post_only_once_for_several_command_lines(tmp_path):
    groups_file, terms_file = write_runtime_lists(
        tmp_path,
        ("eofru",),
        ("Технология", "Прогноз"),
    )
    settings = make_settings(tmp_path, groups_file, terms_file, admin_user_ids=())
    post = Post(
        owner_id=-30,
        post_id=1,
        source_group="eofru",
        date=200,
        text="Технология и прогноз в одном посте",
    )
    wall_client = FakeWallClient({"world_of_futuristica": [], "eofru": [post]})
    message_client = FakeMessageClient()

    service = FutureBotService(settings, wall_client, message_client, Storage(settings.database_path))
    service.run_once(now=datetime(2026, 6, 28, 3, 0, tzinfo=timezone.utc))

    messages = [message for _, message in message_client.sent]
    assert messages[0] == "1. https://vk.com/wall-30_1"
    assert "не найдено" in messages[1]
    assert [post.source_url for post in Storage(settings.database_path).list_new_posts()] == [
        "https://vk.com/wall-30_1",
    ]


def test_run_once_skips_post_already_stored_in_new_posts_database(tmp_path):
    groups_file, terms_file = write_runtime_lists(tmp_path, ("eofru",), ("Технология",))
    settings = make_settings(tmp_path, groups_file, terms_file, admin_user_ids=())
    known_post = Post(
        owner_id=-30,
        post_id=1,
        source_group="eofru",
        date=int(datetime(2026, 6, 27, 3, 0, tzinfo=timezone.utc).timestamp()),
        text="Новая технология",
    )
    fresh_post = Post(
        owner_id=-30,
        post_id=2,
        source_group="eofru",
        date=int(datetime(2026, 6, 28, 1, 0, tzinfo=timezone.utc).timestamp()),
        text="Свежая технология",
    )
    storage = Storage(settings.database_path)
    storage.add_new_posts([known_post])
    wall_client = FakeWallClient({"world_of_futuristica": [], "eofru": [known_post, fresh_post]})
    message_client = FakeMessageClient()

    service = FutureBotService(settings, wall_client, message_client, storage)
    result = service.run_once(now=datetime(2026, 6, 28, 3, 0, tzinfo=timezone.utc))

    assert result.final_posts == 1
    assert message_client.sent == [(2_000_000_170, "1. https://vk.com/wall-30_2")]
    assert sorted(post.source_url for post in storage.list_new_posts()) == [
        "https://vk.com/wall-30_1",
        "https://vk.com/wall-30_2",
    ]
