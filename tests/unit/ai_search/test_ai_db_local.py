"""The ai_reader connection on a real local Supabase with the Stage 2 ai schema (AI search §7.3).

These prove the "Stage 3 must" items of Cayenne's ``supabase/README.md`` that only Postgres can:
the connection is ai_reader itself, the role's login settings are present, row-level security
follows the claims ``run`` sets (also on a pooled connection reused by another cook), nothing is
writable, and the gate is what keeps the role away from tables PUBLIC grants let it read.

Skipped unless ``AI_SEARCH_TEST_ADMIN_DSN`` names a local database; see ``local_db.py``.

``run`` declares a server-side cursor for the statement, so only a SELECT (or VALUES) can run
through it: settings are read with ``current_setting``, never ``show``.
"""

from __future__ import annotations

import time

import pytest

from recipeparser.adapters.ai_search.ai_db import AiReaderDb, QueryFailed
from recipeparser.adapters.ai_search.sql_gate import StatementRefused, check_statement
from tests.unit.ai_search.local_db import EMBEDDING, Seeded, db_fixture, seeded_fixture  # noqa: F401 (fixtures)

pytestmark = pytest.mark.ai_db


def test_connects_as_ai_reader_itself(db: AiReaderDb, seeded: Seeded) -> None:
    result = db.run(seeded.a.claims, "select current_user::text, session_user::text")
    assert result.rows == [("ai_reader", "ai_reader")]


def test_role_settings_present(db: AiReaderDb, seeded: Seeded) -> None:
    def show(name: str) -> str:
        return str(db.run(seeded.a.claims, f"select current_setting('{name}')").rows[0][0])

    assert show("search_path") == "ai"
    assert show("statement_timeout") == "5s"
    assert show("default_transaction_read_only") == "on"
    assert show("lock_timeout") == "2s"
    assert show("idle_in_transaction_session_timeout") == "10s"
    assert show("transaction_read_only") == "on"


def test_cook_sees_only_own_rows(db: AiReaderDb, seeded: Seeded) -> None:
    result = db.run(seeded.a.claims, "select id::text from ai.recipes")
    assert {r[0] for r in result.rows} == set(seeded.a.recipe_ids)


def test_own_recipe_ids_drops_other_cooks(db: AiReaderDb, seeded: Seeded) -> None:
    asked = [*seeded.a.recipe_ids, *seeded.b.recipe_ids, "not-a-uuid"]
    assert db.own_recipe_ids(seeded.a.claims, asked) == set(seeded.a.recipe_ids)
    assert db.own_recipe_ids(seeded.b.claims, asked) == set(seeded.b.recipe_ids)


def test_pooled_connection_switches_cook(seeded: Seeded) -> None:
    db = AiReaderDb(seeded.reader_dsn, max_size=1)
    try:
        pid_a, ids_a = _pid_and_ids(db, seeded.a.claims)
        pid_b, ids_b = _pid_and_ids(db, seeded.b.claims)
        assert pid_a == pid_b  # the same pooled connection served both cooks
        assert ids_a == set(seeded.a.recipe_ids)
        assert ids_b == set(seeded.b.recipe_ids)
        # Nothing run() set outlives its transaction.
        leftover = db.run(seeded.b.claims, "select pg_backend_pid()").rows
        assert leftover[0][0] == pid_a
    finally:
        db.close()


def _pid_and_ids(db: AiReaderDb, claims: dict[str, object]) -> tuple[int, set[str]]:
    rows = db.run(claims, "select pg_backend_pid(), array(select id::text from ai.recipes)").rows
    return rows[0][0], set(rows[0][1])


def test_auth_uid_reads_claim_sub(db: AiReaderDb, seeded: Seeded) -> None:
    # ai_reader has no usage on schema auth, so it cannot call auth.uid() by name; the RLS policies
    # and ai.semantic_search call it for the role. Both keys auth.uid() reads carry run()'s sub...
    keys = db.run(
        seeded.b.claims,
        "select current_setting('request.jwt.claim.sub', true),"
        " current_setting('request.jwt.claims', true)::jsonb ->> 'sub'",
    )
    assert keys.rows == [(seeded.b.id, seeded.b.id)]
    # ...and auth.uid() inside a SECURITY DEFINER function sees cook B.
    found = db.run(seeded.b.claims, "select id::text from ai.semantic_search(%s, 5)", [EMBEDDING])
    assert {r[0] for r in found.rows} == set(seeded.b.recipe_ids)
    with pytest.raises(QueryFailed) as caught:
        db.run(seeded.b.claims, "select auth.uid()")
    assert "permission denied for schema auth" in caught.value.message


def test_writes_refused_read_only(db: AiReaderDb, seeded: Seeded) -> None:
    # A write inside a SELECT (lo_create is PUBLIC) reaches the transaction, which refuses it.
    with pytest.raises(QueryFailed) as caught:
        db.run(seeded.a.claims, "select lo_create(0)")
    assert "read-only" in caught.value.message


def test_insert_refused(db: AiReaderDb, seeded: Seeded) -> None:
    if not seeded.has_net:
        pytest.skip("pg_net is not installed on this local stack")
    # A plain INSERT cannot even be declared as a cursor; a data-modifying WITH cannot either.
    for sql in (
        "insert into net.http_request_queue (method, url, headers, timeout_milliseconds)"
        " values ('GET', 'http://ai-db-test.invalid/write', '{}'::jsonb, 1000)",
        "with w as (insert into net.http_request_queue (method, url, headers, timeout_milliseconds)"
        " values ('GET', 'http://ai-db-test.invalid/write', '{}'::jsonb, 1000) returning id) select * from w",
    ):
        with pytest.raises(QueryFailed):
            db.run(seeded.a.claims, sql)
    leftover = db.run(
        seeded.a.claims, "select count(*) from net.http_request_queue where url = 'http://ai-db-test.invalid/write'"
    )
    assert leftover.rows == [(0,)]


def test_reset_role_gains_nothing(db: AiReaderDb, seeded: Seeded) -> None:
    # Gate bypassed on purpose: a session that logged in as ai_reader has no other role to return to,
    # and RESET is not a SELECT, so it cannot run at all; set_config('role', 'none') changes nothing.
    with pytest.raises(QueryFailed):
        db.run(seeded.a.claims, "reset role")
    db.run(seeded.a.claims, "select set_config('role', 'none', true)")
    after = db.run(seeded.a.claims, "select current_user::text, session_user::text")
    assert after.rows == [("ai_reader", "ai_reader")]


def test_gate_blocks_what_the_role_can_read(db: AiReaderDb, seeded: Seeded) -> None:
    if not seeded.has_net:
        pytest.skip("pg_net is not installed on this local stack")
    sql = "select url, headers from net.http_request_queue"
    # PUBLIC grants let ai_reader read the queue (its headers can carry a mail API key)...
    readable = db.run(seeded.a.claims, sql)
    assert readable.columns == ["url", "headers"]
    assert any(row[0] == "http://ai-db-test.invalid/" for row in readable.rows)
    # ...so the statement gate is what stands between the agent and it.
    catalogue = db.catalogue()
    with pytest.raises(StatementRefused):
        check_statement(sql, catalogue.relations, catalogue.columns)


def test_catalogue_matches_the_stage_2_schema(db: AiReaderDb) -> None:
    catalogue = db.catalogue()
    assert {"recipes", "recipe_ingredients", "recipe_steps", "categories", "recipe_categories"} <= catalogue.relations
    assert {"semantic_search", "keyword_search"} <= set(catalogue.functions)
    assert {"score", "rank", "id", "title"} <= catalogue.columns
    assert all(view.comment for view in catalogue.views.values())
    assert all(col.comment for view in catalogue.views.values() for col in view.columns)
    # The gate accepts a query over the catalogue it was built from.
    check_statement(
        "select r.title, s.score from ai.semantic_search('[0.1]', 5) s join recipes r on r.id = s.id",
        catalogue.relations,
        catalogue.columns,
    )


def test_semantic_search_untyped_vector(db: AiReaderDb, seeded: Seeded) -> None:
    result = db.run(seeded.a.claims, "select id::text from ai.semantic_search(%s, 5)", [EMBEDDING])
    assert {r[0] for r in result.rows} == set(seeded.a.recipe_ids)


def test_statement_timeout_applies(db: AiReaderDb, seeded: Seeded) -> None:
    with pytest.raises(QueryFailed) as caught:
        db.run(seeded.a.claims, "select pg_sleep(6)")
    assert "statement timeout" in caught.value.message


def test_multiple_statements_refused_and_nothing_leaks(seeded: Seeded) -> None:
    # Gate bypassed on purpose. DECLARE ... FOR goes by the extended protocol, which runs one
    # statement only, so the COMMIT and the session SET never run, and the next cook on the same
    # connection keeps the role's settings.
    db = AiReaderDb(seeded.reader_dsn, max_size=1)
    try:
        for sql in ("select 1; commit; set statement_timeout = 0", "select 1; select 2"):
            with pytest.raises(QueryFailed, match="multiple commands"):
                db.run(seeded.a.claims, sql)
        after = db.run(
            seeded.b.claims, "select current_setting('statement_timeout'), current_setting('transaction_read_only')"
        )
        assert after.rows == [("5s", "on")]
    finally:
        db.close()


def test_commit_refused_and_nothing_leaks(seeded: Seeded) -> None:
    db = AiReaderDb(seeded.reader_dsn, max_size=1)
    try:
        with pytest.raises(QueryFailed):
            db.run(seeded.a.claims, "commit")
        after = db.run(seeded.b.claims, "select current_setting('statement_timeout'), current_user::text").rows[0]
        assert after == ("5s", "ai_reader")
    finally:
        db.close()


def test_session_set_does_not_outlive_run(seeded: Seeded) -> None:
    db = AiReaderDb(seeded.reader_dsn, max_size=1)
    try:
        # A session-level set_config inside run()'s transaction is undone by its rollback.
        db.run(seeded.a.claims, "select set_config('statement_timeout', '0', false)")
        db.run(seeded.a.claims, "select set_config('search_path', 'public', false)")
        after = db.run(
            seeded.b.claims, "select current_setting('statement_timeout'), current_setting('search_path')"
        ).rows[0]
        assert after == ("5s", "ai")
    finally:
        db.close()


def test_huge_result_is_bounded_on_the_server(seeded: Seeded) -> None:
    """A cross join of ten billion rows: 200 come back at once, and the connection is fine afterwards."""
    db = AiReaderDb(seeded.reader_dsn, max_size=1)
    try:
        started = time.monotonic()
        result = db.run(
            seeded.a.claims,
            "select a.i, b.i from generate_series(1, 100000) a(i) cross join generate_series(1, 100000) b(i)",
        )
        assert time.monotonic() - started < 3.0
        assert len(result.rows) == 200 and result.truncated is True
        after = db.run(seeded.b.claims, "select current_setting('transaction_read_only'), id::text from ai.recipes")
        assert {r[1] for r in after.rows} == set(seeded.b.recipe_ids)
        assert all(r[0] == "on" for r in after.rows)
    finally:
        db.close()


def test_statement_timeout_follows_the_time_left(db: AiReaderDb, seeded: Seeded) -> None:
    setting = db.run(seeded.a.claims, "select current_setting('statement_timeout')", timeout_ms=1234)
    assert setting.rows == [("1234ms",)]
    started = time.monotonic()
    with pytest.raises(QueryFailed) as caught:
        db.run(seeded.a.claims, "select pg_sleep(3)", timeout_ms=300)
    assert "statement timeout" in caught.value.message
    assert time.monotonic() - started < 2.0
    # Never above the role's 5 s, however much time is left.
    capped = db.run(seeded.a.claims, "select current_setting('statement_timeout')", timeout_ms=60_000)
    assert capped.rows == [("5s",)]


def test_like_pattern_with_percent_runs(db: AiReaderDb, seeded: Seeded) -> None:
    result = db.run(seeded.a.claims, "select title from ai.recipes where title like '%Soup%'")
    assert result.rows == [("Alpha Soup",)]
