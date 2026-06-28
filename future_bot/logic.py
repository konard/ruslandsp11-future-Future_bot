from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse, urlunparse

HTTP_URL_RE = re.compile(r"https?://[^\s<>()\"']+", re.IGNORECASE)
BARE_VK_URL_RE = re.compile(
    r"(?<![\w./:-])(?:www\.)?(?:m\.)?vk\.(?:com|ru)/[^\s<>()\"']+",
    re.IGNORECASE,
)
HASHTAG_RE = re.compile(r"(?<!\w)#([\wа-яА-ЯёЁ]+)", re.UNICODE)
TRAILING_PUNCTUATION = ".,;:!?)]}\"'"
LEADING_PUNCTUATION = "([{\"'"
VK_HOSTS = {"vk.com", "vk.ru", "m.vk.com", "m.vk.ru", "www.vk.com", "www.vk.ru"}


@dataclass(frozen=True)
class Post:
    owner_id: int
    post_id: int
    source_group: str
    date: int
    text: str
    source_url: str | None = None
    links: Sequence[str] = field(default_factory=tuple)
    raw: Mapping[str, Any] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        source_url = normalize_url(self.source_url or post_url(self.owner_id, self.post_id))
        normalized_links = tuple(
            sorted({normalized for link in self.links if (normalized := normalize_url(link))})
        )
        object.__setattr__(self, "source_url", source_url)
        object.__setattr__(self, "links", normalized_links)

    @property
    def post_key(self) -> str:
        return f"{self.owner_id}_{self.post_id}"

    @classmethod
    def from_vk_item(cls, item: Mapping[str, Any], source_group: str) -> "Post":
        owner_id = int(item["owner_id"])
        post_id = int(item["id"])
        return cls(
            owner_id=owner_id,
            post_id=post_id,
            source_group=source_group,
            date=int(item.get("date", 0)),
            text=str(item.get("text") or ""),
            source_url=post_url(owner_id, post_id),
            links=links_from_vk_item(item),
            raw=item,
        )


def post_url(owner_id: int, post_id: int) -> str:
    return f"https://vk.com/wall{owner_id}_{post_id}"


def normalize_url(url: str | None) -> str:
    if not url:
        return ""

    cleaned = str(url).strip().strip(LEADING_PUNCTUATION).strip(TRAILING_PUNCTUATION)
    if not cleaned:
        return ""

    if cleaned.lower().startswith(("vk.com/", "vk.ru/", "m.vk.com/", "m.vk.ru/", "www.vk.")):
        cleaned = f"https://{cleaned}"

    parsed = urlparse(cleaned)
    if not parsed.scheme or not parsed.netloc:
        return ""

    host = parsed.netloc.lower()
    if host in VK_HOSTS:
        host = "vk.com"
        path = parsed.path.rstrip("/").casefold()
    else:
        path = parsed.path.rstrip("/")

    return urlunparse(("https", host, path or "/", "", "", ""))


def extract_links(text: str, attachments: Sequence[Mapping[str, Any]] | None = None) -> list[str]:
    links: set[str] = set()
    for match in HTTP_URL_RE.finditer(text or ""):
        normalized = normalize_url(match.group(0))
        if normalized:
            links.add(normalized)

    for match in BARE_VK_URL_RE.finditer(text or ""):
        normalized = normalize_url(match.group(0))
        if normalized:
            links.add(normalized)

    for url in _iter_attachment_urls(attachments or ()):
        normalized = normalize_url(url)
        if normalized:
            links.add(normalized)

    return sorted(links)


def links_from_vk_item(item: Mapping[str, Any]) -> list[str]:
    links = set(extract_links(str(item.get("text") or ""), item.get("attachments") or ()))

    for original in item.get("copy_history") or ():
        if not isinstance(original, Mapping):
            continue
        owner_id = original.get("owner_id")
        post_id = original.get("id")
        if owner_id is not None and post_id is not None:
            links.add(post_url(int(owner_id), int(post_id)))
        links.update(links_from_vk_item(original))

    return sorted({normalized for link in links if (normalized := normalize_url(link))})


def filter_posts_by_terms(
    posts: Iterable[Post],
    keywords: Sequence[str],
    hashtags: Sequence[str],
) -> list[Post]:
    normalized_keywords = tuple(keyword.casefold() for keyword in keywords if keyword.strip())
    normalized_hashtags = tuple(_normalize_hashtag(hashtag) for hashtag in hashtags if hashtag.strip())

    filtered: list[Post] = []
    for post in posts:
        text = post.text.casefold()
        post_hashtags = {_normalize_hashtag(tag) for tag in HASHTAG_RE.findall(post.text)}
        if any(keyword in text for keyword in normalized_keywords) or any(
            hashtag in post_hashtags for hashtag in normalized_hashtags
        ):
            filtered.append(post)

    return filtered


def remove_posts_linked_from_ff(posts: Iterable[Post], ff_links: Iterable[str]) -> list[Post]:
    normalized_ff_links = {normalized for link in ff_links if (normalized := normalize_url(link))}
    remaining: list[Post] = []

    for post in posts:
        post_links = {normalize_url(post.source_url), *(normalize_url(link) for link in post.links)}
        post_links.discard("")
        if post_links.isdisjoint(normalized_ff_links):
            remaining.append(post)

    return remaining


def dedupe_posts(posts: Iterable[Post]) -> list[Post]:
    seen: set[str] = set()
    unique: list[Post] = []
    for post in posts:
        key = normalize_url(post.source_url) or post.post_key
        if key in seen:
            continue
        seen.add(key)
        unique.append(post)
    return unique


def format_numbered_links(posts: Sequence[Post]) -> str:
    if not posts:
        return "За последние сутки новых постов по заданным критериям не найдено."
    return "\n".join(f"{index}. {post.source_url}" for index, post in enumerate(posts, start=1))


def _iter_attachment_urls(attachments: Sequence[Mapping[str, Any]]) -> Iterable[str]:
    for attachment in attachments:
        if not isinstance(attachment, Mapping):
            continue

        link = attachment.get("link")
        if isinstance(link, Mapping) and link.get("url"):
            yield str(link["url"])

        wall = attachment.get("wall")
        if isinstance(wall, Mapping) and wall.get("owner_id") is not None and wall.get("id") is not None:
            yield post_url(int(wall["owner_id"]), int(wall["id"]))


def _normalize_hashtag(value: str) -> str:
    return value.strip().lstrip("#").casefold()
