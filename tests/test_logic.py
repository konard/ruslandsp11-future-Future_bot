from future_bot.logic import (
    Post,
    extract_links,
    filter_posts_by_groups,
    filter_posts_by_terms,
    flatten_terms,
    format_groups,
    format_numbered_links,
    parse_control_command,
    parse_query_groups,
    parse_search_command,
    remove_known_posts,
    remove_liked_posts,
    remove_posts_linked_from_ff,
)


def _post(text: str, post_id: int = 1) -> Post:
    return Post(owner_id=-1, post_id=post_id, source_group="eofru", date=post_id, text=text)


def test_filter_matches_only_standalone_words_not_substrings():
    posts = [
        _post("Илон Маск снова удивил", post_id=1),
        _post("Отличная маскировка животных", post_id=2),
        _post("Развитие ИИ ускоряется", post_id=3),
        _post("Слово РИИЛ не про нейросети", post_id=4),
        _post("Тема дня #Маск", post_id=5),
    ]

    masк = filter_posts_by_groups(posts, (("Маск",),))
    ии = filter_posts_by_groups(posts, (("ИИ",),))

    assert [post.post_id for post in masк] == [1, 5]
    assert [post.post_id for post in ии] == [3]


def test_filter_by_groups_requires_all_words_in_group_for_and():
    posts = [
        _post("ИИ и прогноз погоды", post_id=1),
        _post("Только про ИИ", post_id=2),
        _post("Только прогноз", post_id=3),
    ]

    filtered = filter_posts_by_groups(posts, (("ИИ", "прогноз"),))

    assert [post.post_id for post in filtered] == [1]


def test_filter_by_groups_matches_any_group_for_or():
    posts = [
        _post("ИИ и прогноз технологии", post_id=1),
        _post("антилопы и экология саванны", post_id=2),
        _post("кулинарные рецепты", post_id=3),
    ]

    groups = parse_query_groups("ИИ+прогноз+технологии, антилопы+экология")
    filtered = filter_posts_by_groups(posts, groups)

    assert [post.post_id for post in filtered] == [1, 2]


def test_parse_query_groups_splits_or_and_and():
    assert parse_query_groups("ИИ+прогноз, антилопы") == (("ИИ", "прогноз"), ("антилопы",))
    assert parse_query_groups("  , +, слово ") == (("слово",),)
    assert parse_query_groups("") == ()


def test_flatten_terms_deduplicates_preserving_order():
    assert flatten_terms((("ИИ", "прогноз"), ("прогноз", "антилопы"))) == ("ИИ", "прогноз", "антилопы")


def test_format_groups_renders_and_or_expression():
    assert format_groups((("ИИ", "прогноз"), ("антилопы",))) == "ИИ+прогноз, антилопы"
    assert format_groups((("Технологии",), ("Технология",))) == "Технологии, Технология"


def test_parse_control_command_tolerates_trailing_punctuation_and_case():
    assert parse_control_command("/Стоп Поиск!").action == "stop_search"
    assert parse_control_command("/стоп программа...").action == "shutdown"


def test_parse_search_command_supports_and_or_groups():
    command = parse_search_command("/поиск по (ИИ+прогноз+технологии, антилопы+экология) интервал 1д")

    assert command is not None
    assert command.groups == (("ИИ", "прогноз", "технологии"), ("антилопы", "экология"))
    assert command.interval_days == 1


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


def test_remove_liked_posts_drops_only_posts_marked_as_liked():
    posts = [
        Post(
            owner_id=-1,
            post_id=1,
            source_group="eofru",
            date=1,
            text="Новая технология",
            liked=True,
        ),
        Post(
            owner_id=-2,
            post_id=2,
            source_group="asimovonline",
            date=2,
            text="Другая технология",
            liked=False,
        ),
    ]

    remaining = remove_liked_posts(posts)

    assert [post.post_id for post in remaining] == [2]


def test_post_from_vk_item_marks_post_as_liked_from_user_likes_field():
    liked_item = {
        "owner_id": -1,
        "id": 1,
        "date": 1,
        "text": "Технология",
        "likes": {"count": 3, "user_likes": 1},
    }
    not_liked_item = {
        "owner_id": -1,
        "id": 2,
        "date": 2,
        "text": "Технология",
        "likes": {"count": 3, "user_likes": 0},
    }
    no_likes_item = {"owner_id": -1, "id": 3, "date": 3, "text": "Технология"}

    assert Post.from_vk_item(liked_item, "eofru").liked is True
    assert Post.from_vk_item(not_liked_item, "eofru").liked is False
    assert Post.from_vk_item(no_likes_item, "eofru").liked is False


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


def test_remove_known_posts_drops_posts_already_stored_in_database():
    known = Post(owner_id=-30, post_id=1, source_group="eofru", date=100, text="Технология")
    fresh = Post(owner_id=-30, post_id=2, source_group="eofru", date=200, text="Технология")

    remaining = remove_known_posts([known, fresh], ["https://vk.ru/wall-30_1"])

    assert [post.post_id for post in remaining] == [2]
