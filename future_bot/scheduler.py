from __future__ import annotations

import logging
import time as time_module
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
    while True:
        delay = seconds_until_next_run(settings.schedule_time, settings.timezone)
        LOGGER.info(
            "Next sync is scheduled in %.0f seconds at %s %s",
            delay,
            settings.schedule_time.strftime("%H:%M"),
            settings.timezone,
        )
        time_module.sleep(delay)
        try:
            service.run_once()
        except Exception:
            LOGGER.exception("Scheduled sync failed")
