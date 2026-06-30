from future_bot.logic import (
    Post,
    extract_links,
    filter_posts_by_terms,
    format_numbered_links,
    parse_control_command,
    parse_search_command,
    remove_posts_linked_from_ff,
)


def test_filter_posts_by_russian_keyword_and_hashtag_case_insensitive():
    posts = [
        Post(owner_id=-1, post_id=1, source_group="eofru", date=1, text="Новая технология хранения энергии"),
        Post(owner_id=-1, post_id=2, source_group="eofru", date=2, text="Обзор дня #технология"),
        Post(owner_id=-1, post_id=3, source_group="eofru", date=3, text="Культурная афиша"),
    ]

    filtered = filter_posts_by_terms(posts, keywords=("Технология",), hashtags=("#Технология",))

    assert [post.post_id for post in filtered] == [1, 2]


def test_extract_links_from_text_and_vk_attachments_with_normalized_vk_domains():
    attachments = [
        {"type": "link", "link": {"url": "https://vk.ru/wall-20_30?utm_source=feed"}},
        {"type": "wall", "wall": {"owner_id": -40, "id": 50}},
    ]

    links = extract_links(
        "Источник: https://vk.com/wall-10_20?from=feed, группа vk.ru/eofru.",
        attachments,
    )

    assert links == [
        "https://vk.com/eofru",
        "https://vk.com/wall-10_20",
        "https://vk.com/wall-20_30",
        "https://vk.com/wall-40_50",
    ]


def test_remove_posts_whose_source_url_is_already_linked_from_ff_posts():
    posts = [
        Post(
            owner_id=-1,
            post_id=1,
            source_group="eofru",
            date=1,
            text="Новая технология",
            source_url="https://vk.ru/wall-100_200",
        ),
        Post(
            owner_id=-2,
            post_id=2,
            source_group="asimovonline",
            date=2,
            text="Новая технология",
            source_url="https://vk.com/wall-300_400",
        ),
    ]

    remaining = remove_posts_linked_from_ff(posts, {"https://vk.com/wall-100_200"})

    assert [post.source_url for post in remaining] == ["https://vk.com/wall-300_400"]


def test_format_numbered_links_and_empty_result_message():
    posts = [
        Post(owner_id=-1, post_id=1, source_group="eofru", date=1, text="", source_url="https://vk.com/wall-1_1"),
        Post(owner_id=-2, post_id=2, source_group="asimovonline", date=2, text="", source_url="https://vk.com/wall-2_2"),
    ]

    assert format_numbered_links(posts) == "1. https://vk.com/wall-1_1\n2. https://vk.com/wall-2_2"
    assert format_numbered_links([]) == "За последние сутки новых постов по заданным критериям не найдено."


def test_parse_search_command_extracts_keywords_and_interval():
    command = parse_search_command("/поиск по (Технологии, Технология) интервал 5д")

    assert command is not None
    assert command.keywords == ("Технологии", "Технология")
    assert command.hashtags == ("#Технологии", "#Технология")
    assert command.interval_days == 5


def test_parse_search_command_ignores_unrelated_messages():
    assert parse_search_command("поиск по (Технология) интервал 5д") is None
    assert parse_search_command("/поиск по (Технология) интервал 0д") is None


def test_parse_search_command_allows_empty_terms_for_file_based_search():
    command = parse_search_command("/поиск по () интервал 5д")

    assert command is not None
    assert command.keywords == ()
    assert command.hashtags == ()
    assert command.interval_days == 5


def test_parse_control_commands_for_stop_search_and_shutdown():
    stop_search = parse_control_command("/стоп поиск")
    shutdown = parse_control_command("/стоп программа")

    assert stop_search is not None
    assert stop_search.action == "stop_search"
    assert shutdown is not None
    assert shutdown.action == "shutdown"
