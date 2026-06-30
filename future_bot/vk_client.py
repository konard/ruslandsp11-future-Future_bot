from __future__ import annotations

import logging
import time
import json
from collections.abc import Iterable, Mapping
from json import JSONDecodeError
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from future_bot.logic import IncomingMessage, Post

LOGGER = logging.getLogger(__name__)


class VKAPIError(RuntimeError):
    pass


class VKClient:
    def __init__(
        self,
        token: str,
        api_version: str = "5.199",
        api_url: str = "https://api.vk.com/method",
        session: Any | None = None,
    ) -> None:
        self.token = token
        self.api_version = api_version
        self.api_url = api_url.rstrip("/")
        self.session = session or build_opener()

    def request(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        payload = dict(params or {})
        payload["access_token"] = self.token
        payload["v"] = self.api_version

        request = Request(
            f"{self.api_url}/{method}",
            data=urlencode(payload, doseq=True).encode("utf-8"),
            method="POST",
        )
        try:
            with self.session.open(request, timeout=30) as response:
                data = response.read()
        except HTTPError as exc:
            raise VKAPIError(f"HTTP-ошибка VK API {method}: {exc.code}") from exc
        except URLError as exc:
            raise VKAPIError(f"Сетевая ошибка VK API {method}: {exc.reason}") from exc

        try:
            decoded = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, JSONDecodeError) as exc:
            raise VKAPIError(f"VK API {method} вернул некорректный JSON") from exc

        data = decoded
        if "error" in data:
            error = data["error"]
            raise VKAPIError(
                f"Ошибка VK API {method}: {error.get('error_code')} {error.get('error_msg')}"
            )
        return data.get("response")

    def iter_wall_posts(self, group: str, since_timestamp: int | None = None) -> Iterable[Post]:
        offset = 0
        count = 100

        while True:
            response = self.request(
                "wall.get",
                {
                    "domain": group,
                    "count": count,
                    "offset": offset,
                    "filter": "owner",
                },
            )
            items = response.get("items", []) if isinstance(response, Mapping) else []
            if not items:
                break

            page_recent_count = 0
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                date = int(item.get("date", 0))
                if since_timestamp is None or date >= since_timestamp:
                    page_recent_count += 1
                    try:
                        yield Post.from_vk_item(item, group)
                    except KeyError:
                        LOGGER.warning("Пропущен пост VK без owner_id/id: %s", item)

            offset += len(items)
            if len(items) < count:
                break
            if since_timestamp is not None and page_recent_count == 0:
                break

    def send_message(self, peer_id: int, message: str) -> Any:
        return self.request(
            "messages.send",
            {
                "peer_id": peer_id,
                "message": message,
                "random_id": int(time.time() * 1000),
            },
        )

    def edit_message(self, peer_id: int, message_id: int, message: str) -> Any:
        return self.request(
            "messages.edit",
            {
                "peer_id": peer_id,
                "message_id": message_id,
                "message": message,
            },
        )

    def get_conversations(self, count: int = 200) -> list[Mapping[str, Any]]:
        response = self.request("messages.getConversations", {"count": count})
        items = response.get("items", []) if isinstance(response, Mapping) else []
        return [item for item in items if isinstance(item, Mapping)]

    def find_conversation_peer_id(self, title: str) -> int | None:
        normalized_title = title.casefold()
        for item in self.get_conversations():
            conversation = item.get("conversation")
            if not isinstance(conversation, Mapping):
                continue

            chat_settings = conversation.get("chat_settings")
            if not isinstance(chat_settings, Mapping):
                continue

            if str(chat_settings.get("title") or "").casefold() != normalized_title:
                continue

            peer = conversation.get("peer")
            if isinstance(peer, Mapping) and peer.get("id") is not None:
                return int(peer["id"])

        return None

    def iter_recent_messages(self, peer_id: int, count: int = 50) -> Iterable[IncomingMessage]:
        response = self.request("messages.getHistory", {"peer_id": peer_id, "count": count})
        items = response.get("items", []) if isinstance(response, Mapping) else []
        for item in items:
            if not isinstance(item, Mapping):
                continue
            yield IncomingMessage(
                peer_id=int(item.get("peer_id") or peer_id),
                from_id=int(item.get("from_id") or 0),
                text=str(item.get("text") or ""),
                date=int(item.get("date") or 0),
                message_id=_optional_int(item.get("id")),
                conversation_message_id=_optional_int(item.get("conversation_message_id")),
            )


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)
