from future_bot.config import Settings
from future_bot.scheduler import run_forever


class FakeScheduledService:
    def __init__(self):
        self.run_calls = []
        self.shutdown_requested = False

    def wait_for_shutdown(self, timeout=None):
        return self.shutdown_requested

    def run_once(self, **kwargs):
        self.run_calls.append(kwargs)
        self.shutdown_requested = True


def test_run_forever_requests_summary_report_for_timer_triggered_search():
    settings = Settings(vk_group_token="group-token", vk_user_token="user-token", vk_message_token="group-token")
    service = FakeScheduledService()

    run_forever(service, settings)

    assert service.run_calls == [{"include_summary": True}]
