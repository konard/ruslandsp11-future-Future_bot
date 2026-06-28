from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from future_bot.logic import Post, normalize_url

FF_POSTS_TABLE = '"База постов ФФ"'
NEW_POSTS_TABLE = '"База новых постов"'
METADATA_TABLE = "bot_metadata"


class Storage:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {FF_POSTS_TABLE} (
                    post_key TEXT PRIMARY KEY,
                    owner_id INTEGER NOT NULL,
                    post_id INTEGER NOT NULL,
                    source_group TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    date INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    links_json TEXT NOT NULL,
                    fetched_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {NEW_POSTS_TABLE} (
                    post_key TEXT PRIMARY KEY,
                    owner_id INTEGER NOT NULL,
                    post_id INTEGER NOT NULL,
                    source_group TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    date INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    links_json TEXT NOT NULL,
                    fetched_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {METADATA_TABLE} (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )

    def has_ff_posts(self) -> bool:
        with self._connect() as connection:
            row = connection.execute(f"SELECT COUNT(*) FROM {FF_POSTS_TABLE}").fetchone()
        return bool(row[0])

    def upsert_ff_posts(self, posts: Iterable[Post]) -> int:
        post_list = list(posts)
        with self._connect() as connection:
            self._upsert_posts(connection, FF_POSTS_TABLE, post_list)
        return len(post_list)

    def replace_new_posts(self, posts: Iterable[Post]) -> int:
        post_list = list(posts)
        with self._connect() as connection:
            connection.execute(f"DELETE FROM {NEW_POSTS_TABLE}")
            self._upsert_posts(connection, NEW_POSTS_TABLE, post_list)
        return len(post_list)

    def get_ff_links(self) -> set[str]:
        links: set[str] = set()
        with self._connect() as connection:
            rows = connection.execute(f"SELECT links_json FROM {FF_POSTS_TABLE}").fetchall()

        for row in rows:
            for link in json.loads(row["links_json"]):
                normalized = normalize_url(link)
                if normalized:
                    links.add(normalized)
        return links

    def list_new_posts(self) -> list[Post]:
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT owner_id, post_id, source_group, source_url, date, text, links_json
                FROM {NEW_POSTS_TABLE}
                ORDER BY date DESC, post_key ASC
                """
            ).fetchall()

        return [
            Post(
                owner_id=row["owner_id"],
                post_id=row["post_id"],
                source_group=row["source_group"],
                date=row["date"],
                text=row["text"],
                source_url=row["source_url"],
                links=json.loads(row["links_json"]),
            )
            for row in rows
        ]

    def set_metadata(self, key: str, value: str) -> None:
        with self._connect() as connection:
            connection.execute(
                f"""
                INSERT INTO {METADATA_TABLE} (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def get_metadata(self, key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT value FROM {METADATA_TABLE} WHERE key = ?",
                (key,),
            ).fetchone()
        return None if row is None else str(row["value"])

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _upsert_posts(self, connection: sqlite3.Connection, table: str, posts: list[Post]) -> None:
        fetched_at = datetime.now(timezone.utc).isoformat()
        connection.executemany(
            f"""
            INSERT INTO {table} (
                post_key,
                owner_id,
                post_id,
                source_group,
                source_url,
                date,
                text,
                links_json,
                fetched_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(post_key) DO UPDATE SET
                source_group = excluded.source_group,
                source_url = excluded.source_url,
                date = excluded.date,
                text = excluded.text,
                links_json = excluded.links_json,
                fetched_at = excluded.fetched_at
            """,
            [
                (
                    post.post_key,
                    post.owner_id,
                    post.post_id,
                    post.source_group,
                    post.source_url,
                    post.date,
                    post.text,
                    json.dumps(list(post.links), ensure_ascii=False),
                    fetched_at,
                )
                for post in posts
            ],
        )
