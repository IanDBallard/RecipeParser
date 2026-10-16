from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from recipeparser.adapters import ai_gate
from recipeparser.adapters.ai_gate import AiGate


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _sb(*, paused=False, row=None, consume=True):
    sb = MagicMock()

    def table(name):
        q = MagicMock()
        data = [{"paused": paused}] if name == "ai_settings" else ([row] if row else [])
        q.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = data
        return q

    sb.table.side_effect = table
    sb.rpc.return_value.execute.return_value.data = consume
    return sb


ROW = {"recipes_processed": 10, "recipe_cap": 2000, "exempt": False}


def test_allows_when_not_paused_and_under_cap() -> None:
    AiGate(lambda: _sb(row=ROW)).check("u1", imports=True)


def test_paused_refuses_503_unless_exempt() -> None:
    with pytest.raises(HTTPException) as e:
        AiGate(lambda: _sb(paused=True, row=ROW)).check("u1")
    assert (e.value.status_code, e.value.detail) == (503, ai_gate.PAUSED_DETAIL)
    AiGate(lambda: _sb(paused=True, row={**ROW, "exempt": True})).check("u1", imports=True)


def test_at_cap_refuses_imports_429_but_not_other_ai() -> None:
    gate = AiGate(lambda: _sb(row={**ROW, "recipes_processed": 2000}))
    with pytest.raises(HTTPException) as e:
        gate.check("u1", imports=True)
    assert (e.value.status_code, e.value.detail) == (429, "You've used all 2000 of your beta recipe imports.")
    gate.check("u1")  # a picture or a classify is not capped (D4)


def test_exempt_passes_the_cap() -> None:
    AiGate(lambda: _sb(row={**ROW, "recipes_processed": 5000, "exempt": True})).check("u1", imports=True)


def test_missing_row_fails_closed_as_paused() -> None:
    with pytest.raises(HTTPException) as e:
        AiGate(lambda: _sb(row=None)).check("u1")
    assert e.value.status_code == 503


def test_unconfigured_database_allows() -> None:
    gate = AiGate(lambda: None)
    gate.check("u1", imports=True, embed=True)
    assert gate.consume_recipe("u1") is True


def test_paused_is_cached_for_30_seconds() -> None:
    clock = _Clock()
    state = {"paused": False}
    sb = MagicMock()

    def table(name):
        q = MagicMock()
        data = [{"paused": state["paused"]}] if name == "ai_settings" else [ROW]
        q.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = data
        return q

    sb.table.side_effect = table
    gate = AiGate(lambda: sb, clock=clock)
    assert gate.paused() is False
    state["paused"] = True
    clock.t = 29.0
    assert gate.paused() is False
    clock.t = 30.5
    assert gate.paused() is True


def test_embed_rate_limit_is_60_a_minute_per_user() -> None:
    clock = _Clock()
    gate = AiGate(lambda: _sb(row=ROW), clock=clock)
    for _ in range(60):
        gate.check("u1", embed=True)
    with pytest.raises(HTTPException) as e:
        gate.check("u1", embed=True)
    assert (e.value.status_code, e.value.detail) == (429, ai_gate.EMBED_RATE_DETAIL)
    gate.check("u2", embed=True)  # another user is unaffected
    clock.t = 60.1
    gate.check("u1", embed=True)  # the window rolled


def test_consume_true_false_and_rpc_error_fails_closed() -> None:
    assert AiGate(lambda: _sb(row=ROW, consume=True)).consume_recipe("u1") is True
    assert AiGate(lambda: _sb(row=ROW, consume=False)).consume_recipe("u1") is False
    sb = _sb(row=ROW)
    sb.rpc.return_value.execute.side_effect = RuntimeError("down")
    assert AiGate(lambda: sb).consume_recipe("u1") is False


def test_a_settings_read_error_counts_as_paused() -> None:
    sb = MagicMock()
    sb.table.side_effect = RuntimeError("down")
    assert AiGate(lambda: sb).paused() is True


def test_exempt_passes_the_embed_check_while_paused() -> None:
    AiGate(lambda: _sb(paused=True, row={**ROW, "exempt": True})).check("u1", embed=True)


def test_ai_allowed_matrix() -> None:
    assert AiGate(lambda: _sb(paused=False, row=ROW)).ai_allowed("u1") is True
    assert AiGate(lambda: _sb(paused=True, row={**ROW, "exempt": True})).ai_allowed("u1") is True
    assert AiGate(lambda: _sb(paused=True, row=ROW)).ai_allowed("u1") is False
    assert AiGate(lambda: _sb(paused=True, row=None)).ai_allowed("u1") is False


def test_ai_allowed_is_false_when_paused_and_the_allowance_read_raises() -> None:
    sb = _sb(paused=True, row=ROW)
    real_table = sb.table.side_effect

    def table(name):
        if name == "ai_allowance":
            raise RuntimeError("down")
        return real_table(name)

    sb.table.side_effect = table
    assert AiGate(lambda: sb).ai_allowed("u1") is False


def test_a_raising_client_factory_fails_closed() -> None:
    def boom():
        raise RuntimeError("no client")

    gate = AiGate(boom)
    with pytest.raises(HTTPException) as e:
        gate.check("u1", imports=True)
    assert e.value.status_code == 503
    assert gate.paused() is True
    assert gate.consume_recipe("u1") is False
