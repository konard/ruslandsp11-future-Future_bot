from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Mapping
from typing import Any

import requests

from future_bot.logic import Post

LOGGER = logging.getLogger(__name__)


class VKAPIError(RuntimeError):
    pass


class VKClient:
    def __init__(
        self,
        token: str,
        api_version: str = "5.199",
        api_url: str = "https://api.vk.com/method",
        session: requests.Session | None = None,
    ) -> None:
        self.token = token
        self.api_version = api_version
        self.api_url = api_url.rstrip("/")
        self.session = session or requests.Session()

    def request(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        payload = dict(params or {})
        payload["access_token"] = self.token
        payload["v"] = self.api_version

        response = self.session.get(f"{self.api_url}/{method}", params=payload, timeout=30)
        response.raise_for_status()
        data = response.json()
        if "error" in data:
            error = data["error"]
            raise VKAPIError(
                f"VK API {method} failed: {error.get('error_code')} {error.get('error_msg')}"
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
                        LOGGER.warning("Skipping VK post without owner_id/id: %s", item)

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
