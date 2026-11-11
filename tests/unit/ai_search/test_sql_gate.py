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
        check_statement(sql, AI)
    assert reason_fragment in caught.value.reason
    assert "\n" not in caught.value.reason
    assert str(caught.value) == caught.value.reason


@pytest.mark.parametrize("sql", ALLOWED)
def test_allowed(sql: str) -> None:
    check_statement(sql, AI)


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
        check_statement(sql, AI)
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
    check_statement(sql, AI)


def test_ai_functions_are_the_two_searches() -> None:
    assert AI_FUNCTIONS == frozenset({"semantic_search", "keyword_search"})
