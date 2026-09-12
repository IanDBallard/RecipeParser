"""The citation rules, against the strings measured in the library on 2026-09-11."""
import json
from pathlib import Path

import pytest

from recipeparser.core.citation import (
    UNKNOWN_BOOK_KEY,
    Citation,
    book_citation,
    classify_source,
    host_of,
    normalise_key,
    resolve_citation,
    web_citation,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "citation_keys.json"


@pytest.mark.parametrize("case", json.loads(FIXTURE.read_text(encoding="utf-8")), ids=lambda c: c["input"])
def test_normalise_key_matches_the_shared_fixture(case):
    assert normalise_key(case["input"]) == case["key"]


def test_normalise_key_strips_a_backtick_like_the_client_does():
    assert normalise_key("`Perfect`") == "perfect"


def test_host_of_strips_scheme_path_case_and_www():
    assert host_of("https://www.Cooking.NYTimes.com/recipes/1") == "cooking.nytimes.com"
    assert host_of("thewoksoflife.com") == "thewoksoflife.com"
    assert host_of("www.thewoksoflife.com") == "thewoksoflife.com"
    assert host_of("cooking.nytimes.com/recipes/1") == "cooking.nytimes.com"


def test_book_citation_with_metadata():
    c = book_citation("Italian Food", "Elizabeth David")
    assert c == Citation("book", "italian food", "Italian Food", "Elizabeth David")
    assert c.display() == "Italian Food — Elizabeth David"


def test_book_citation_without_metadata_is_unknown_book():
    c = book_citation(None, None)
    assert c == Citation("unknown", UNKNOWN_BOOK_KEY, None, None)
    assert c.display() is None
    assert book_citation("", "Someone") == Citation("unknown", UNKNOWN_BOOK_KEY, None, None)


def test_web_citation_title_is_site_name_else_host():
    assert web_citation("https://cooking.nytimes.com/recipes/1") == Citation(
        "web", "cooking.nytimes.com", "cooking.nytimes.com", None
    )
    assert web_citation("https://cooking.nytimes.com/recipes/1", "NYT Cooking", "Melissa Clark") == Citation(
        "web", "cooking.nytimes.com", "NYT Cooking", "Melissa Clark"
    )


# The seven rules, in order, first match wins (design: The backfill, per measured value).
@pytest.mark.parametrize(
    "text, rule, expected",
    [
        (None, 1, Citation("unknown", None, None, None)),
        ("   ", 1, Citation("unknown", None, None, None)),
        (
            "The Food of Sichuan — Fuchsia Dunlop",
            2,
            Citation("book", "the food of sichuan", "The Food of Sichuan", "Fuchsia Dunlop"),
        ),
        ("Italian Food — Elizabeth David", 2, Citation("book", "italian food", "Italian Food", "Elizabeth David")),
        ("EPUB Auto-Import", 3, Citation("unknown", UNKNOWN_BOOK_KEY, None, None)),
        ("PDF Auto-Import", 3, Citation("unknown", UNKNOWN_BOOK_KEY, None, None)),
        ("cooking.nytimes.com", 4, Citation("web", "cooking.nytimes.com", "cooking.nytimes.com", None)),
        ("Cooking.nytimes.com", 4, Citation("web", "cooking.nytimes.com", "cooking.nytimes.com", None)),
        ("www.thewoksoflife.com", 4, Citation("web", "thewoksoflife.com", "thewoksoflife.com", None)),
        ("thewoksoflife.com", 4, Citation("web", "thewoksoflife.com", "thewoksoflife.com", None)),
        ("https://www.seriouseats.com/x", 4, Citation("web", "seriouseats.com", "seriouseats.com", None)),
        (
            "Womanandhome.com Justin Gellatly",
            5,
            Citation("web", "womanandhome.com", "womanandhome.com", "Justin Gellatly"),
        ),
        ("Gemini", 6, Citation("unknown", None, None, None)),
        ("Nik Sharma", 7, Citation("person", "nik sharma", "Nik Sharma", None)),
        ("Perfect", 7, Citation("person", "perfect", "Perfect", None)),
    ],
)
def test_classify_source_rules(text, rule, expected):
    got = classify_source(text)
    assert got.rule == rule
    assert got.citation == expected


def test_the_two_spellings_of_a_site_share_one_key():
    a = classify_source("cooking.nytimes.com").citation.key
    b = classify_source("Cooking.nytimes.com").citation.key
    c = classify_source("www.thewoksoflife.com").citation.key
    d = classify_source("thewoksoflife.com").citation.key
    assert a == b and c == d


def test_classify_source_carries_a_byline_where_the_rule_has_no_author():
    assert classify_source("cooking.nytimes.com", "Melissa Clark").citation.author == "Melissa Clark"
    assert classify_source(None, "Melissa Clark").citation.author == "Melissa Clark"
    # A book string's own author beats a byline.
    assert classify_source("Italian Food — Elizabeth David", "Someone Else").citation.author == "Elizabeth David"


def test_resolve_citation_reader_beats_model():
    book = book_citation("Italian Food", "Elizabeth David")
    assert resolve_citation(book, "Some Site", "Someone") == book
    unknown_book = book_citation(None, None)
    assert resolve_citation(unknown_book, "Some Site", "Someone") == unknown_book


def test_resolve_citation_web_takes_site_name_and_byline_from_the_model():
    host = web_citation("https://cooking.nytimes.com/recipes/1")
    got = resolve_citation(host, "NYT Cooking", "Melissa Clark")
    assert got == Citation("web", "cooking.nytimes.com", "NYT Cooking", "Melissa Clark")
    assert resolve_citation(host, None, None) == host


def test_resolve_citation_with_nothing_known_classifies_the_stated_source():
    assert resolve_citation(None, "Bon Appétit", "Molly Baz") == Citation(
        "person", "bon appétit", "Bon Appétit", "Molly Baz"
    )
    assert resolve_citation(None, "cooking.nytimes.com", None).kind == "web"
    assert resolve_citation(None, "Gemini", None) == Citation("unknown", None, None, None)
    assert resolve_citation(None, None, None) == Citation("unknown", None, None, None)
