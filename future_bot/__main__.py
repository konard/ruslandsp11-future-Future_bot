from __future__ import annotations

import argparse
import logging

from future_bot.config import ConfigError, Settings
from future_bot.scheduler import poll_chat_forever, run_forever
from future_bot.service import FutureBotService
from future_bot.storage import Storage


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Ищет посты VK по командам в чате.")
    parser.add_argument("--env-file", default=".env", help="Путь к файлу окружения.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Выполнить один цикл поиска и выйти.")
    mode.add_argument("--daemon", action="store_true", help="Запускать поиск каждый день в FFBOT_SCHEDULE_TIME.")
    mode.add_argument("--poll-chat", action="store_true", help="Проверять чат на команды раз в 3 секунды.")
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

    wall_client = VKClient(settings.vk_user_token, api_version=settings.vk_api_version)
    message_client = VKClient(settings.vk_message_token, api_version=settings.vk_api_version)
    storage = Storage(settings.database_path)
    service = FutureBotService(settings, wall_client, message_client, storage, chat_client=message_client)

    if args.daemon:
        run_forever(service, settings)
        return 0

    if args.poll_chat:
        poll_chat_forever(service, settings)
        return 0

    result = service.run_once()
    logging.getLogger(__name__).info(
        "Синхронизация завершена: фф=%s источники=%s отфильтровано=%s итог=%s",
        result.ff_posts_seen,
        result.source_posts_seen,
        result.filtered_posts,
        result.final_posts,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
