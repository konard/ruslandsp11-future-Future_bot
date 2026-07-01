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
VK_FLOOD_CONTROL_ERROR_CODE = 9
VK_FLOOD_CONTROL_SLEEP_SECONDS = 5 * 60


class VKAPIError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        method: str | None = None,
        error_code: int | None = None,
        error_msg: str | None = None,
    ) -> None:
        super().__init__(message)
        self.method = method
        self.error_code = error_code
        self.error_msg = error_msg


class VKClient:
    def __init__(
        self,
        token: str,
        api_version: str = "5.199",
        api_url: str = "https://api.vk.com/method",
        session: Any | None = None,
        sleeper: Any | None = None,
        flood_control_sleep_seconds: int = VK_FLOOD_CONTROL_SLEEP_SECONDS,
    ) -> None:
        self.token = token
        self.api_version = api_version
        self.api_url = api_url.rstrip("/")
        self.session = session or build_opener()
        self.sleeper = sleeper or time.sleep
        self.flood_control_sleep_seconds = flood_control_sleep_seconds

    def request(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        try:
            return self._request_once(method, params)
        except VKAPIError as exc:
            if exc.error_code != VK_FLOOD_CONTROL_ERROR_CODE:
                raise

            LOGGER.warning(
                "VK API %s вернул Flood control, пауза на %s секунд",
                method,
                self.flood_control_sleep_seconds,
            )
            self.sleeper(self.flood_control_sleep_seconds)
            return self._request_once(method, params)

    def _request_once(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
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
            raise VKAPIError(f"HTTP-ошибка VK API {method}: {exc.code}", method=method) from exc
        except URLError as exc:
            raise VKAPIError(f"Сетевая ошибка VK API {method}: {exc.reason}", method=method) from exc

        try:
            decoded = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, JSONDecodeError) as exc:
            raise VKAPIError(f"VK API {method} вернул некорректный JSON", method=method) from exc

        data = decoded
        if "error" in data:
            error = data["error"]
            error_code = _error_code(error)
            error_msg = str(error.get("error_msg") or "") if isinstance(error, Mapping) else ""
            raise VKAPIError(
                f"Ошибка VK API {method}: {error_code} {error_msg}",
                method=method,
                error_code=error_code,
                error_msg=error_msg,
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


def _error_code(error: Any) -> int | None:
    if not isinstance(error, Mapping):
        return None
    try:
        return _optional_int(error.get("error_code"))
    except (TypeError, ValueError):
        return None
