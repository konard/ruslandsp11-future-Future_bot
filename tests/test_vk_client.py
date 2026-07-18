import ssl
from urllib.error import HTTPError, URLError

import pytest

from future_bot.vk_client import VKAPIError, VKClient, find_ca_bundle


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.payload


class FakeSession:
    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.requests = []

    def open(self, request, timeout=30):
        self.requests.append((request, timeout))
        return FakeResponse(self.payloads.pop(0))


def test_vk_client_sleeps_and_retries_once_on_flood_control_error():
    session = FakeSession(
        b'{"error": {"error_code": 9, "error_msg": "Flood control"}}',
        b'{"response": 123}',
    )
    sleeps = []
    client = VKClient("token", session=session, sleeper=sleeps.append)

    result = client.send_message(2_000_000_015, "hello")

    assert result == 123
    assert sleeps == [300]
    assert len(session.requests) == 2


class FailingSession(FakeSession):
    def __init__(self, errors, *payloads):
        super().__init__(*payloads)
        self.errors = list(errors)

    def open(self, request, timeout=30):
        self.requests.append((request, timeout))
        if self.errors:
            raise self.errors.pop(0)
        return FakeResponse(self.payloads.pop(0))


def test_get_post_likers_uses_likes_get_list_and_returns_user_ids():
    session = FakeSession(b'{"response": {"count": 2, "items": [1849091, 777]}}')
    client = VKClient("service-token", session=session)

    assert client.get_post_likers(-30, 5) == {1849091, 777}

    request, _ = session.requests[0]
    assert request.full_url.endswith("/likes.getList")
    body = request.data.decode("utf-8")
    assert "type=post" in body
    assert "owner_id=-30" in body
    assert "item_id=5" in body


def test_request_retries_network_errors_and_then_succeeds():
    session = FailingSession(
        [URLError(ssl.SSLCertVerificationError("certificate verify failed"))],
        b'{"response": {"items": []}}',
    )
    sleeps = []
    client = VKClient("token", session=session, sleeper=sleeps.append)

    assert client.request("messages.getHistory", {"peer_id": 1}) == {"items": []}
    assert len(session.requests) == 2
    assert sleeps == [5]


def test_request_reports_certificate_hint_after_all_retries_fail():
    error = URLError(ssl.SSLCertVerificationError("certificate verify failed"))
    session = FailingSession([error, error, error])
    client = VKClient("token", session=session, sleeper=lambda seconds: None)

    with pytest.raises(VKAPIError) as exc_info:
        client.request("messages.getHistory", {"peer_id": 1})

    assert "certifi" in str(exc_info.value)
    assert "FFBOT_VK_CA_BUNDLE" in str(exc_info.value)
    assert len(session.requests) == 3


def test_request_does_not_retry_http_errors():
    session = FailingSession([HTTPError("https://api.vk.com", 500, "boom", {}, None)])
    client = VKClient("token", session=session, sleeper=lambda seconds: None)

    with pytest.raises(VKAPIError, match="HTTP-ошибка"):
        client.request("wall.get", {"domain": "eofru"})

    assert len(session.requests) == 1


def test_find_ca_bundle_prefers_existing_explicit_file(tmp_path, monkeypatch):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    bundle = tmp_path / "cacert.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----", encoding="utf-8")

    assert find_ca_bundle(bundle) == str(bundle)
