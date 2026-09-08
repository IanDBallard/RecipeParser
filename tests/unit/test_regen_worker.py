"""RegenWorker: claim → REFINE → EMBED → guarded write-back (spec 5)."""
import asyncio
import logging
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from recipeparser.models import CayenneRefinement, StructuredIngredient, TokenizedDirection


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    """Records a chained supabase-py query and returns canned data on execute()."""
    def __init__(self, fake, table):
        self.fake, self.table, self.ops = fake, table, []
    def __getattr__(self, name):
        def _op(*args, **kwargs):
            self.ops.append((name, args, kwargs))
            return self
        return _op
    def execute(self):
        self.fake.queries.append(self)
        return _Result(self.fake.responses.get(self.table, []))


class FakeSupabase:
    def __init__(self, responses: Dict[str, Any] = None, rpc_responses: Dict[str, Any] = None):
        self.responses = responses or {}
        self.rpc_responses = rpc_responses or {}
        self.queries: List[_Query] = []
        self.rpcs: List[tuple] = []
    def table(self, name):
        return _Query(self, name)
    def rpc(self, name, params):
        self.rpcs.append((name, params))
        q = _Query(self, f"rpc:{name}")
        self.responses[f"rpc:{name}"] = self.rpc_responses.get(name, [])
        return q


def _row(rev=3):
    return {"id": "r1", "user_id": "u1", "title": "Cake",
            "ingredient_lines": ["1 cup flour"], "direction_steps": ["Mix."], "body_rev": rev}


def _refinement():
    return CayenneRefinement(
        title="Cake", base_servings=4,
        structured_ingredients=[StructuredIngredient(id="ing_01", name="flour",
                                                     fallback_string="1 cup flour", line_index=0)],
        tokenized_directions=[TokenizedDirection(step=1, text="Mix {{ing_01|flour}}.")],
    )


def _worker(fake, refine_fn=None, embed_fn=None):
    from recipeparser.adapters.regen_worker import RegenWorker
    return RegenWorker(
        fake, gemini_client=MagicMock(),
        refine_fn=refine_fn or MagicMock(return_value=_refinement()),
        embed_fn=embed_fn or MagicMock(return_value=[0.5] * 3),
        axes_loader=lambda user_id: {"Cuisine": ["Italian"]},
        batch=5, concurrency=1,
    )


def _ops(fake, table):
    return [q.ops for q in fake.queries if q.table == table]


def test_claims_with_batch_size():
    fake = FakeSupabase(rpc_responses={"claim_stale_recipes": []})
    assert _worker(fake).run_once() == 0
    assert fake.rpcs == [("claim_stale_recipes", {"p_limit": 5})]


def test_success_writes_back_with_guard():
    fake = FakeSupabase(
        rpc_responses={"claim_stale_recipes": [_row(rev=3)]},
        responses={"profiles": [{"uom_system": "Metric", "measure_preference": "Weight"}],
                   "recipes": [{"id": "r1"}]},
    )
    refine_fn = MagicMock(return_value=_refinement())
    w = _worker(fake, refine_fn=refine_fn)
    assert w.run_once() == 1
    # REFINE got the row's text and the profile prefs
    kwargs = refine_fn.call_args.kwargs
    assert kwargs["uom_system"] == "Metric" and kwargs["measure_preference"] == "Weight"
    assert kwargs["user_axes"] == {"Cuisine": ["Italian"]}
    assert refine_fn.call_args.args[0].ingredients == ["1 cup flour"]
    # write-back guarded by id AND body_rev
    ops = _ops(fake, "recipes")[0]
    names = [o[0] for o in ops]
    assert names == ["update", "eq", "eq"]
    assert ops[0][1][0]["derived_rev"] == 3
    assert ops[0][1][0]["embedding"] == [0.5] * 3
    assert ("eq", ("id", "r1"), {}) in ops and ("eq", ("body_rev", 3), {}) in ops
    assert not any(n == "regen_failed" for n, _ in fake.rpcs)


def test_profile_defaults_when_missing():
    fake = FakeSupabase(rpc_responses={"claim_stale_recipes": [_row()]},
                        responses={"recipes": [{"id": "r1"}]})
    refine_fn = MagicMock(return_value=_refinement())
    _worker(fake, refine_fn=refine_fn).run_once()
    assert refine_fn.call_args.kwargs["uom_system"] == "US"
    assert refine_fn.call_args.kwargs["measure_preference"] == "Volume"


def test_zero_rows_updated_is_not_a_failure(caplog):
    caplog.set_level(logging.INFO, logger="recipeparser.adapters.regen_worker")
    fake = FakeSupabase(rpc_responses={"claim_stale_recipes": [_row()]},
                        responses={"recipes": []})       # body_rev moved during the run
    assert _worker(fake).run_once() == 1
    assert not any(n == "regen_failed" for n, _ in fake.rpcs)
    assert "edited again" in caplog.text


def test_exception_records_failure():
    fake = FakeSupabase(rpc_responses={"claim_stale_recipes": [_row()]})
    w = _worker(fake, refine_fn=MagicMock(side_effect=ValueError("bad tokens")))
    assert w.run_once() == 1
    assert ("regen_failed", {"p_id": "r1", "p_msg": "bad tokens"}) in fake.rpcs
    assert _ops(fake, "recipes") == []                    # no write-back attempted


def test_run_workers_loops_until_stopped():
    from recipeparser.adapters.regen_worker import run_workers
    calls = []
    class W:
        def run_once(self):
            calls.append(1)
            if len(calls) >= 2:
                stop.set()
            return 0
    stop = asyncio.Event()
    asyncio.run(run_workers([W()], stop, poll_seconds=0.01))
    assert len(calls) == 2


def test_run_workers_survives_exceptions(caplog):
    from recipeparser.adapters.regen_worker import run_workers
    n = {"count": 0}
    class W:
        def run_once(self):
            n["count"] += 1
            if n["count"] == 1:
                raise RuntimeError("supabase down")
            stop.set()
            return 0
    stop = asyncio.Event()
    asyncio.run(run_workers([W()], stop, poll_seconds=0.01))
    assert n["count"] == 2 and "supabase down" in caplog.text
