"""The agent's four tools and recipe-id provenance (Cayenne AI search §7.4). No database, no Gemini."""

from __future__ import annotations

import datetime
import json
import logging
import uuid
from decimal import Decimal
from typing import Any

import pytest

from recipeparser.adapters.ai_search.ai_db import Catalogue, ColumnInfo, QueryFailed, QueryResult, ViewInfo
from recipeparser.adapters.ai_search.answer import NoneAnswer, TextAnswer
from recipeparser.adapters.ai_search.tools import TOOL_DEFINITIONS, ToolBox

A = "11111111-1111-1111-1111-111111111111"
B = "22222222-2222-2222-2222-222222222222"
FOREIGN = "99999999-9999-9999-9999-999999999999"
CLAIMS = {"sub": "cook-a", "role": "authenticated"}


def _catalogue() -> Catalogue:
    cols = [ColumnInfo("id", "uuid", ""), ColumnInfo("title", "text", "")]
    return Catalogue(views={"recipes": ViewInfo("", cols)}, functions={})


def _result(columns: list[str], rows: list[tuple[Any, ...]], truncated: bool = False) -> QueryResult:
    return QueryResult(columns=columns, rows=rows, truncated=truncated, duration_ms=1.0)


class FakeDb:
    def __init__(self, results: list[Any] | None = None, owned: set[str] | None = None) -> None:
        self.results = list(results or [])
        self.calls: list[tuple[str, Any]] = []
        self.owned = owned if owned is not None else {A, B}

    def run(self, claims: Any, sql: str, params: Any = None, *, max_rows: int = 200) -> QueryResult:
        self.calls.append((sql, params))
        item = self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item  # type: ignore[no-any-return]

    def own_recipe_ids(self, claims: Any, ids: Any) -> set[str]:
        out = set()
        for v in ids:
            try:
                c = str(uuid.UUID(str(v)))
            except ValueError:
                continue
            if c in self.owned:
                out.add(c)
        return out


def _box(db: FakeDb, embed: Any = None) -> ToolBox:
    return ToolBox(db, CLAIMS, embed or (lambda text: [0.5, 1.0, -2.0]), _catalogue())  # type: ignore[arg-type]


def test_run_sql_refused_never_reaches_db(caplog: pytest.LogCaptureFixture) -> None:
    db = FakeDb()
    with caplog.at_level(logging.WARNING):
        out = _box(db).run("run_sql", {"query": "select * from pg_stat_activity"})
    assert out.is_error
    assert db.calls == []
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_run_sql_collects_uuid_values() -> None:
    db = FakeDb([_result(["id", "category_id", "title"], [(uuid.UUID(A), uuid.UUID(B), "Soup")])])
    box = _box(db)
    out = box.run("run_sql", {"query": "select id, title from recipes"})
    assert not out.is_error
    assert box.seen_ids == {A, B}
    assert json.loads(out.content)["rows"] == [{"id": A, "category_id": B, "title": "Soup"}]


def test_run_sql_string_uuid_canonicalised_but_text_not_scanned() -> None:
    db = FakeDb([_result(["id", "t"], [(A.upper(), f"see {B} here")])])
    box = _box(db)
    box.run("run_sql", {"query": "select id from recipes"})
    assert box.seen_ids == {A}


def test_run_sql_truncates_long_text_and_notes_cap() -> None:
    row = ("x" * 400, Decimal("2.5"), Decimal("3"), datetime.date(2026, 1, 2))
    db = FakeDb([_result(["t", "n", "d", "when"], [row], truncated=True)])
    data = json.loads(_box(db).run("run_sql", {"query": "select title from recipes"}).content)
    assert data["rows"][0]["t"] == "x" * 300 + "…"
    assert data["rows"][0]["n"] == 2.5
    assert data["rows"][0]["d"] == 3
    assert data["rows"][0]["when"] == "2026-01-02"
    assert data["note"] == "Showing the first 200 rows."


def test_run_sql_no_note_when_not_truncated() -> None:
    db = FakeDb([_result(["t"], [("a",)])])
    assert "note" not in json.loads(_box(db).run("run_sql", {"query": "select title from recipes"}).content)


def test_run_sql_query_failed_is_error() -> None:
    db = FakeDb([QueryFailed('column "x" does not exist')])
    out = _box(db).run("run_sql", {"query": "select title from recipes"})
    assert out.is_error
    assert "does not exist" in out.content


def test_run_sql_logs_never_rows(caplog: pytest.LogCaptureFixture) -> None:
    db = FakeDb([_result(["t"], [("secret-row-value",)])])
    with caplog.at_level(logging.INFO):
        _box(db).run("run_sql", {"query": "select title from recipes"})
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "select title from recipes" in text
    assert "secret-row-value" not in text


def test_semantic_search_clamps_limit_and_formats_vector() -> None:
    db = FakeDb([_result(["id", "title", "score"], [(uuid.UUID(A), "Miso soup", 0.9)])])
    box = _box(db)
    out = box.run("semantic_search", {"text": "cosy autumn soup", "limit": 500})
    sql, params = db.calls[0]
    assert "ai.semantic_search" in sql
    assert params[1] == 50
    assert params[0].startswith("[")
    assert [float(x) for x in params[0].strip("[]").split(",")] == [0.5, 1.0, -2.0]
    assert box.seen_ids == {A}
    assert not out.is_error


def test_semantic_search_clamps_low() -> None:
    db = FakeDb([_result(["id", "title", "score"], [])])
    _box(db).run("semantic_search", {"text": "x", "limit": 0})
    assert db.calls[0][1][1] == 1


def test_keyword_search_records_ids() -> None:
    rows = [(uuid.UUID(A), "Miso", 0.1), (uuid.UUID(B), "Dal", 0.05)]
    db = FakeDb([_result(["id", "title", "rank"], rows)])
    box = _box(db)
    box.run("keyword_search", {"text": "miso", "limit": 10})
    sql, params = db.calls[0]
    assert "ai.keyword_search" in sql
    assert params == ["miso", 10]
    assert box.seen_ids == {A, B}


def test_embed_failure_is_error_not_exception() -> None:
    def boom(text: str) -> list[float]:
        raise RuntimeError("gemini down")

    out = _box(FakeDb(), boom).run("semantic_search", {"text": "x", "limit": 5})
    assert out.is_error


def test_get_recipe_found_assembles_one_object() -> None:
    db = FakeDb(
        [
            _result(["id", "title"], [(uuid.UUID(A), "Miso soup")]),
            _result(["position", "name"], [(1, "miso")]),
            _result(["position", "text"], [(1, "Simmer.")]),
        ]
    )
    box = _box(db)
    out = box.run("get_recipe", {"id": A.upper()})
    data = json.loads(out.content)
    assert data["title"] == "Miso soup"
    assert data["ingredients"] == [{"position": 1, "name": "miso"}]
    assert data["steps"] == [{"position": 1, "text": "Simmer."}]
    assert len(db.calls) == 3
    assert all("order by" in s for s, _ in db.calls[1:])
    assert box.seen_ids == {A}
    assert out.progress == "Checking Miso soup"


def test_get_recipe_not_found() -> None:
    db = FakeDb([_result(["id", "title"], [])])
    box = _box(db)
    out = box.run("get_recipe", {"id": A})
    assert out.is_error
    assert out.content == "No recipe with that id."
    assert box.seen_ids == set()


def test_get_recipe_bad_uuid() -> None:
    db = FakeDb()
    out = _box(db).run("get_recipe", {"id": "not-a-uuid"})
    assert out.is_error
    assert db.calls == []


def test_unknown_tool_and_bad_input_are_errors() -> None:
    box = _box(FakeDb())
    assert box.run("drop_tables", {}).is_error
    assert box.run("run_sql", {}).is_error
    assert box.run("run_sql", {"query": 5}).is_error
    assert box.run("semantic_search", {"text": "x", "limit": "many"}).is_error


def test_progress_phrases() -> None:
    db = FakeDb(
        [
            _result(["id", "title", "score"], []),
            _result(["id", "title", "rank"], []),
            _result(["t"], []),
        ]
    )
    box = _box(db)
    assert (
        box.run("semantic_search", {"text": "cosy autumn soup", "limit": 5}).progress == "Looking for cosy autumn soup"
    )
    assert box.run("keyword_search", {"text": "a" * 60, "limit": 5}).progress == "Looking for " + "a" * 40
    assert box.run("run_sql", {"query": "select title from recipes"}).progress == "Checking the details"


def test_previous_ids_verified() -> None:
    box = _box(FakeDb(owned={A}))
    box.admit_previous(TextAnswer(kind="text", text="t", recipe_ids=[uuid.UUID(A), uuid.UUID(FOREIGN)]))
    assert box.seen_ids == {A}


def test_admit_previous_without_ids_is_noop() -> None:
    box = _box(FakeDb())
    box.admit_previous(NoneAnswer(kind="none", text="None of your recipes fit that."))
    assert box.seen_ids == set()


def test_permitted_requires_seen_and_owned() -> None:
    box = _box(FakeDb(owned={A}))
    box.seen_ids.update({A, B})
    assert box.permitted([A.upper(), B, FOREIGN, "junk"]) == {A}


def test_tool_definitions_strict() -> None:
    names = [t["name"] for t in TOOL_DEFINITIONS]
    assert names == ["run_sql", "semantic_search", "keyword_search", "get_recipe"]
    for t in TOOL_DEFINITIONS:
        assert t["strict"] is True
        schema = t["input_schema"]
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        assert t["description"]
    assert "cache_control" not in TOOL_DEFINITIONS[0]
    assert TOOL_DEFINITIONS[-1]["cache_control"] == {"type": "ephemeral"}
    assert TOOL_DEFINITIONS[1]["input_schema"]["properties"]["limit"]["type"] == "integer"


def test_failed_query_is_logged_as_warning(caplog: pytest.LogCaptureFixture) -> None:
    db = FakeDb([QueryFailed("boom message")])
    with caplog.at_level(logging.INFO):
        _box(db).run("run_sql", {"query": "select title from recipes"})
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings
    text = warnings[0].getMessage()
    assert "boom message" in text
    assert "select title from recipes" in text
    assert "cook-a" in text


def test_get_recipe_logs_counts(caplog: pytest.LogCaptureFixture) -> None:
    db = FakeDb(
        [
            _result(["id", "title"], [(uuid.UUID(A), "Miso")]),
            _result(["position"], [(1,), (2,)]),
            _result(["position"], [(1,)]),
        ]
    )
    with caplog.at_level(logging.INFO):
        _box(db).run("get_recipe", {"id": A})
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "ingredients=2" in text
    assert "steps=1" in text
    assert "duration_ms" in text


def test_pool_timeout_and_not_ai_reader_propagate() -> None:
    from psycopg_pool import PoolTimeout

    from recipeparser.adapters.ai_search.ai_db import NotAiReader

    with pytest.raises(PoolTimeout):
        _box(FakeDb([PoolTimeout("busy")])).run("run_sql", {"query": "select title from recipes"})
    with pytest.raises(NotAiReader):
        _box(FakeDb([NotAiReader("wrong role")])).run("keyword_search", {"text": "x", "limit": 3})


def test_unexpected_exception_logged_at_error_and_returned(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR):
        out = _box(FakeDb([RuntimeError("kaboom")])).run("run_sql", {"query": "select title from recipes"})
    assert out.is_error
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors
    assert errors[0].exc_info is not None


def test_non_finite_decimal_is_text() -> None:
    db = FakeDb([_result(["a", "b"], [(Decimal("Infinity"), Decimal("NaN"))])])
    out = _box(db).run("run_sql", {"query": "select title from recipes"})
    row = json.loads(out.content)["rows"][0]
    assert row == {"a": "Infinity", "b": "NaN"}


def test_only_dashed_uuid_form_counts() -> None:
    other = "33333333-3333-3333-3333-333333333333"
    db = FakeDb(
        [_result(["a", "b", "c", "d"], [(A.replace("-", ""), "{" + B + "}", "urn:uuid:" + other, other.upper())])]
    )
    box = _box(db)
    out = box.run("run_sql", {"query": "select title from recipes"})
    assert box.seen_ids == {other}
    assert json.loads(out.content)["rows"][0]["a"] == A.replace("-", "")


def test_refusal_log_caps_sql(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        _box(FakeDb()).run("run_sql", {"query": "select " + "x" * 20000 + " from pg_stat_activity"})
    assert all(len(r.getMessage()) < 9000 for r in caplog.records)


def test_progress_collapses_whitespace_and_cuts_title() -> None:
    db = FakeDb(
        [
            _result(["id", "title", "score"], []),
            _result(["id", "title"], [(uuid.UUID(A), "Miso\n  soup " + "z" * 80)]),
            _result([], []),
            _result([], []),
        ]
    )
    box = _box(db)
    assert (
        box.run("semantic_search", {"text": "cosy\n\n  autumn   soup", "limit": 5}).progress
        == "Looking for cosy autumn soup"
    )
    progress = box.run("get_recipe", {"id": A}).progress
    assert progress == "Checking " + ("Miso soup " + "z" * 80)[:40]


def test_non_ascii_not_escaped() -> None:
    db = FakeDb([_result(["t"], [("crème brûlée",)])])
    assert "crème brûlée" in _box(db).run("run_sql", {"query": "select title from recipes"}).content
