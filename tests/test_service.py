from datetime import datetime, timezone

from future_bot.config import Settings
from future_bot.logic import Post
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
