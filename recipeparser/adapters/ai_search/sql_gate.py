# recipeparser/adapters/ai_search/sql_gate.py
"""The statement gate: a parse-tree allow-list for SQL the agent writes (Cayenne AI search §7.3).

The agent's SQL runs as the Postgres role ``ai_reader``. PUBLIC grants still let that role read
``net.http_request_queue`` (its headers carry a mail API key), ``net._http_response``,
``extensions.pg_stat_statements`` and ``pg_stat_activity``, and any role may call
``set_config('request.jwt.claims', ...)`` to pose as another cook. This gate is what keeps the
agent away from them, so it is an allow-list: anything not named here is refused.

Three axes, applied to the whole parse tree:

1. Statements: exactly one ``SelectStmt``, with no ``INTO`` and no row locking anywhere.
2. Node types: every node is in ``ALLOWED_NODES``.
3. Names: relations are ``ai`` views (or a CTE in scope), functions, types and operators are on
   their lists with only the qualifiers that keep them where they belong.

Postgres reads attribute notation as a call when no field matches: ``(x).f`` and ``q.f`` become
``f(x)`` / ``f(q)``. So field access after a parenthesis is refused, and the last part of every
column reference must be an ``ai`` column or a name the statement itself declares.

A refusal raises ``StatementRefused`` whose ``reason`` is one plain sentence naming what was
refused. It goes back to the agent so it can rewrite the query.

pglast representations the lists cover (probed against pglast 8.7):

- ``TRIM(x)`` is ``FuncCall pg_catalog.btrim``; ``SUBSTRING(x FROM a FOR b)``, ``POSITION(a IN b)``
  and ``EXTRACT(f FROM x)`` are ``FuncCall pg_catalog.substring/position/extract``.
- ``NULLIF`` and ``= ANY(...)`` are ``A_Expr`` kinds; ``GREATEST`` is ``MinMaxExpr``.
- ``count(*)`` is a ``FuncCall`` with ``agg_star`` set (no ``A_Star`` node).
- ``::integer`` and ``interval '30 days'`` are ``TypeName pg_catalog.int4`` / ``pg_catalog.interval``.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence

from pglast import ast, parse_sql
from pglast.parser import ParseError

MAX_SQL_CHARS = 8000

ALLOWED_FUNCTIONS = frozenset({
    # aggregates and windows
    "count", "sum", "avg", "min", "max", "array_agg", "string_agg", "bool_and", "bool_or", "every",
    "row_number", "rank", "dense_rank",
    # text
    "lower", "upper", "initcap", "length", "char_length", "btrim", "ltrim", "rtrim", "substring", "substr",
    "position", "strpos", "replace", "split_part", "concat", "concat_ws", "left", "right", "starts_with",
    # numbers, dates
    "abs", "round", "ceil", "ceiling", "floor", "trunc", "now", "date_trunc", "date_part", "extract",
    "age", "make_date", "to_char",
    # arrays
    "array_length", "cardinality", "array_to_string", "array_position", "string_to_array", "unnest",
})  # fmt: skip

AI_FUNCTIONS = frozenset({"semantic_search", "keyword_search"})

ALLOWED_TYPES = frozenset({
    "text", "varchar", "bpchar", "int2", "int4", "int8", "integer", "smallint", "bigint", "numeric",
    "float4", "float8", "real", "bool", "boolean", "date", "time", "timestamp", "timestamptz",
    "interval", "uuid",
})  # fmt: skip

ALLOWED_NODES = frozenset({
    "SelectStmt", "ResTarget", "ColumnRef", "A_Star", "A_Const", "A_Expr", "BoolExpr", "NullTest",
    "BooleanTest", "SubLink", "RangeVar", "RangeSubselect", "RangeFunction", "JoinExpr", "FuncCall",
    "TypeCast", "TypeName", "SortBy", "WithClause", "CommonTableExpr", "CaseExpr", "CaseWhen",
    "CoalesceExpr", "MinMaxExpr", "A_ArrayExpr", "A_Indices", "A_Indirection", "SQLValueFunction",
    "WindowDef", "RowExpr", "Alias", "CollateClause", "String", "Integer", "Float", "Boolean", "BitString",
})  # fmt: skip

_AI_SCHEMA = "ai"
_CATALOG_SCHEMA = "pg_catalog"
# catalog.schema.table.column is four parts; only a reference into another catalog needs four.
_MAX_COLUMN_PARTS = 3


class StatementRefused(Exception):
    """The gate refused the statement; ``reason`` is one plain sentence for the agent."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def check_statement(sql: str, ai_relations: frozenset[str], ai_columns: frozenset[str]) -> None:
    """Raise ``StatementRefused`` unless ``sql`` is one SELECT that stays inside the allow-list.

    ``ai_relations`` is the set of view names in the ``ai`` schema (the catalogue's relations);
    ``ai_columns`` is every column name those views (and the two search functions) return.
    """
    if not sql.strip():
        raise StatementRefused("The statement is empty.")
    if len(sql) > MAX_SQL_CHARS:
        raise StatementRefused(f"The statement is longer than {MAX_SQL_CHARS} characters.")
    try:
        statements = parse_sql(sql)
    except ParseError as exc:
        detail = " ".join(str(exc).split()).rstrip(".")
        raise StatementRefused(f"The statement could not be read: {detail}.") from None
    if not statements:
        raise StatementRefused("The statement is empty.")
    if len(statements) > 1:
        raise StatementRefused("Send one statement at a time.")
    stmt = statements[0].stmt
    if not isinstance(stmt, ast.SelectStmt):
        raise StatementRefused(f"Statement kind {type(stmt).__name__} is not allowed; only SELECT is.")
    _Gate(ai_relations, ai_columns, _declared_names(stmt)).walk(stmt, frozenset())


class _Gate:
    def __init__(
        self,
        ai_relations: frozenset[str],
        ai_columns: frozenset[str],
        aliased: frozenset[str],
    ) -> None:
        self._ai_relations = ai_relations
        self._ai_columns = ai_columns
        self._aliased = aliased

    def walk(self, value: object, ctes: frozenset[str]) -> None:
        """Check ``value`` and everything under it; ``ctes`` are the CTE names in scope here."""
        if value is None or isinstance(value, (str, int, float, bool, enum.Enum)):
            return
        if isinstance(value, (tuple, list)):
            for item in value:
                self.walk(item, ctes)
            return
        if not isinstance(value, ast.Node):
            raise StatementRefused(f"{type(value).__name__} is not allowed.")
        kind = type(value).__name__
        if kind not in ALLOWED_NODES:
            raise StatementRefused(f"{kind} is not allowed.")
        if isinstance(value, ast.SelectStmt):
            self._select(value, ctes)
            return
        if isinstance(value, ast.RangeVar):
            self._relation(value, ctes)
        elif isinstance(value, ast.RangeFunction):
            self._range_function(value)
        elif isinstance(value, ast.ColumnRef):
            self._column(value)
        elif isinstance(value, ast.FuncCall):
            self._function(value)
        elif isinstance(value, ast.TypeName):
            self._type(value)
        elif isinstance(value, ast.A_Expr):
            self._operator(value.name)
        elif isinstance(value, ast.SubLink):
            self._operator(value.operName)
        elif isinstance(value, ast.SortBy):
            self._operator(value.useOp)
        elif isinstance(value, ast.A_Indirection):
            # Inner first, so `((x).f).g` names the innermost call it would make.
            self.walk(value.arg, ctes)
            self._indirection(value.indirection)
        elif isinstance(value, ast.ResTarget):
            self._indirection(value.indirection)
        elif isinstance(value, ast.CollateClause):
            self._collation(value)
        for attr in value:
            self.walk(getattr(value, attr), ctes)

    def _select(self, node: ast.SelectStmt, ctes: frozenset[str]) -> None:
        if node.intoClause is not None:
            raise StatementRefused("SELECT ... INTO is not allowed.")
        if node.lockingClause:
            raise StatementRefused("Row locking clauses (FOR UPDATE, FOR SHARE) are not allowed.")
        body_ctes = ctes
        with_clause = node.withClause
        if with_clause is not None:
            if not isinstance(with_clause, ast.WithClause):
                raise StatementRefused(f"{type(with_clause).__name__} is not allowed.")
            entries = tuple(with_clause.ctes or ())
            names = [_cte_name(entry) for entry in entries]
            for index, entry in enumerate(entries):
                # SQL's own rule: a WITH RECURSIVE list sees all of itself; a plain WITH entry sees
                # only the entries before it. Anything else is a real relation, checked as one.
                visible = names if with_clause.recursive else names[:index]
                self.walk(entry, ctes | frozenset(visible))
            body_ctes = ctes | frozenset(names)
        # FROM first: it is what the rest of the query names, so its refusal is the useful one.
        self.walk(node.fromClause, body_ctes)
        for attr in node:
            if attr not in ("withClause", "fromClause"):
                self.walk(getattr(node, attr), body_ctes)

    def _relation(self, node: ast.RangeVar, ctes: frozenset[str]) -> None:
        parts = [p for p in (node.catalogname, node.schemaname, node.relname) if p]
        refused = StatementRefused(f"Relation {'.'.join(parts)} is not allowed.")
        if node.catalogname:
            raise refused
        if node.schemaname is None:
            if node.relname in self._ai_relations or node.relname in ctes:
                return
            raise refused
        if node.schemaname == _AI_SCHEMA and node.relname in self._ai_relations:
            return
        raise refused

    def _column(self, node: ast.ColumnRef) -> None:
        fields = node.fields or ()
        if len(fields) > _MAX_COLUMN_PARTS:
            shown = ".".join(f.sval if isinstance(f, ast.String) and f.sval else "*" for f in fields)
            raise StatementRefused(f"Column reference {shown} is not allowed.")
        last = fields[-1] if fields else None
        if isinstance(last, ast.A_Star):
            return
        names = _names(fields)
        # Postgres reads `q.name` as the call name(q) when q has no column `name`, so the last part
        # must be a real column: one of the ai views', or a name this statement gives a column.
        # (Every range entry's whole row is composite, see _range_function, so even a borrowed
        # name can reach only record/anyelement functions over the cook's own row.)
        if names[-1] not in self._ai_columns and names[-1] not in self._aliased:
            raise StatementRefused(f"Column {'.'.join(names)} is not allowed; it is not a column of any view or alias.")

    def _range_function(self, node: ast.RangeFunction) -> None:
        # A function in FROM that returns a scalar makes its alias a scalar whole-row value, and
        # Postgres reads `alias.f` as f(alias), which would reach any one-argument function. The
        # two ai search functions return rows, so they are the only functions FROM may hold.
        refused = StatementRefused("Only ai.semantic_search and ai.keyword_search may be used in FROM.")
        functions = tuple(node.functions or ())
        if node.is_rowsfrom or node.ordinality or node.coldeflist or len(functions) != 1:
            raise refused
        item = functions[0]
        if not isinstance(item, (tuple, list)) or not item or any(extra for extra in item[1:]):
            raise refused
        call = item[0]
        if not isinstance(call, ast.FuncCall):
            raise refused
        names = _names(call.funcname)
        if names[-1:] and names[-1] in AI_FUNCTIONS and (len(names) == 1 or names[:-1] == [_AI_SCHEMA]):
            return
        raise refused

    def _function(self, node: ast.FuncCall) -> None:
        names = _names(node.funcname)
        if names and names[-1] == "set_config":
            raise StatementRefused("set_config is never allowed.")
        if len(names) == 1 and names[0] in ALLOWED_FUNCTIONS | AI_FUNCTIONS:
            return
        if len(names) == 2:
            schema, name = names
            if schema == _CATALOG_SCHEMA and name in ALLOWED_FUNCTIONS:
                return
            if schema == _AI_SCHEMA and name in AI_FUNCTIONS:
                return
        raise StatementRefused(f"Function {'.'.join(names)} is not allowed.")

    def _type(self, node: ast.TypeName) -> None:
        names = _names(node.names)
        allowed = bool(names) and names[-1] in ALLOWED_TYPES
        if (len(names) == 2 and names[0] != _CATALOG_SCHEMA) or len(names) > 2:
            allowed = False
        if not allowed:
            raise StatementRefused(f"Type {'.'.join(names)} is not allowed.")

    def _collation(self, node: ast.CollateClause) -> None:
        names = _names(node.collname)
        if (len(names) == 2 and names[0] != _CATALOG_SCHEMA) or len(names) > 2:
            raise StatementRefused(f"Collation {'.'.join(names)} is not allowed.")

    def _indirection(self, parts: Sequence[object] | None) -> None:
        # `(x).name` is the call name(x) when x has no field `name`, on scalars too, so field
        # access is refused outright; subscripts and `.*` are all that may follow a parenthesis.
        for part in parts or ():
            if isinstance(part, ast.String):
                raise StatementRefused(f"Field access .{part.sval} is not allowed; select columns by name instead.")
            if not isinstance(part, (ast.A_Indices, ast.A_Star)):
                raise StatementRefused(f"{type(part).__name__} is not allowed after a parenthesis.")

    def _operator(self, name: Sequence[object] | None) -> None:
        names = _names(name)
        if len(names) > 1:
            raise StatementRefused(f"Qualified operator {'.'.join(names)} is not allowed; write it unqualified.")


def _names(value: Sequence[object] | None) -> list[str]:
    names: list[str] = []
    for item in value or ():
        if not isinstance(item, ast.String) or not isinstance(item.sval, str):
            raise StatementRefused(f"{type(item).__name__} is not allowed in a name.")
        names.append(item.sval)
    return names


def _declared_names(root: ast.Node) -> frozenset[str]:
    """Column names the statement itself declares.

    Output names (``AS x``), alias column lists (``t(n)``) and CTE column lists.
    """
    names: set[str] = set()

    def strings(values: Sequence[object] | None) -> list[str]:
        return [v.sval for v in values or () if isinstance(v, ast.String) and v.sval]

    def visit(value: object) -> None:
        if isinstance(value, (tuple, list)):
            for item in value:
                visit(item)
            return
        if not isinstance(value, ast.Node):
            return
        if isinstance(value, ast.ResTarget) and value.name:
            names.add(value.name)
        elif isinstance(value, ast.Alias):
            names.update(strings(value.colnames))
        elif isinstance(value, ast.CommonTableExpr):
            names.update(strings(value.aliascolnames))
        for attr in value:
            visit(getattr(value, attr))

    visit(root)
    return frozenset(names)


def _cte_name(entry: object) -> str:
    if not isinstance(entry, ast.CommonTableExpr) or not isinstance(entry.ctename, str):
        raise StatementRefused(f"{type(entry).__name__} is not allowed.")
    return entry.ctename
