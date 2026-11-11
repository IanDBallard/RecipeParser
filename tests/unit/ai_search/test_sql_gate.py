"""The statement gate: the parse-tree allow-list for the agent's SQL (Cayenne AI search §7.3).

The agent's SQL runs as ``ai_reader``, which PUBLIC grants let read ``net.http_request_queue``,
``net._http_response``, ``extensions.pg_stat_statements`` and ``pg_stat_activity``, and which can
call ``set_config('request.jwt.claims', ...)``. This gate is the only thing between the agent and
those, so every case below is one thing it must refuse or one shape it must let through.
"""

from __future__ import annotations

import pytest

from recipeparser.adapters.ai_search.sql_gate import (
    AI_FUNCTIONS,
    MAX_SQL_CHARS,
    StatementRefused,
    check_statement,
)

AI = frozenset(
    {
        "recipes",
        "recipe_ingredients",
        "recipe_steps",
        "categories",
        "recipe_categories",
        "meal_plans",
        "meal_plan_entries",
    }
)

# Column names of the ai views the SQL below touches (a realistic subset of the catalogue's columns).
COLUMNS = frozenset(
    {
        "id",
        "title",
        "categories",
        "prep_min_minutes",
        "cook_min_minutes",
        "created_at",
        "rating",
        "source",
        "recipe_id",
        "name",
        "axis",
        "category_id",
        "position",
        "text",
        "rank",
    }
)

REFUSED = [
    pytest.param("select set_config('request.jwt.claims', '{\"sub\":\"x\"}', true)", "set_config", id="set_config"),
    pytest.param(
        "select 1 from ai.recipes where set_config('role','postgres',true) <> ''",
        "set_config",
        id="set_config_in_where",
    ),
    pytest.param("select * from net.http_request_queue", "net.http_request_queue", id="net_queue"),
    pytest.param("select * from net._http_response", "net._http_response", id="net_response"),
    pytest.param("select * from extensions.pg_stat_statements", "extensions", id="pg_stat_statements"),
    pytest.param("select * from pg_stat_activity", "pg_stat_activity", id="bare_catalog_name_refused"),
    pytest.param("select * from pg_catalog.pg_roles", "pg_catalog.pg_roles", id="pg_roles"),
    pytest.param("select * from information_schema.tables", "information_schema.tables", id="information_schema"),
    pytest.param("select * from public.recipes", "public.recipes", id="public_schema"),
    pytest.param(
        "select * from (with pg_stat_activity as (select 1) select 1) a, pg_stat_activity",
        "pg_stat_activity",
        id="cte_out_of_scope_refused",
    ),
    pytest.param("table pg_roles", "pg_roles", id="table_pg_roles"),
    pytest.param("select 1; select 2", "one statement", id="two_statements"),
    pytest.param("set role postgres", "VariableSetStmt", id="set_role"),
    pytest.param("reset role", "VariableSetStmt", id="reset_role"),
    pytest.param("do $$ begin end $$", "DoStmt", id="do_block"),
    pytest.param("explain select 1", "ExplainStmt", id="explain"),
    pytest.param("insert into ai.recipes (title) values ('x')", "InsertStmt", id="insert"),
    pytest.param("update ai.recipes set title = 'x'", "UpdateStmt", id="update"),
    pytest.param("delete from ai.recipes", "DeleteStmt", id="delete"),
    pytest.param("copy (select 1) to stdout", "CopyStmt", id="copy"),
    pytest.param("select * into t from ai.recipes", "INTO", id="select_into"),
    pytest.param("select * from ai.recipes for update", "FOR UPDATE", id="for_update"),
    pytest.param("select pg_sleep(10)", "pg_sleep", id="pg_sleep"),
    pytest.param("select current_setting('request.jwt.claims')", "current_setting", id="current_setting"),
    pytest.param("select lo_import('/etc/passwd')", "lo_import", id="lo_import"),
    pytest.param("select 'ai.recipes'::regclass", "regclass", id="regclass"),
    pytest.param(
        "select title from ai.recipes where title operator(public.=) 'x'", "operator", id="qualified_operator"
    ),
    pytest.param("select $1", "ParamRef", id="param_ref"),
    pytest.param("", "empty", id="empty"),
    pytest.param("selec", "could not be read", id="parse_error"),
    pytest.param("select 1" + " " * MAX_SQL_CHARS, str(MAX_SQL_CHARS), id="too_long"),
]

ALLOWED = [
    pytest.param(
        "select id, title from ai.recipes where 'Dessert' = any(categories) order by title limit 20", id="any_array"
    ),
    pytest.param("select count(*) from recipes", id="count_star"),
    pytest.param(
        "select r.id from recipes r join recipe_ingredients i on i.recipe_id = r.id where i.name ilike '%miso%'",
        id="join_ilike",
    ),
    pytest.param("with soups as (select id from ai.recipes where title ilike '%soup%') select * from soups", id="cte"),
    pytest.param(
        "with s as (select id from recipes) select * from recipe_steps where recipe_id in (select id from s)",
        id="cte_in_sublink",
    ),
    pytest.param(
        "with recursive t(n) as (select 1 union all select n + 1 from t where n < 3) select n from t",
        id="recursive_cte",
    ),
    pytest.param("select * from ai.keyword_search('gochujang', 10)", id="keyword_search"),
    pytest.param("select (prep_min_minutes + cook_min_minutes)::integer, created_at::date from recipes", id="casts"),
    pytest.param("select title, row_number() over (order by rating desc nulls last) from recipes", id="window"),
    pytest.param(
        "select extract(year from created_at), trim(title), substring(title from 1 for 10), position('a' in title)"
        " from recipes",
        id="sql_syntax_functions",
    ),
    pytest.param("select title from recipes where created_at > now() - interval '30 days'", id="interval"),
    pytest.param(
        "select coalesce(rating, 0), greatest(prep_min_minutes, 0), nullif(source, '') from recipes",
        id="coalesce_greatest_nullif",
    ),
    pytest.param(
        "select c.axis, count(*) from recipe_categories c group by c.axis having count(*) > 2", id="group_having"
    ),
]


@pytest.mark.parametrize(("sql", "reason_fragment"), REFUSED)
def test_refused(sql: str, reason_fragment: str) -> None:
    with pytest.raises(StatementRefused) as caught:
        check_statement(sql, AI, COLUMNS)
    assert reason_fragment in caught.value.reason
    assert "\n" not in caught.value.reason
    assert str(caught.value) == caught.value.reason


@pytest.mark.parametrize("sql", ALLOWED)
def test_allowed(sql: str) -> None:
    check_statement(sql, AI, COLUMNS)


# Beyond the brief's table: the scoping and name-length rulings of the task.
SCOPING_REFUSED = [
    # A non-recursive WITH sees only the CTEs before it; a later one is a real relation there.
    pytest.param(
        "with a as (select * from pg_roles), pg_roles as (select 1) select * from a",
        "pg_roles",
        id="forward_cte_reference",
    ),
    # A CTE in one FROM subquery is not in scope in its sibling.
    pytest.param(
        "select * from (with pg_roles as (select 1) select 1) a, (select * from pg_roles) b",
        "pg_roles",
        id="sibling_subquery_cte",
    ),
    pytest.param("select * from other.ai.recipes", "other.ai.recipes", id="catalog_qualified_relation"),
    pytest.param("select other.ai.recipes.title from ai.recipes", "other.ai.recipes.title", id="catalog_column"),
    pytest.param(
        "select 1 from ai.recipes where title operator(public.=) any (select title from recipes)",
        "operator",
        id="qualified_sublink_operator",
    ),
    pytest.param("select pg_catalog.keyword_search('x', 1)", "pg_catalog.keyword_search", id="search_in_catalog"),
    pytest.param("select ai.lower(title) from recipes", "ai.lower", id="builtin_in_ai"),
    pytest.param("select pg_catalog.set_config('role', 'x', true)", "set_config", id="qualified_set_config"),
    pytest.param("select 'x'::public.text", "public.text", id="qualified_type"),
]


@pytest.mark.parametrize(("sql", "reason_fragment"), SCOPING_REFUSED)
def test_scoping_refused(sql: str, reason_fragment: str) -> None:
    with pytest.raises(StatementRefused) as caught:
        check_statement(sql, AI, COLUMNS)
    assert reason_fragment in caught.value.reason


SCOPING_ALLOWED = [
    pytest.param("with a as (select 1 as x), b as (select x from a) select * from b", id="earlier_cte"),
    pytest.param(
        "with a as (select id from recipes) select * from (select * from a) s where s.id in (select id from a)",
        id="cte_in_nested_subqueries",
    ),
    pytest.param("select pg_catalog.lower(title), ai.semantic_search('x', 5) from recipes", id="qualified_ok"),
]


@pytest.mark.parametrize("sql", SCOPING_ALLOWED)
def test_scoping_allowed(sql: str) -> None:
    check_statement(sql, AI, COLUMNS)


# Fix round 1. Postgres reads `(x).name` and `alias.name` as `name(x)` when no such field or column
# exists, so attribute notation could call any one-argument function the gate never saw.
FUNCTION_NOTATION_REFUSED = [
    pytest.param("select ('request.jwt.claims'::text).current_setting", "current_setting", id="attr_current_setting"),
    pytest.param("select ((null::int).pg_stat_get_activity).query", "pg_stat_get_activity", id="attr_stat_activity"),
    pytest.param(
        "select (n).pg_stat_get_backend_activity from unnest(array[1,2,3,4,5]) n",
        "may be used in FROM",
        id="attr_backend_activity",
    ),
    pytest.param(
        "select (pid).pg_terminate_backend from unnest(array[123]) pid", "may be used in FROM", id="attr_terminate"
    ),
    pytest.param("select (1::bigint).pg_advisory_lock", "pg_advisory_lock", id="attr_advisory_lock"),
    pytest.param("select (5::float8).pg_sleep", "pg_sleep", id="attr_pg_sleep"),
    pytest.param("select ('s'::text).nextval", "nextval", id="attr_nextval"),
    pytest.param("select ('pg_class'::text).pg_relation_size", "pg_relation_size", id="attr_relation_size"),
    pytest.param("select (title).length from recipes", "length", id="attr_length"),
    pytest.param("select s.to_json from (select 1 a) s", "to_json", id="column_to_json"),
    pytest.param("select r.row_to_json from recipes r", "row_to_json", id="column_row_to_json"),
    pytest.param("select r.pg_column_size from recipes r", "pg_column_size", id="column_pg_column_size"),
    pytest.param("select title collate public.x from recipes", "public.x", id="qualified_collation"),
    # A function in FROM that returns a scalar makes `alias.name` call name(scalar); a name aliased
    # elsewhere in the statement must not open that door.
    pytest.param(
        "select s.current_setting from unnest(array['request.jwt.claims']) s, (select 1 as current_setting) t",
        "may be used in FROM",
        id="scalar_function_alias_borrowed_name",
    ),
    pytest.param(
        "select unnest.pg_sleep from unnest(array[10]), (select 1 as pg_sleep) t",
        "may be used in FROM",
        id="unaliased_function_borrowed_name",
    ),
]

# Fix round 2. Only the two ai search functions (which return rows) may stand in FROM: any other
# function there can return a scalar, whose alias Postgres reads `alias.f` as f(scalar).
FROM_FUNCTION_REFUSED = [
    # (a) a function alias's column list reused from another scope
    pytest.param(
        "select (select s.current_setting from unnest(array['server_version']) s)"
        " from unnest(array['a']) s(current_setting)",
        id="alias_reuse_in_scalar_subquery",
    ),
    pytest.param(
        "select q.v from unnest(array['a']) s(current_setting),"
        " lateral (select s.current_setting as v from unnest(array['server_version']) s) q",
        id="alias_reuse_in_lateral",
    ),
    pytest.param(
        "select s.pg_sleep from unnest(array[1]) s(pg_sleep)"
        " union all select null from (select s.pg_sleep from unnest(array[0.2::float8]) s) z",
        id="alias_reuse_across_union",
    ),
    # (b) non-FuncCall expressions in FROM
    pytest.param(
        "select coalesce.current_setting from coalesce('server_version'), (select 1 as current_setting) t",
        id="coalesce_in_from",
    ),
    pytest.param(
        "select text.current_setting from cast('server_version' as text), (select 1 as current_setting) t",
        id="cast_in_from",
    ),
    pytest.param(
        "select nullif.current_setting from nullif('server_version','x'), (select 1 as current_setting) t",
        id="nullif_in_from",
    ),
    pytest.param(
        "select greatest.pg_sleep from greatest(0.1::float8), (select 1 as pg_sleep) t", id="greatest_in_from"
    ),
    pytest.param(
        "select coalesce.pg_stat_get_backend_pid from coalesce(1), (select 1 as pg_stat_get_backend_pid) t",
        id="coalesce_backend_pid_in_from",
    ),
    pytest.param(
        'select "current_user".current_setting from current_user, (select 1 as current_setting) t',
        id="current_user_in_from",
    ),
    # shapes of FROM functions
    pytest.param("select * from unnest(array[1]) u", id="unnest_in_from"),
    pytest.param("select n from unnest(array[1, 2]) n", id="function_alias_column"),
    pytest.param("select * from rows from (ai.keyword_search('x', 5))", id="rows_from"),
    pytest.param("select * from ai.keyword_search('x', 5) with ordinality", id="with_ordinality"),
    pytest.param("select * from generate_series(1,3) g", id="generate_series"),
    pytest.param("select * from coalesce(1)", id="bare_coalesce_in_from"),
]


@pytest.mark.parametrize("sql", FROM_FUNCTION_REFUSED)
def test_from_function_refused(sql: str) -> None:
    with pytest.raises(StatementRefused) as caught:
        check_statement(sql, AI, COLUMNS)
    assert "may be used in FROM" in caught.value.reason


FROM_FUNCTION_ALLOWED = [
    pytest.param("select k.id, k.title, k.rank from ai.keyword_search('soup', 10) k", id="aliased_keyword_search"),
    pytest.param("select * from keyword_search('miso', 5)", id="bare_keyword_search"),
    pytest.param("select unnest(categories) from recipes", id="unnest_in_select_list"),
]


@pytest.mark.parametrize("sql", FROM_FUNCTION_ALLOWED)
def test_from_function_allowed(sql: str) -> None:
    check_statement(sql, AI, COLUMNS)


@pytest.mark.parametrize(("sql", "reason_fragment"), FUNCTION_NOTATION_REFUSED)
def test_function_notation_refused(sql: str, reason_fragment: str) -> None:
    with pytest.raises(StatementRefused) as caught:
        check_statement(sql, AI, COLUMNS)
    assert reason_fragment in caught.value.reason


NAMES_ALLOWED = [
    pytest.param("select categories[1] from recipes", id="subscript"),
    pytest.param("select (array_agg(title))[1] from recipes", id="subscript_of_call"),
    pytest.param("select (r.*) from recipes r", id="parenthesised_row_star"),
    pytest.param('select r.title collate "C" from recipes r', id="unqualified_collation"),
    pytest.param('select title collate pg_catalog."default" from recipes', id="catalog_collation"),
    pytest.param(
        "with recursive t(n) as (select 1 union all select n + 1 from t where n < 3) select n from t",
        id="alias_colname",
    ),
    pytest.param("select x.c from (select title as c from recipes) x", id="subquery_output_name"),
]


@pytest.mark.parametrize("sql", NAMES_ALLOWED)
def test_names_allowed(sql: str) -> None:
    check_statement(sql, AI, COLUMNS)


def test_ai_functions_are_the_two_searches() -> None:
    assert AI_FUNCTIONS == frozenset({"semantic_search", "keyword_search"})
