"""Fixtures for the ai_reader tests against a LOCAL Supabase with the Stage 2 ai schema.

``AI_SEARCH_TEST_ADMIN_DSN`` names the local database as ``postgres``. Unset: every test skips.
Set to anything but 127.0.0.1, localhost or ::1: every test FAILS, because these fixtures set
ai_reader's password and write auth.users rows, which must never happen to a live project.

As postgres, the fixtures give ai_reader a throwaway password, seed two cooks (A and B) the way
Cayenne's ``supabase/ci-assertions.sql`` does (auth.users rows, which the signup trigger turns into
starter categories; recipes with embeddings; one category each) and one ``net.http_request_queue``
row when pg_net is installed. Everything seeded is removed afterwards, the cooks through Cayenne's
``admin_delete_account``.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from recipeparser.adapters.ai_search.ai_db import AiReaderDb

ENV = "AI_SEARCH_TEST_ADMIN_DSN"
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
AI_READER_PASSWORD = "local-test-only"
EMBEDDING = "[" + ",".join(["0.1"] * 1536) + "]"


@dataclass
class Cook:
    id: str
    recipe_ids: list[str] = field(default_factory=list)

    @property
    def claims(self) -> dict[str, Any]:
        return {"sub": self.id, "role": "authenticated", "aud": "authenticated"}


@dataclass
class Seeded:
    admin_dsn: str
    reader_dsn: str
    a: Cook
    b: Cook
    has_net: bool


def admin_dsn() -> str:
    dsn = os.environ.get(ENV, "").strip()
    if not dsn:
        pytest.skip(f"{ENV} is not set: no local Supabase to test against")
    host = conninfo_to_dict(dsn).get("host") or ""
    if host not in LOCAL_HOSTS:
        pytest.fail(f"{ENV} must point at 127.0.0.1, localhost or ::1, not {host!r}: these tests write to it")
    return dsn


def reader_dsn(admin: str) -> str:
    parts: dict[str, Any] = dict(conninfo_to_dict(admin))
    parts["user"] = "ai_reader"
    parts["password"] = AI_READER_PASSWORD
    return make_conninfo("", **parts)


def _seed_cook(conn: psycopg.Connection[Any], label: str, titles: list[str]) -> Cook:
    cook = Cook(str(uuid.uuid4()))
    email = f"ai-db-{label}-{cook.id}@example.test"
    conn.execute("insert into auth.users (id, email) values (%s, %s)", (cook.id, email))
    conn.execute("insert into public.categories (user_id, name) values (%s, %s)", (cook.id, f"{label} category"))
    for title in titles:
        rid = str(uuid.uuid4())
        conn.execute(
            "insert into public.recipes (id, user_id, title, structured_ingredients, tokenized_directions, embedding)"
            " values (%s, %s, %s, '[]'::jsonb, '[]'::jsonb, %s::vector)",
            (rid, cook.id, title, EMBEDDING),
        )
        cook.recipe_ids.append(rid)
    return cook


@pytest.fixture(name="seeded", scope="module")
def seeded_fixture() -> Iterator[Seeded]:
    admin = admin_dsn()
    with psycopg.connect(admin, autocommit=True) as conn:
        # pytest-xdist workers each seed their own cooks; only the role change must not race.
        conn.execute("select pg_advisory_lock(hashtext('recipeparser ai_db tests'))")
        try:
            conn.execute(f"alter role ai_reader with password '{AI_READER_PASSWORD}'")
        finally:
            conn.execute("select pg_advisory_unlock(hashtext('recipeparser ai_db tests'))")
        has_net = conn.execute("select to_regclass('net.http_request_queue') is not null").fetchone() == (True,)
        a = _seed_cook(conn, "a", ["Alpha Soup", "Alpha Bread"])
        b = _seed_cook(conn, "b", ["Bravo Stew"])
        net_id = None
        if has_net:
            row = conn.execute(
                "insert into net.http_request_queue (method, url, headers, timeout_milliseconds)"
                " values ('GET', 'http://ai-db-test.invalid/', '{}'::jsonb, 1000) returning id"
            ).fetchone()
            net_id = row[0] if row else None
    try:
        yield Seeded(admin, reader_dsn(admin), a, b, has_net)
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            if net_id is not None:
                conn.execute("delete from net.http_request_queue where id = %s", (net_id,))
            # Cayenne's own account deletion removes every row the signup trigger and the seed made.
            for cook in (a, b):
                conn.execute("select public.admin_delete_account(%s, true)", (cook.id,))


@pytest.fixture(name="db")
def db_fixture(seeded: Seeded) -> Iterator[AiReaderDb]:
    d = AiReaderDb(seeded.reader_dsn, max_size=2)
    try:
        yield d
    finally:
        d.close()
