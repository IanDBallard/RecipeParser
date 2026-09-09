"""RegenWorker: claim → REFINE → EMBED → guarded write-back (spec 5)."""
import asyncio
import logging
from typing import Any, Dict, List
from unittest.mock import MagicMock

from recipeparser.models import CayenneRefinement, StructuredIngredient, TokenizedDirection


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    """Records a chained supabase-py query and returns canned data on execute() —
    or raises the exception the fake was told to simulate for this table/rpc,
    standing in for a real postgrest-py APIError / network failure."""
    def __init__(self, fake, table):
        self.fake, self.table, self.ops = fake, table, []
    def __getattr__(self, name):
        def _op(*args, **kwargs):
            self.ops.append((name, args, kwargs))
            return self
        return _op
    def execute(self):
        self.fake.queries.append(self)
        exc = self.fake.raises.get(self.table)
        if exc is not None:
            raise exc
        return _Result(self.fake.responses.get(self.table, []))


class FakeSupabase:
    def __init__(self, responses: Dict[str, Any] = None, rpc_responses: Dict[str, Any] = None,
                 raises: Dict[str, Exception] = None):
        self.responses = responses or {}
        self.rpc_responses = rpc_responses or {}
        # table name (or "rpc:<name>") -> exception to raise from that query's execute().
        # Simulates infrastructure failures (APIError, network drop) distinct from an
        # application-level exception raised by refine_fn/embed_fn.
        self.raises = raises or {}
        self.queries: List[_Query] = []
        self.rpcs: List[tuple] = []
    def table(self, name):
        return _Query(self, name)
    def rpc(self, name, params):
        self.rpcs.append((name, params))
        q = _Query(self, f"rpc:{name}")
        self.responses[f"rpc:{name}"] = self.rpc_responses.get(name, [])
        return q


def _row(rev=3, rid="r1"):
    return {"id": rid, "user_id": "u1", "title": "Cake",
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


def test_double_encoded_body_column_is_recorded_not_regenerated():
    # A double-encoded jsonb column comes back as a str. Iterating it fed REFINE
    # one "ingredient" per character; the result then wrote back cleanly under
    # the body_rev guard, so the recipe was silently replaced by garbage with no
    # derived_error and no attempt counted. It must fail and be recorded.
    row = _row()
    row["ingredient_lines"] = '["1 cup flour", "2 eggs"]'
    fake = FakeSupabase(rpc_responses={"claim_stale_recipes": [row]},
                        responses={"recipes": [{"id": "r1"}]})
    refine_fn = MagicMock(return_value=_refinement())
    assert _worker(fake, refine_fn=refine_fn).run_once() == 1
    refine_fn.assert_not_called()                          # never reached Gemini
    assert _ops(fake, "recipes") == []                     # nothing written back
    failures = [p for n, p in fake.rpcs if n == "regen_failed"]
    assert len(failures) == 1
    assert failures[0]["p_id"] == "r1" and "must be a list" in failures[0]["p_msg"]


def test_batch_mixed_outcomes_do_not_abandon_other_rows():
    # Two claimed rows in one batch: the first fails REFINE, the second succeeds.
    # A per-row exception must not abort the rest of the claimed batch — each row
    # is isolated, so run_once() still reports both rows processed, r1 lands a
    # regen_failed call, and r2 still gets its guarded write-back.
    fake = FakeSupabase(
        rpc_responses={"claim_stale_recipes": [_row(rev=3, rid="r1"), _row(rev=5, rid="r2")]},
        responses={"recipes": [{"id": "r2"}]},
    )
    refine_fn = MagicMock(side_effect=[ValueError("bad tofu"), _refinement()])
    w = _worker(fake, refine_fn=refine_fn)
    assert w.run_once() == 2
    assert ("regen_failed", {"p_id": "r1", "p_msg": "bad tofu"}) in fake.rpcs
    recipe_ops = _ops(fake, "recipes")
    assert len(recipe_ops) == 1                            # only the surviving row wrote back
    assert ("eq", ("id", "r2"), {}) in recipe_ops[0]
    assert ("eq", ("body_rev", 5), {}) in recipe_ops[0]


def test_infrastructure_failure_on_write_back_is_caught_and_recorded():
    # .execute() itself raising (an APIError / network failure from the real
    # postgrest-py client) must be caught the same way an application-level
    # refine_fn exception is — recorded via regen_failed, not left to crash the run.
    fake = FakeSupabase(
        rpc_responses={"claim_stale_recipes": [_row(rev=3)]},
        responses={"recipes": [{"id": "r1"}]},
        raises={"recipes": RuntimeError("db unreachable")},
    )
    w = _worker(fake)
    assert w.run_once() == 1
    assert ("regen_failed", {"p_id": "r1", "p_msg": "db unreachable"}) in fake.rpcs


def test_regen_failed_rpc_failure_is_logged_not_raised(caplog):
    # regen_failed fails under exactly the conditions that broke the primary
    # call (RPC missing, Supabase down). Letting it escape _process re-raises
    # out of pool.map() and out of run_once(), so the row keeps claimed_at and
    # gains no derived_attempts: re-claimed every 5 minutes forever, burning a
    # REFINE and an EMBED call each time.
    fake = FakeSupabase(rpc_responses={"claim_stale_recipes": [_row()]},
                        raises={"rpc:regen_failed": RuntimeError("function does not exist")})
    w = _worker(fake, refine_fn=MagicMock(side_effect=ValueError("bad tokens")))
    assert w.run_once() == 1                                # did not blow up the batch
    assert ("regen_failed", {"p_id": "r1", "p_msg": "bad tokens"}) in fake.rpcs
    assert "could not record the failure for r1" in caplog.text


def test_regen_failed_rpc_failure_does_not_abandon_the_rest_of_the_batch():
    # The second row must still be processed after the first row's regen_failed
    # call itself fails.
    fake = FakeSupabase(
        rpc_responses={"claim_stale_recipes": [_row(rev=3, rid="r1"), _row(rev=5, rid="r2")]},
        responses={"recipes": [{"id": "r2"}]},
        raises={"rpc:regen_failed": RuntimeError("supabase down")},
    )
    w = _worker(fake, refine_fn=MagicMock(side_effect=[ValueError("bad tofu"), _refinement()]))
    assert w.run_once() == 2
    recipe_ops = _ops(fake, "recipes")
    assert len(recipe_ops) == 1
    assert ("eq", ("id", "r2"), {}) in recipe_ops[0]


def test_malformed_claimed_row_is_recorded_not_raised(caplog):
    # A claimed row missing body_rev used to raise from the unpack, outside the
    # try, straight out of pool.map() and run_once().
    fake = FakeSupabase(rpc_responses={"claim_stale_recipes": [{"id": "r9", "user_id": "u1"}]})
    assert _worker(fake).run_once() == 1
    assert ("regen_failed", {"p_id": "r9", "p_msg": "'body_rev'"}) in fake.rpcs
    assert "regen: r9 failed" in caplog.text


def test_claimed_row_without_an_id_is_logged_not_raised(caplog):
    fake = FakeSupabase(rpc_responses={"claim_stale_recipes": [{"user_id": "u1", "body_rev": 2}]})
    assert _worker(fake).run_once() == 1
    assert ("regen_failed", {"p_id": "?", "p_msg": "'id'"}) in fake.rpcs
    assert "regen: ? failed" in caplog.text


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
