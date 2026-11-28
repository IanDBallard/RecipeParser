"""Host detector for mandate ``fail-loud.quota-cancel-propagate``.

A job cancellation and an exhausted daily quota are not failures of one recipe or one
chunk: ``JobCancelledError`` ends the job as 'cancelled' and ``DailyQuotaExhaustedError``
stops the whole run (``recipeparser/exceptions.py``). A broad handler that catches them
and carries on records a skip or an unreadable file instead. This detector reports a
handler that would catch one of them and neither re-raises it nor follows an earlier
handler of the same ``try`` that re-raises it.

The rule, per ``except`` clause (``try`` and ``try … except*`` alike):

* The clause *catches* a listed exception when it is bare, or names ``Exception``,
  ``BaseException`` or an ``ancestors`` entry, alone or in a tuple. A clause that names
  a listed exception itself is deliberate handling and is never reported.
* A catching clause is clean when its body re-raises: a bare ``raise``, or
  ``raise <bound name>``, anywhere in the body outside a nested function, class or
  lambda. A bare ``raise`` inside a nested ``except`` re-raises the inner exception, so
  it does not count. A conditional re-raise counts; the check is syntactic.
* Or when every listed exception is covered by an earlier clause of the same ``try``
  that names it or an ``ancestors`` entry and re-raises by the rule above.
* Re-raising as another type (``raise X from exc``) does not let it through: the
  caller's ``except JobCancelledError`` would no longer match.

Scope: a clause is reported when its ``except`` line was added (``added_lines``). With
``added_lines`` None (the edit hook) the whole file is checked; an empty set (a change
with only deletions) checks nothing. ``params.roots`` limits the check to paths under
those directories.

The rule lives in ``uncovered_handlers``, which needs only the standard library, so the
project's tests run it without the ``acf`` package; ``check`` is the entry point ACF
calls and wraps each result as an ``acf.detectors.host.HostHit``.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any

_BROAD = frozenset({"Exception", "BaseException"})
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _str_list(params: Mapping[str, Any], key: str, *, required: bool) -> list[str]:
    value = params.get(key)
    if value is None and not required:
        return []
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v for v in value):
        raise ValueError(f"params.{key} must be a non-empty list of names, got {value!r}")
    return list(value)


def _names(node: ast.expr | None) -> set[str]:
    """The last segment of every class a clause's type names."""
    if node is None:
        return set()
    if isinstance(node, ast.Tuple):
        return set().union(*(_names(e) for e in node.elts))
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Attribute):
        return {node.attr}
    return set()


def _walk_body(nodes: Iterable[ast.AST], nested_handler: bool = False) -> Iterator[tuple[ast.AST, bool]]:
    """Every node of a clause body outside nested scopes, with whether it sits in a nested ``except``."""
    for node in nodes:
        if isinstance(node, _SCOPES):
            continue
        yield node, nested_handler
        for child in ast.iter_child_nodes(node):
            yield from _walk_body([child], nested_handler or isinstance(child, ast.ExceptHandler))


def _reraises(handler: ast.ExceptHandler) -> bool:
    for node, nested in _walk_body(handler.body):
        if not isinstance(node, ast.Raise):
            continue
        if node.exc is None and not nested:
            return True
        if handler.name and isinstance(node.exc, ast.Name) and node.exc.id == handler.name:
            return True
    return False


def uncovered_handlers(
    tree: ast.Module,
    added_lines: frozenset[int] | None,
    exceptions: Sequence[str],
    ancestors: Sequence[str] = (),
) -> list[tuple[int, list[str]]]:
    """(line of the ``except``, the listed exceptions it swallows) for every reported clause."""
    listed = list(exceptions)
    narrow = set(ancestors)
    found: list[tuple[int, list[str]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Try, ast.TryStar)):
            continue
        covered: set[str] = set()
        for handler in node.handlers:
            named = _names(handler.type)
            reraises = _reraises(handler)
            if named & set(listed):
                if reraises:
                    covered.update(named & set(listed))
                continue
            via_ancestor = bool(named & narrow)
            catches = handler.type is None or bool(named & _BROAD) or via_ancestor
            if catches and not reraises:
                missing = [e for e in listed if e not in covered]
                in_scope = added_lines is None or handler.lineno in added_lines
                if missing and in_scope:
                    found.append((handler.lineno, missing))
            if via_ancestor and reraises and not (named & _BROAD) and handler.type is not None:
                covered.update(listed)
    return found


def _under(path: str, roots: Sequence[str]) -> bool:
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)


def check(
    text: str,
    path: str,
    added_lines: frozenset[int] | None,
    *,
    tree: ast.Module | None,
    params: Mapping[str, Any] | None,
) -> list[Any]:
    """ACF entry point (``detector: config/architecture/detectors/quota_cancel.py:check``)."""
    from acf.detectors.host import HostHit  # noqa: PLC0415 - only the gate has acf installed

    params = params or {}
    exceptions = _str_list(params, "exceptions", required=True)
    ancestors = _str_list(params, "ancestors", required=False)
    roots = _str_list(params, "roots", required=True)
    if tree is None or not _under(path, roots):
        return []
    return [
        HostHit(
            path=path,
            line=line,
            message=(
                f"Handler catches {', '.join(missing)} without re-raising: add "
                f"`except ({', '.join(missing)}): raise` before it, or re-raise here"
            ),
        )
        for line, missing in uncovered_handlers(tree, added_lines, exceptions, ancestors)
    ]
