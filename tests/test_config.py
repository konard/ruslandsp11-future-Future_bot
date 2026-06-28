from datetime import time

import pytest

from future_bot.config import ConfigError, Settings, normalize_group_identifier, parse_hhmm


def test_normalize_group_identifier_accepts_vk_urls_and_plain_slugs():
    assert normalize_group_identifier("https://vk.ru/world_of_futuristica") == "world_of_futuristica"
    assert normalize_group_identifier("@eofru") == "eofru"


def test_parse_hhmm():
    assert parse_hhmm("03:00") == time(3, 0)

    with pytest.raises(ConfigError):
        parse_hhmm("3am")


def test_settings_from_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("VK_GROUP_TOKEN", raising=False)
    monkeypatch.delenv("VK_USER_TOKEN", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "VK_GROUP_TOKEN=group",
                "VK_USER_TOKEN=user",
                "FFBOT_SOURCE_GROUPS=https://vk.ru/eofru,@asimovonline",
                "FFBOT_SCHEDULE_TIME=03:00",
                "FFBOT_TIMEZONE=UTC",
            ]
        ),
        encoding="utf-8",
    )

    settings = Settings.from_env(env_file)

    assert settings.vk_group_token == "group"
    assert settings.vk_user_token == "user"
    assert settings.vk_message_token == "user"
    assert settings.source_groups == ("eofru", "asimovonline")
    assert settings.schedule_time == time(3, 0)
    assert settings.timezone == "UTC"
