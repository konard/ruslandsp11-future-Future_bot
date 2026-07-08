from __future__ import annotations

import logging
import threading
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from future_bot.config import Settings
from future_bot.service import FutureBotService

LOGGER = logging.getLogger(__name__)


def seconds_until_next_run(
    schedule_time: time,
    timezone_name: str,
    now: datetime | None = None,
) -> float:
    zone = ZoneInfo(timezone_name)
    current = (now or datetime.now(zone)).astimezone(zone)
    next_run = current.replace(
        hour=schedule_time.hour,
        minute=schedule_time.minute,
        second=0,
        microsecond=0,
    )
    if next_run <= current:
        next_run += timedelta(days=1)
    return max(0.0, (next_run - current).total_seconds())


def run_forever(service: FutureBotService, settings: Settings) -> None:
    while not service.shutdown_requested:
        delay = seconds_until_next_run(settings.schedule_time, settings.timezone)
        LOGGER.info(
            "Следующая синхронизация запланирована через %.0f с, в %s %s",
            delay,
            settings.schedule_time.strftime("%H:%M"),
            settings.timezone,
        )
        if service.wait_for_shutdown(delay):
            break
        try:
            service.run_once(include_summary=True)
        except Exception:
            LOGGER.exception("Ошибка плановой синхронизации")


def poll_chat_forever(service: FutureBotService, settings: Settings) -> None:
    while not service.shutdown_requested:
        try:
            # background=True: команды поиска выполняются в отдельном потоке,
            # поэтому поток опроса не блокируется и сразу распознает «/стоп поиск».
            handled_count = service.poll_chat_once(background=True)
            if handled_count:
                LOGGER.info("Обработано команд поиска: %s", handled_count)
        except Exception:
            LOGGER.exception("Ошибка проверки команд в чате")

        service.wait_for_shutdown(settings.command_poll_interval_seconds)


def run_forever_with_chat_polling(service: FutureBotService, settings: Settings) -> None:
    chat_thread = threading.Thread(
        target=poll_chat_forever,
        args=(service, settings),
        name="future-bot-chat-polling",
        daemon=True,
    )
    chat_thread.start()
    run_forever(service, settings)
