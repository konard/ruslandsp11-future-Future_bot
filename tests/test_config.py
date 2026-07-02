from datetime import time

import pytest

from future_bot.config import (
    ConfigError,
    Settings,
    load_command_lines,
    load_source_groups_file,
    load_terms_file,
    normalize_group_identifier,
    parse_hhmm,
    parse_int_csv,
    parse_positive_int,
)


def test_load_command_lines_parses_each_line_as_separate_command(tmp_path):
    terms_file = tmp_path / "Список слов и хэштегов.txt"
    terms_file.write_text(
        "ИИ+прогноз+технологии, антилопы+экология\n\n#роботы\n",
        encoding="utf-8",
    )

    commands = load_command_lines(terms_file)

    assert [command.groups for command in commands] == [
        (("ИИ", "прогноз", "технологии"), ("антилопы", "экология")),
        (("#роботы",),),
    ]
    assert commands[0].raw == "ИИ+прогноз+технологии, антилопы+экология"


def test_load_command_lines_rejects_empty_file(tmp_path):
    terms_file = tmp_path / "Список слов и хэштегов.txt"
    terms_file.write_text("\n  \n", encoding="utf-8")

    with pytest.raises(ConfigError):
        load_command_lines(terms_file)


def test_parse_positive_int_validates_value():
    assert parse_positive_int("10", "FFBOT_POST_RETENTION_DAYS") == 10

    with pytest.raises(ConfigError):
        parse_positive_int("0", "FFBOT_POST_RETENTION_DAYS")
    with pytest.raises(ConfigError):
        parse_positive_int("abc", "FFBOT_POST_RETENTION_DAYS")


def test_normalize_group_identifier_accepts_vk_urls_and_plain_slugs():
    assert normalize_group_identifier("https://vk.ru/world_of_futuristica") == "world_of_futuristica"
    assert normalize_group_identifier("@eofru") == "eofru"


def test_parse_hhmm():
    assert parse_hhmm("03:00") == time(3, 0)

    with pytest.raises(ConfigError):
        parse_hhmm("3am")


def test_parse_int_csv():
    assert parse_int_csv("199592366, 1849091") == (199592366, 1849091)

    with pytest.raises(ConfigError):
        parse_int_csv("199592366, abc")


def test_load_source_groups_file_accepts_lines_and_csv(tmp_path):
    groups_file = tmp_path / "Список групп.txt"
    groups_file.write_text("https://vk.ru/eofru, @asimovonline\n\n# комментарий\n", encoding="utf-8")

    assert load_source_groups_file(groups_file) == ("eofru", "asimovonline")


def test_load_terms_file_splits_keywords_and_hashtags(tmp_path):
    terms_file = tmp_path / "Список слов и хэштегов.txt"
    terms_file.write_text("Технология, #роботы\nИИ\n", encoding="utf-8")

    terms = load_terms_file(terms_file)

    assert terms.keywords == ("Технология", "ИИ")
    assert terms.hashtags == ("#Технология", "#роботы", "#ИИ")


def test_settings_from_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("VK_GROUP_TOKEN", raising=False)
    monkeypatch.delenv("VK_USER_TOKEN", raising=False)
    monkeypatch.delenv("FFBOT_SOURCE_GROUPS_FILE", raising=False)
    monkeypatch.delenv("FFBOT_TERMS_FILE", raising=False)
    monkeypatch.delenv("FFBOT_POST_RETENTION_DAYS", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "VK_GROUP_TOKEN=group",
                "VK_USER_TOKEN=user",
                "FFBOT_ALLOWED_USER_IDS=199592366,1849091",
                "FFBOT_SCHEDULE_TIME=03:00",
                "FFBOT_TIMEZONE=UTC",
                "FFBOT_POST_RETENTION_DAYS=15",
            ]
        ),
        encoding="utf-8",
    )

    settings = Settings.from_env(env_file)
    assert settings.post_retention_days == 15

    assert settings.vk_group_token == "group"
    assert settings.vk_user_token == "user"
    assert settings.vk_message_token == "group"
    assert settings.source_groups_file.name == "Список групп.txt"
    assert settings.terms_file.name == "Список слов и хэштегов.txt"
    assert settings.target_peer_id == 2_000_000_015
    assert settings.target_chat_title == "Аналитика и прогнозы"
    assert settings.allowed_user_ids == (199592366, 1849091)
    assert settings.command_poll_interval_seconds == 10.0
    assert settings.schedule_time == time(3, 0)
    assert settings.timezone == "UTC"
