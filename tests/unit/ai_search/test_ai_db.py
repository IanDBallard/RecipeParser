"""The ai_reader connection, against a recording fake pool (Cayenne AI search §7.3).

The local-stack tests in ``test_ai_db_local.py`` prove the same behaviour on a real Postgres; these
pin the exact statements ``AiReaderDb`` sends and in what order.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import psycopg
import psycopg_pool
import pytest
from psycopg.pq import TransactionStatus

from recipeparser.adapters.ai_search import ai_db
from recipeparser.adapters.ai_search.ai_db import (
    AiReaderDb,
    Catalogue,
    ColumnInfo,
    NotAiReader,
    QueryFailed,
    ViewInfo,
    _check_identity,
)

CLAIMS = {"sub": "11111111-1111-1111-1111-111111111111", "role": "authenticated"}


class FakeCursor:
    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn
        self.description: list[SimpleNamespace] | None = None
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None, *, prepare: bool | None = None) -> FakeCursor:
        self.conn.executed.append((sql, params))
        self.conn.prepare_flags.append(prepare)
        self.conn.pipelined.append(self.conn.in_pipeline)
        lowered = sql.strip().lower()
        if lowered.startswith("begin"):
            self.conn.info.transaction_status = TransactionStatus.INTRANS
        elif lowered in ("rollback", "commit") or lowered.endswith("; commit"):
            self.conn.info.transaction_status = TransactionStatus.IDLE
        for fragment, outcome in self.conn.script.items():
            if fragment in sql:
                if isinstance(outcome, _Raiser):
                    outcome.callback()
                if isinstance(outcome, BaseException):
                    raise outcome
                columns, rows = outcome
                self.description = [SimpleNamespace(name=c) for c in columns]
                self._rows = list(rows)
                return self
        self.description = None
        self._rows = []
        return self

    def fetchmany(self, size: int) -> list[tuple[Any, ...]]:
        self.conn.fetch_sizes.append(size)
        return self._rows[:size]

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


class FakeConn:
    def __init__(self, script: dict[str, Any] | None = None) -> None:
        self.script: dict[str, Any] = script or {}
        self.executed: list[tuple[str, Any]] = []
        self.prepare_flags: list[bool | None] = []
        self.pipelined: list[bool] = []
        self.in_pipeline = False
        self.fetch_sizes: list[int] = []
        self.info = SimpleNamespace(transaction_status=TransactionStatus.IDLE)
        self.closed = False

    def close(self) -> None:
        self.closed = True

    @contextmanager
    def pipeline(self) -> Iterator[None]:
        self.in_pipeline = True
        try:
            yield
        finally:
            self.in_pipeline = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def execute(self, sql: str, params: Any = None) -> FakeCursor:
        return self.cursor().execute(sql, params)


class FakePool:
    instances: list[FakePool] = []

    def __init__(self, conninfo: str, **kwargs: Any) -> None:
        self.conninfo = conninfo
        self.kwargs = kwargs
        self.conn = FakeConn()
        self.closed = False
        FakePool.instances.append(self)

    @contextmanager
    def connection(self, timeout: float | None = None) -> Iterator[FakeConn]:
        yield self.conn

    def close(self) -> None:
        self.closed = True

    @contextmanager
    def pipeline(self) -> Iterator[None]:
        self.in_pipeline = True
        try:
            yield
        finally:
            self.in_pipeline = False


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> Iterator[AiReaderDb]:
    FakePool.instances = []
    monkeypatch.setattr(ai_db, "ConnectionPool", FakePool)
    d = AiReaderDb("postgresql://ai_reader:x@127.0.0.1:5432/postgres", max_size=2)
    yield d
    d.close()


def _pool() -> FakePool:
    return FakePool.instances[-1]


def _statements() -> list[str]:
    return [" ".join(sql.split()) for sql, _ in _pool().conn.executed]


def test_pool_is_autocommit_unprepared_bounded_and_checks_identity(db: AiReaderDb) -> None:
    kwargs = _pool().kwargs
    assert kwargs["kwargs"] == {
        "autocommit": True,
        "prepare_threshold": None,
        "connect_timeout": 5,
        "keepalives": 1,
        "keepalives_idle": 10,
        "keepalives_interval": 5,
        "keepalives_count": 2,
        "tcp_user_timeout": 15000,
    }
    assert kwargs["max_size"] == 2
    assert kwargs["timeout"] == 10
    assert kwargs["check"] is psycopg_pool.ConnectionPool.check_connection
    assert callable(kwargs["configure"])


def test_identity_error_cleared_once_a_connection_passes(db: AiReaderDb) -> None:
    configure = _pool().kwargs["configure"]
    with pytest.raises(NotAiReader):
        configure(FakeConn({"current_user": (["current_user", "session_user"], [("postgres", "postgres")])}))
    assert db._identity_error is not None
    configure(FakeConn({"current_user": (["current_user", "session_user"], [("ai_reader", "ai_reader")])}))
    assert db._identity_error is None


def test_run_wraps_read_only_and_sets_both_claims(db: AiReaderDb) -> None:
    _pool().conn.script = {"from ai.recipes": (["id"], [("a",)])}
    db.run(CLAIMS, "select id from ai.recipes")
    executed = _pool().conn.executed
    statements = _statements()
    assert statements[0] == "begin transaction read only"
    assert "set_config('request.jwt.claim.sub', %s, true)" in statements[1]
    assert "set_config('request.jwt.claims', %s, true)" in statements[1]
    assert executed[1][1] == (CLAIMS["sub"], json.dumps(CLAIMS))
    assert statements[2] == "select id from ai.recipes"
    assert executed[2][1] is None
    assert statements[3] == "rollback"
    assert len(statements) == 4


def test_run_returns_columns_rows_and_duration(db: AiReaderDb) -> None:
    _pool().conn.script = {"from ai.recipes": (["id", "title"], [("a", "Soup")])}
    result = db.run(CLAIMS, "select id, title from ai.recipes where title = %s", ["Soup"])
    assert result.columns == ["id", "title"]
    assert result.rows == [("a", "Soup")]
    assert result.truncated is False
    assert result.duration_ms >= 0
    assert _pool().conn.executed[2][1] == ["Soup"]


def test_run_caps_rows_and_flags_truncation(db: AiReaderDb) -> None:
    rows = [(str(i),) for i in range(201)]
    _pool().conn.script = {"from ai.recipes": (["id"], rows)}
    result = db.run(CLAIMS, "select id from ai.recipes")
    assert _pool().conn.fetch_sizes == [201]
    assert len(result.rows) == 200
    assert result.rows[-1] == ("199",)
    assert result.truncated is True


def test_run_exactly_max_rows_is_not_truncated(db: AiReaderDb) -> None:
    _pool().conn.script = {"from ai.recipes": (["id"], [("a",), ("b",)])}
    result = db.run(CLAIMS, "select id from ai.recipes", max_rows=2)
    assert result.rows == [("a",), ("b",)]
    assert result.truncated is False


def test_run_rolls_back_on_error_and_raises_query_failed(db: AiReaderDb) -> None:
    _pool().conn.script = {"from ai.nope": psycopg.errors.UndefinedTable('relation "ai.nope" does not exist')}
    with pytest.raises(QueryFailed) as caught:
        db.run(CLAIMS, "select * from ai.nope")
    assert 'relation "ai.nope" does not exist' in caught.value.message
    assert _statements()[-1] == "rollback"


def test_run_requires_a_sub(db: AiReaderDb) -> None:
    with pytest.raises(ValueError):
        db.run({"role": "authenticated"}, "select 1")
    assert _pool().conn.executed == []


def test_identity_check_refuses_other_roles() -> None:
    conn = FakeConn({"current_user": (["current_user", "session_user"], [("postgres", "postgres")])})
    with pytest.raises(NotAiReader):
        _check_identity(conn)  # type: ignore[arg-type]


def test_identity_check_refuses_set_role_from_another_login() -> None:
    conn = FakeConn({"current_user": (["current_user", "session_user"], [("ai_reader", "postgres")])})
    with pytest.raises(NotAiReader):
        _check_identity(conn)  # type: ignore[arg-type]


def test_identity_check_accepts_ai_reader() -> None:
    conn = FakeConn({"current_user": (["current_user", "session_user"], [("ai_reader", "ai_reader")])})
    _check_identity(conn)  # type: ignore[arg-type]


def test_own_recipe_ids_drops_non_uuids_before_query(db: AiReaderDb) -> None:
    good = "22222222-2222-2222-2222-222222222222"
    _pool().conn.script = {"from ai.recipes": (["id"], [(good,)])}
    owned = db.own_recipe_ids(CLAIMS, [good, "not-a-uuid", "1; drop table x", ""])
    assert owned == {good}
    sql, params = _pool().conn.executed[2]
    assert "from ai.recipes where id = any(%s::uuid[])" in " ".join(sql.split())
    assert params == [[good]]


def test_own_recipe_ids_with_nothing_valid_skips_the_query(db: AiReaderDb) -> None:
    assert db.own_recipe_ids(CLAIMS, ["nope"]) == set()
    assert _pool().conn.executed == []


CATALOGUE_COLUMNS = ["kind", "name", "comment", "pos", "col", "type", "col_comment"]
CATALOGUE_ROWS = [
    ("function", "keyword_search", "Keyword match.", 3, "id", None, ""),
    ("function", "keyword_search", "Keyword match.", 4, "title", None, ""),
    ("function", "keyword_search", "Keyword match.", 5, "rank", None, ""),
    ("function", "semantic_search", "Meaning match.", 3, "id", None, ""),
    ("function", "semantic_search", "Meaning match.", 4, "title", None, ""),
    ("function", "semantic_search", "Meaning match.", 5, "score", None, ""),
    ("view", "recipe_steps", "One row per step.", 1, "recipe_id", "uuid", "The recipe."),
    ("view", "recipe_steps", "One row per step.", 2, "position", "integer", "From 1."),
    ("view", "recipes", "The cook's recipes.", 1, "id", "uuid", "Recipe id."),
    ("view", "recipes", "The cook's recipes.", 2, "title", "text", "Title."),
]


def test_catalogue_shapes_views_and_functions(db: AiReaderDb) -> None:
    _pool().conn.script = {"pg_class": (CATALOGUE_COLUMNS, CATALOGUE_ROWS)}
    cat = db.catalogue()
    assert cat.views == {
        "recipe_steps": ViewInfo(
            "One row per step.",
            [ColumnInfo("recipe_id", "uuid", "The recipe."), ColumnInfo("position", "integer", "From 1.")],
        ),
        "recipes": ViewInfo(
            "The cook's recipes.", [ColumnInfo("id", "uuid", "Recipe id."), ColumnInfo("title", "text", "Title.")]
        ),
    }
    assert cat.functions == {"keyword_search": "Keyword match.", "semantic_search": "Meaning match."}
    assert cat.relations == frozenset({"recipes", "recipe_steps"})
    statements = _statements()
    assert statements[0] == "begin transaction read only"
    assert not any("set_config" in s for s in statements)
    assert statements[-1] == "rollback"


def test_catalogue_columns_include_function_outputs(db: AiReaderDb) -> None:
    _pool().conn.script = {"pg_class": (CATALOGUE_COLUMNS, CATALOGUE_ROWS)}
    assert db.catalogue().columns == frozenset({"recipe_id", "position", "id", "title", "score", "rank"})


def test_catalogue_columns_property_unit() -> None:
    cat = Catalogue(
        views={"recipes": ViewInfo("", [ColumnInfo("title", "text", "")])},
        functions={"semantic_search": ""},
        function_columns={"semantic_search": ["id", "score"]},
    )
    assert cat.columns == frozenset({"title", "id", "score"})


def test_catalogue_cached(db: AiReaderDb) -> None:
    _pool().conn.script = {"pg_class": (CATALOGUE_COLUMNS, CATALOGUE_ROWS)}
    first = db.catalogue()
    count = len(_pool().conn.executed)
    assert db.catalogue() is first
    assert len(_pool().conn.executed) == count


def test_close_closes_the_pool(db: AiReaderDb) -> None:
    db.close()
    assert _pool().closed is True


def test_run_always_uses_the_extended_protocol(db: AiReaderDb) -> None:
    # No parameters would mean psycopg's simple protocol, which runs "select 1; commit; ..." whole.
    _pool().conn.script = {"from ai.recipes": (["title"], [("Soup",)])}
    db.run(CLAIMS, "select title from ai.recipes where title like '%soup%'")
    sql, params = _pool().conn.executed[2]
    assert sql == "select title from ai.recipes where title like '%soup%'"
    assert params is None
    assert _pool().conn.prepare_flags[2] is False
    assert _pool().conn.pipelined == [False, False, True, False]  # only the statement is pipelined


def test_run_discards_a_connection_whose_transaction_ended(db: AiReaderDb) -> None:
    with pytest.raises(QueryFailed) as caught:
        db.run(CLAIMS, "commit")
    assert "transaction" in caught.value.message
    assert _pool().conn.closed is True
    assert _statements()[-1] == "commit"  # no rollback on a connection already closed


def test_stale_connection_on_begin_raises_query_failed(db: AiReaderDb) -> None:
    _pool().conn.script = {"begin transaction": psycopg.OperationalError("server closed the connection")}
    with pytest.raises(QueryFailed) as caught:
        db.run(CLAIMS, "select 1")
    assert "server closed the connection" in caught.value.message


def test_failed_rollback_closes_and_raises_query_failed(db: AiReaderDb) -> None:
    _pool().conn.script = {"rollback": psycopg.OperationalError("connection lost")}
    with pytest.raises(QueryFailed) as caught:
        db.run(CLAIMS, "select 1")
    assert "connection lost" in caught.value.message
    assert _pool().conn.closed is True


def test_failed_statement_that_ended_the_transaction_is_discarded(db: AiReaderDb) -> None:
    class Ends(psycopg.ProgrammingError):
        pass

    conn = _pool().conn

    def end_and_fail() -> None:
        conn.info.transaction_status = TransactionStatus.IDLE
        raise Ends("ended")

    conn.script = {"end it": _Raiser(end_and_fail)}
    with pytest.raises(QueryFailed) as caught:
        db.run(CLAIMS, "end it")
    assert "transaction" in caught.value.message
    assert conn.closed is True


class _Raiser(Exception):
    """A scripted outcome that runs a callback (which raises) instead of being raised itself."""

    def __init__(self, callback: Any) -> None:
        super().__init__("callback")
        self.callback = callback
