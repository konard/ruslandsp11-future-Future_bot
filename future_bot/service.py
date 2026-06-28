from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from future_bot.config import Settings
from future_bot.logic import (
    Post,
    dedupe_posts,
    filter_posts_by_terms,
    format_numbered_links,
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


@dataclass(frozen=True)
class SyncResult:
    ff_full_import: bool
    ff_posts_seen: int
    source_posts_seen: int
    filtered_posts: int
    final_posts: int
    message: str


class FutureBotService:
    def __init__(
        self,
        settings: Settings,
        wall_client: WallClient,
        message_client: MessageClient,
        storage: Storage,
    ) -> None:
        self.settings = settings
        self.wall_client = wall_client
        self.message_client = message_client
        self.storage = storage

    def run_once(self, now: datetime | None = None) -> SyncResult:
        current_time = now or datetime.now(ZoneInfo(self.settings.timezone))
        since_timestamp = int((current_time - timedelta(days=1)).timestamp())

        ff_full_import = not self.storage.has_ff_posts()
        ff_since = None if ff_full_import else since_timestamp
        ff_posts = list(self.wall_client.iter_wall_posts(self.settings.ff_group, ff_since))
        self.storage.upsert_ff_posts(ff_posts)
        LOGGER.info("Stored %s Formula Futurology posts", len(ff_posts))

        source_posts: list[Post] = []
        for group in self.settings.source_groups:
            group_posts = list(self.wall_client.iter_wall_posts(group, since_timestamp))
            LOGGER.info("Fetched %s posts from %s", len(group_posts), group)
            source_posts.extend(group_posts)

        unique_source_posts = dedupe_posts(source_posts)
        filtered_posts = filter_posts_by_terms(
            unique_source_posts,
            keywords=self.settings.keywords,
            hashtags=self.settings.hashtags,
        )
        final_posts = remove_posts_linked_from_ff(filtered_posts, self.storage.get_ff_links())
        final_posts = sorted(final_posts, key=lambda post: post.date, reverse=True)
        self.storage.replace_new_posts(final_posts)

        message = format_numbered_links(final_posts)
        self.message_client.send_message(self.settings.target_peer_id, message)
        self.storage.set_metadata("last_successful_sync_at", current_time.isoformat())

        return SyncResult(
            ff_full_import=ff_full_import,
            ff_posts_seen=len(ff_posts),
            source_posts_seen=len(unique_source_posts),
            filtered_posts=len(filtered_posts),
            final_posts=len(final_posts),
            message=message,
        )
