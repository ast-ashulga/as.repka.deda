from repka.domain.suggest import suggest_keywords

TEXT = (
    "Tools for building Model Context Protocol servers. The servers expose tools and resources. "
    "Инструменты для MCP серверов и клиентов."
)


def test_suggest_quoted_phrases_first():
    assert suggest_keywords('Tools for "model context protocol" servers')[0] == ["model context protocol"]


def test_suggest_skips_stopwords():
    words = {word for group in suggest_keywords(TEXT) for term in group for word in term.split()}
    assert not words & {"the", "and", "for", "для", "и"}


def test_suggest_is_deterministic_and_capped():
    first = suggest_keywords(TEXT)
    assert first == suggest_keywords(TEXT)
    assert 0 < len(first) <= 6
