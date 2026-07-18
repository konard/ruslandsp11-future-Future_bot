from __future__ import annotations

import logging
import os
import ssl
import time
import json
from collections.abc import Iterable, Mapping
from json import JSONDecodeError
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPSHandler, Request, build_opener

from future_bot.logic import IncomingMessage, Post

LOGGER = logging.getLogger(__name__)
VK_FLOOD_CONTROL_ERROR_CODE = 9
VK_FLOOD_CONTROL_SLEEP_SECONDS = 5 * 60
NETWORK_RETRY_ATTEMPTS = 3
NETWORK_RETRY_SLEEP_SECONDS = 5


def find_ca_bundle(ca_bundle: str | Path | None = None) -> str | None:
    """Находит файл доверенных корневых сертификатов для HTTPS-запросов к VK.

    Порядок поиска: явно заданный путь (``FFBOT_VK_CA_BUNDLE``), затем
    стандартные переменные ``SSL_CERT_FILE`` / ``REQUESTS_CA_BUNDLE``, затем
    пакет ``certifi``, если он установлен. Если ничего не найдено,
    возвращается ``None`` и используется системное хранилище сертификатов.

    Именно отсутствие доступного хранилища вызывает ошибку
    ``[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate``
    на Windows, где системное хранилище может не содержать промежуточных
    сертификатов VK.
    """

    candidates = [ca_bundle, os.environ.get("SSL_CERT_FILE"), os.environ.get("REQUESTS_CA_BUNDLE")]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
        if candidate:
            LOGGER.warning("Файл сертификатов не найден: %s", candidate)

    try:
        import certifi
    except ImportError:
        return None

    bundle = certifi.where()
    return bundle if Path(bundle).is_file() else None


def build_https_opener(ca_bundle: str | Path | None = None, verify: bool = True) -> Any:
    """Создает opener с явно настроенной проверкой TLS-сертификатов."""

    if not verify:
        LOGGER.warning("Проверка TLS-сертификатов VK API отключена настройкой FFBOT_VK_SSL_VERIFY")
        context = ssl._create_unverified_context()
        return build_opener(HTTPSHandler(context=context))

    bundle = find_ca_bundle(ca_bundle)
    context = ssl.create_default_context(cafile=bundle)
    if bundle:
        LOGGER.debug("Используется файл сертификатов %s", bundle)
    return build_opener(HTTPSHandler(context=context))


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
        ca_bundle: str | Path | None = None,
        verify_ssl: bool = True,
        network_retry_attempts: int = NETWORK_RETRY_ATTEMPTS,
        network_retry_sleep_seconds: float = NETWORK_RETRY_SLEEP_SECONDS,
    ) -> None:
        self.token = token
        self.api_version = api_version
        self.api_url = api_url.rstrip("/")
        self.session = session or build_https_opener(ca_bundle, verify_ssl)
        self.sleeper = sleeper or time.sleep
        self.flood_control_sleep_seconds = flood_control_sleep_seconds
        self.network_retry_attempts = max(1, network_retry_attempts)
        self.network_retry_sleep_seconds = network_retry_sleep_seconds

    def request(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        try:
            return self._request_with_network_retries(method, params)
        except VKAPIError as exc:
            if exc.error_code != VK_FLOOD_CONTROL_ERROR_CODE:
                raise

            LOGGER.warning(
                "VK API %s вернул Flood control, пауза на %s секунд",
                method,
                self.flood_control_sleep_seconds,
            )
            self.sleeper(self.flood_control_sleep_seconds)
            return self._request_with_network_retries(method, params)

    def _request_with_network_retries(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        """Повторяет запрос при временных сетевых сбоях.

        Обрыв соединения или недоступность DNS не должны останавливать опрос
        чата, поэтому такие ошибки повторяются несколько раз с паузой. Ошибки
        самого VK API (в том числе Flood control) сюда не попадают.
        """

        last_error: VKAPIError | None = None
        for attempt in range(1, self.network_retry_attempts + 1):
            try:
                return self._request_once(method, params)
            except VKAPIError as exc:
                cause = exc.__cause__
                if not isinstance(cause, URLError) or isinstance(cause, HTTPError):
                    raise
                last_error = exc
                if attempt < self.network_retry_attempts:
                    LOGGER.warning(
                        "Сетевая ошибка VK API %s (попытка %s из %s): %s",
                        method,
                        attempt,
                        self.network_retry_attempts,
                        exc,
                    )
                    self.sleeper(self.network_retry_sleep_seconds)

        assert last_error is not None
        raise last_error

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
            raise VKAPIError(
                f"Сетевая ошибка VK API {method}: {exc.reason}{_ssl_hint(exc)}",
                method=method,
            ) from exc

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

    def get_post_likers(self, owner_id: int, post_id: int, count: int = 1000) -> set[int]:
        """Возвращает идентификаторы пользователей, лайкнувших пост.

        Сервисный токен приложения не может вызывать ``likes.isLiked`` (метод
        требует токен пользователя), но ``likes.getList`` ему доступен, поэтому
        принадлежность лайка определяется по списку лайкнувших.
        """

        response = self.request(
            "likes.getList",
            {
                "type": "post",
                "owner_id": owner_id,
                "item_id": post_id,
                "filter": "likes",
                "count": count,
                "skip_own": 0,
            },
        )
        items = response.get("items", []) if isinstance(response, Mapping) else []
        likers: set[int] = set()
        for item in items:
            if isinstance(item, Mapping):
                item = item.get("id")
            try:
                likers.add(int(item))
            except (TypeError, ValueError):
                LOGGER.warning("Пропущен некорректный идентификатор лайка: %s", item)
        return likers

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


def _ssl_hint(exc: URLError) -> str:
    if not isinstance(exc.reason, ssl.SSLError):
        return ""
    return (
        ". Проверьте хранилище TLS-сертификатов: установите пакет certifi "
        "(python -m pip install certifi) или укажите путь к файлу сертификатов "
        "в FFBOT_VK_CA_BUNDLE"
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
