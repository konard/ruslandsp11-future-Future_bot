from datetime import datetime, timezone

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


class FakeMessageClient:
    def __init__(self):
        self.sent = []

    def send_message(self, peer_id, message):
        self.sent.append((peer_id, message))


class FakeChatClient:
    def __init__(self, peer_id, messages):
        self.peer_id = peer_id
        self.messages = messages
        self.requested_titles = []
        self.history_calls = []

    def find_conversation_peer_id(self, title):
        self.requested_titles.append(title)
        return self.peer_id

    def iter_recent_messages(self, peer_id, count=50):
        self.history_calls.append((peer_id, count))
        return iter(self.messages)


def test_run_once_builds_ff_database_filters_dedupes_and_sends_digest(tmp_path):
    settings = Settings(
        vk_group_token="group-token",
        vk_user_token="user-token",
        vk_message_token="user-token",
        database_path=tmp_path / "future_bot.sqlite3",
        ff_group="world_of_futuristica",
        source_groups=("eofru", "asimovonline"),
        keywords=("Технология",),
        hashtags=("#Технология",),
        target_peer_id=2_000_000_170,
        timezone="UTC",
    )
    ff_post = Post(
        owner_id=-10,
        post_id=1,
        source_group="world_of_futuristica",
        date=100,
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


def test_run_once_refreshes_ff_posts_from_latest_stored_date(tmp_path):
    settings = Settings(
        vk_group_token="group-token",
        vk_user_token="user-token",
        vk_message_token="group-token",
        database_path=tmp_path / "future_bot.sqlite3",
        ff_group="world_of_futuristica",
        source_groups=("eofru",),
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


def test_handle_allowed_chat_search_command_uses_chat_peer_and_interval(tmp_path):
    settings = Settings(
        vk_group_token="group-token",
        vk_user_token="user-token",
        vk_message_token="group-token",
        database_path=tmp_path / "future_bot.sqlite3",
        ff_group="world_of_futuristica",
        source_groups=("eofru",),
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
    assert wall_client.calls[-1] == ("eofru", expected_since)
    assert message_client.sent == [
        (
            2_000_000_015,
            "Поиск выполнен.\n"
            "Ключевые слова: Технологии, Технология.\n"
            "Интервал: 5 д.\n"
            "Постов ФФ загружено: 0.\n"
            "Постов источников проверено: 1.\n"
            "После фильтра по словам: 1.\n"
            "Итоговых ссылок: 1.\n\n"
            "1. https://vk.com/wall-20_5",
        )
    ]


def test_handle_chat_search_command_denies_unlisted_user(tmp_path):
    settings = Settings(
        vk_group_token="group-token",
        vk_user_token="user-token",
        vk_message_token="group-token",
        database_path=tmp_path / "future_bot.sqlite3",
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


def test_poll_chat_once_resolves_conversation_and_processes_new_command(tmp_path):
    settings = Settings(
        vk_group_token="group-token",
        vk_user_token="user-token",
        vk_message_token="group-token",
        database_path=tmp_path / "future_bot.sqlite3",
        ff_group="world_of_futuristica",
        source_groups=("eofru",),
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
    wall_client = FakeWallClient({"world_of_futuristica": [], "eofru": []})
    message_client = FakeMessageClient()
    service = FutureBotService(
        settings,
        wall_client,
        message_client,
        Storage(settings.database_path),
        chat_client=chat_client,
    )

    handled_count = service.poll_chat_once(now=datetime(2026, 6, 28, 3, 0, tzinfo=timezone.utc))

    assert handled_count == 1
    assert chat_client.requested_titles == ["Аналитика и прогнозы"]
    assert chat_client.history_calls == [(2_000_000_099, 50)]
    assert message_client.sent[0][0] == 2_000_000_099
    assert Storage(settings.database_path).get_metadata(
        "last_processed_message_sequence:2000000099"
    ) == "7"
