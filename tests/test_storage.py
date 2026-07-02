from future_bot.logic import Post
from future_bot.storage import Storage


def test_storage_keeps_ff_links_and_replaces_new_posts(tmp_path):
    storage = Storage(tmp_path / "future_bot.sqlite3")
    storage.upsert_ff_posts(
        [
            Post(
                owner_id=-1,
                post_id=1,
                source_group="world_of_futuristica",
                date=1,
                text="ФФ",
                links=("https://vk.ru/wall-2_2?utm_source=feed",),
            )
        ]
    )

    assert storage.has_ff_posts() is True
    assert storage.get_ff_links() == {"https://vk.com/wall-2_2"}
    assert storage.get_latest_ff_post_date() == 1

    storage.replace_new_posts(
        [
            Post(
                owner_id=-3,
                post_id=3,
                source_group="eofru",
                date=3,
                text="Новая технология",
            )
        ]
    )
    storage.replace_new_posts([])

    assert storage.list_new_posts() == []


def test_delete_posts_older_than_removes_only_stale_ff_posts(tmp_path):
    storage = Storage(tmp_path / "future_bot.sqlite3")
    storage.upsert_ff_posts(
        [
            Post(owner_id=-1, post_id=1, source_group="ff", date=100, text="старый"),
            Post(owner_id=-1, post_id=2, source_group="ff", date=500, text="свежий"),
        ]
    )

    removed = storage.delete_posts_older_than(200)

    assert removed == 1
    assert storage.get_latest_ff_post_date() == 500
    assert storage.get_ff_links() == set()
