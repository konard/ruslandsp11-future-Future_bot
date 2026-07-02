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
SEARCH_COMMAND_RE = re.compile(
    r"^\s*/поиск\s+по\s*\((?P<keywords>[^)]*)\)\s+интервал\s+(?P<days>\d+)\s*д\b",
    re.IGNORECASE,
)


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


@dataclass(frozen=True)
class SearchCommand:
    """Разобранная команда поиска.

    ``groups`` описывает поисковый запрос как логическое ИЛИ групп: каждая
    группа - это набор слов, соединенных логическим И (все слова должны
    присутствовать в одном посте). Например ``(ИИ+прогноз, антилопы)``
    превращается в ``((\"ИИ\", \"прогноз\"), (\"антилопы\",))``.
    """

    groups: tuple[tuple[str, ...], ...]
    interval_days: int

    @property
    def keywords(self) -> tuple[str, ...]:
        return flatten_terms(self.groups)

    @property
    def hashtags(self) -> tuple[str, ...]:
        return tuple(f"#{term.lstrip('#')}" for term in self.keywords if term.lstrip("#"))


@dataclass(frozen=True)
class ControlCommand:
    action: str


@dataclass(frozen=True)
class IncomingMessage:
    peer_id: int
    from_id: int
    text: str
    date: int
    message_id: int | None = None
    conversation_message_id: int | None = None

    @property
    def sequence_id(self) -> int:
        return self.conversation_message_id or self.message_id or self.date


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


def parse_query_groups(text: str) -> tuple[tuple[str, ...], ...]:
    """Разбирает выражение в скобках в группы слов.

    Запятая означает логическое ИЛИ между группами, ``+`` - логическое И
    внутри группы. Пустые слова и группы отбрасываются.
    """

    groups: list[tuple[str, ...]] = []
    for part in (text or "").split(","):
        terms = tuple(term.strip() for term in part.split("+") if term.strip())
        if terms:
            groups.append(terms)
    return tuple(groups)


def flatten_terms(groups: Sequence[Sequence[str]]) -> tuple[str, ...]:
    """Возвращает уникальные слова из всех групп, сохраняя порядок."""

    seen: set[str] = set()
    result: list[str] = []
    for group in groups:
        for term in group:
            key = term.casefold()
            if term and key not in seen:
                seen.add(key)
                result.append(term)
    return tuple(result)


def format_groups(groups: Sequence[Sequence[str]]) -> str:
    """Человекочитаемое представление запроса, например ``ИИ+прогноз, антилопы``."""

    return ", ".join("+".join(group) for group in groups if group)


def parse_search_command(text: str) -> SearchCommand | None:
    match = SEARCH_COMMAND_RE.search(text or "")
    if not match:
        return None

    groups = parse_query_groups(match.group("keywords"))
    interval_days = int(match.group("days"))
    if interval_days <= 0:
        return None

    return SearchCommand(groups=groups, interval_days=interval_days)


def parse_control_command(text: str) -> ControlCommand | None:
    normalized = " ".join((text or "").strip().casefold().split())
    normalized = normalized.rstrip(".,;:!?…").strip()
    if normalized in {
        "/стоп программа",
        "/стоп программу",
        "/стоп бот",
        "/стоп программы",
        "/выход",
        "/shutdown",
    }:
        return ControlCommand(action="shutdown")
    if normalized in {
        "/стоп",
        "/стоп поиск",
        "/стоп поиска",
        "/стоп поиски",
        "/стоп-поиск",
        "/stop",
        "/stop search",
    }:
        return ControlCommand(action="stop_search")
    return None


def _compile_term(term: str) -> "re.Pattern[str]":
    """Компилирует слово в шаблон, требующий обособленного совпадения.

    Слово должно быть отдельным словом (или хэштегом), а не частью другого
    слова: ``Маск`` не совпадет с ``маскировка``, а ``ИИ`` не совпадет внутри
    другого слова. Границы определяются символами ``\\w`` в Unicode, поэтому
    правило работает и для кириллицы.
    """

    body = re.escape(term.strip().lstrip("#"))
    return re.compile(rf"(?<!\w){body}(?!\w)", re.IGNORECASE)


def filter_posts_by_groups(
    posts: Iterable[Post],
    groups: Sequence[Sequence[str]],
) -> list[Post]:
    """Отбирает посты по запросу вида ИЛИ(групп) из И(слов).

    Пост проходит фильтр, если хотя бы одна группа совпала полностью, то есть
    все слова группы присутствуют в тексте поста как обособленные слова.
    """

    compiled_groups: list[list[re.Pattern[str]]] = []
    for group in groups:
        patterns = [_compile_term(term) for term in group if term.strip()]
        if patterns:
            compiled_groups.append(patterns)

    if not compiled_groups:
        return []

    filtered: list[Post] = []
    for post in posts:
        text = post.text
        if any(all(pattern.search(text) for pattern in group) for group in compiled_groups):
            filtered.append(post)

    return filtered


def filter_posts_by_terms(
    posts: Iterable[Post],
    keywords: Sequence[str],
    hashtags: Sequence[str],
) -> list[Post]:
    """Совместимая обертка: каждое слово и хэштег - отдельная группа (ИЛИ)."""

    groups: list[tuple[str, ...]] = [(keyword,) for keyword in keywords if keyword.strip()]
    groups.extend((hashtag,) for hashtag in hashtags if hashtag.strip())
    return filter_posts_by_groups(posts, groups)


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


def format_numbered_links(
    posts: Sequence[Post],
    empty_message: str = "За последние сутки новых постов по заданным критериям не найдено.",
) -> str:
    if not posts:
        return empty_message
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
