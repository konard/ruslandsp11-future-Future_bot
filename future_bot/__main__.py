from __future__ import annotations

import argparse
import logging

from future_bot.config import ConfigError, Settings
from future_bot.scheduler import run_forever
from future_bot.service import FutureBotService
from future_bot.storage import Storage


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect VK technology posts and send a daily digest.")
    parser.add_argument("--env-file", default=".env", help="Path to the environment file.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Run one sync cycle and exit.")
    mode.add_argument("--daemon", action="store_true", help="Run every day at FFBOT_SCHEDULE_TIME.")
    return parser


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args()

    try:
        settings = Settings.from_env(args.env_file)
    except ConfigError as exc:
        logging.getLogger(__name__).error("%s", exc)
        return 2

    from future_bot.vk_client import VKClient

    wall_client = VKClient(settings.vk_group_token, api_version=settings.vk_api_version)
    message_client = VKClient(settings.vk_message_token, api_version=settings.vk_api_version)
    storage = Storage(settings.database_path)
    service = FutureBotService(settings, wall_client, message_client, storage)

    if args.daemon:
        run_forever(service, settings)
        return 0

    result = service.run_once()
    logging.getLogger(__name__).info(
        "Sync complete: ff=%s source=%s filtered=%s final=%s",
        result.ff_posts_seen,
        result.source_posts_seen,
        result.filtered_posts,
        result.final_posts,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
