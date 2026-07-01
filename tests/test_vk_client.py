from future_bot.vk_client import VKClient


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
