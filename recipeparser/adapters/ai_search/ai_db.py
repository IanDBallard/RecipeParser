# recipeparser/adapters/ai_search/ai_db.py
"""The ai_reader connection: where the agent's SQL runs (Cayenne AI search §7.3).

Every statement runs as the Postgres role ``ai_reader`` itself (never reached through ``set role``),
inside a ``READ ONLY`` transaction that carries one cook's token claims, so row-level security shows
that cook's rows and nothing else. The transaction is always rolled back, so nothing a statement
sets (the claims included) outlives it on a pooled connection.

The role's own settings (``search_path = ai``, ``statement_timeout = 5s``,
``default_transaction_read_only = on``) apply at login; this module sets none of them.

The statement gate (``sql_gate.check_statement``) runs before ``run``; this module trusts its input
only that far, and Postgres is the second line.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool, PoolTimeout

ROLE = "ai_reader"
DEFAULT_MAX_ROWS = 200

_BEGIN = "begin transaction read only"
_ROLLBACK = "rollback"
_SET_CLAIMS = "select set_config('request.jwt.claim.sub', %s, true), set_config('request.jwt.claims', %s, true)"
_OWN_RECIPES = "select id::text from ai.recipes where id = any(%s::uuid[])"

# Views with their columns, and functions with their output columns (TABLE/OUT arguments), in schema ai.
_CATALOGUE = """
select 'view' as kind, c.relname::text as name, coalesce(obj_description(c.oid, 'pg_class'), '') as comment,
       a.attnum::int as pos, a.attname::text as column_name, format_type(a.atttypid, a.atttypmod) as column_type,
       coalesce(col_description(c.oid, a.attnum), '') as column_comment
  from pg_class c
  join pg_namespace n on n.oid = c.relnamespace
  join pg_attribute a on a.attrelid = c.oid and a.attnum > 0 and not a.attisdropped
 where n.nspname = 'ai' and c.relkind = 'v'
union all
select 'function', p.proname::text, coalesce(obj_description(p.oid, 'pg_proc'), ''),
       o.ord::int, o.arg_name, null, ''
  from pg_proc p
  join pg_namespace n on n.oid = p.pronamespace
  left join lateral (
        select x.arg_name, x.ord
          from unnest(p.proargnames, p.proargmodes::text[]) with ordinality as x(arg_name, arg_mode, ord)
         where x.arg_mode in ('t', 'o')
       ) o on true
 where n.nspname = 'ai'
 order by 1 desc, 2, 4
"""


class NotAiReader(RuntimeError):
    """The connection is not logged in as ai_reader itself."""


class QueryFailed(Exception):
    """Postgres refused or failed the statement; ``message`` is its message, for the agent."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class QueryResult:
    columns: list[str]
    rows: list[tuple[Any, ...]]
    truncated: bool
    duration_ms: float


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    type: str
    comment: str


@dataclass(frozen=True)
class ViewInfo:
    comment: str
    columns: list[ColumnInfo]


@dataclass(frozen=True)
class Catalogue:
    views: dict[str, ViewInfo]
    functions: dict[str, str]
    function_columns: dict[str, list[str]] = field(default_factory=dict)

    @property
    def relations(self) -> frozenset[str]:
        """The ai view names: the only relations the gate lets the agent name."""
        return frozenset(self.views)

    @property
    def columns(self) -> frozenset[str]:
        """Every ai view column name and every ai function output column name."""
        names = {c.name for v in self.views.values() for c in v.columns}
        names.update(n for cols in self.function_columns.values() for n in cols)
        return frozenset(names)


def _check_identity(conn: psycopg.Connection[Any]) -> None:
    """Refuse a connection unless both current_user and session_user are ai_reader."""
    row = conn.execute("select current_user, session_user").fetchone()
    if row is None or row[0] != ROLE or row[1] != ROLE:
        raise NotAiReader(f"the search connection must log in as {ROLE}, not {row!r}")


class AiReaderDb:
    def __init__(self, dsn: str, *, max_size: int = 4) -> None:
        self._identity_error: NotAiReader | None = None
        self._catalogue: Catalogue | None = None
        self._lock = threading.Lock()
        self._pool = ConnectionPool(
            dsn,
            min_size=1,
            open=True,
            max_size=max_size,
            # prepare_threshold=None: no server-side prepared statements, which Supavisor's
            # transaction mode cannot carry from one transaction to the next.
            kwargs={"autocommit": True, "prepare_threshold": None},
            configure=self._configure,
        )

    def _configure(self, conn: psycopg.Connection[Any]) -> None:
        try:
            _check_identity(conn)
        except NotAiReader as e:
            # The pool discards the connection and retries; keep the reason so a caller left
            # waiting sees it instead of a bare timeout.
            self._identity_error = e
            raise

    def run(
        self,
        claims: Mapping[str, Any],
        sql: str,
        params: Sequence[Any] | None = None,
        *,
        max_rows: int = DEFAULT_MAX_ROWS,
    ) -> QueryResult:
        """Run one statement as the cook in ``claims`` inside a read-only transaction, always rolled back."""
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub:
            raise ValueError("claims must carry the cook's sub")
        return self._execute((sub, json.dumps(dict(claims))), sql, params, max_rows)

    def own_recipe_ids(self, claims: Mapping[str, Any], ids: Iterable[str]) -> set[str]:
        """The subset of ``ids`` that are the cook's own recipes, as ai.recipes shows them."""
        valid: list[str] = []
        for value in ids:
            try:
                valid.append(str(uuid.UUID(str(value))))
            except ValueError:
                continue
        if not valid:
            return set()
        result = self.run(claims, _OWN_RECIPES, [valid], max_rows=len(valid))
        return {str(row[0]) for row in result.rows}

    def catalogue(self) -> Catalogue:
        """The ai views, their columns and the ai functions, with their comments. Loaded once."""
        with self._lock:
            if self._catalogue is None:
                result = self._execute(None, _CATALOGUE, None, max_rows=100_000)
                self._catalogue = _build_catalogue(result.rows)
            return self._catalogue

    def close(self) -> None:
        self._pool.close()

    def _execute(
        self,
        claim_params: tuple[str, str] | None,
        sql: str,
        params: Sequence[Any] | None,
        max_rows: int,
    ) -> QueryResult:
        try:
            with self._pool.connection() as conn, conn.cursor() as cur:
                cur.execute(_BEGIN)
                try:
                    if claim_params is not None:
                        cur.execute(_SET_CLAIMS, claim_params)
                    started = time.perf_counter()
                    cur.execute(sql, params)
                    if cur.description is None:
                        columns: list[str] = []
                        rows: list[tuple[Any, ...]] = []
                    else:
                        columns = [d.name for d in cur.description]
                        rows = list(cur.fetchmany(max_rows + 1))
                    duration_ms = (time.perf_counter() - started) * 1000
                except psycopg.Error as e:
                    raise QueryFailed(_message(e)) from e
                finally:
                    try:
                        cur.execute(_ROLLBACK)
                    except psycopg.Error:
                        # A broken connection cannot roll back; the pool discards it on return.
                        pass
        except PoolTimeout as e:
            if self._identity_error is not None:
                raise self._identity_error from e
            raise
        return QueryResult(columns, rows[:max_rows], len(rows) > max_rows, duration_ms)


def _message(error: psycopg.Error) -> str:
    primary = getattr(error.diag, "message_primary", None)
    return str(primary) if primary else str(error)


def _build_catalogue(rows: Sequence[tuple[Any, ...]]) -> Catalogue:
    views: dict[str, ViewInfo] = {}
    functions: dict[str, str] = {}
    function_columns: dict[str, list[str]] = {}
    for kind, name, comment, _pos, column, column_type, column_comment in rows:
        if kind == "view":
            view = views.setdefault(name, ViewInfo(comment, []))
            view.columns.append(ColumnInfo(column, column_type, column_comment))
        else:
            functions[name] = comment
            cols = function_columns.setdefault(name, [])
            if column is not None:
                cols.append(column)
    return Catalogue(views=views, functions=functions, function_columns=function_columns)
