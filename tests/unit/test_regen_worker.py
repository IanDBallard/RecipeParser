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


def _worker(fake, refine_fn=None, embed_fn=None, clock=None):
    from recipeparser.adapters.regen_worker import RegenWorker
    extra = {"clock": clock} if clock else {}
    return RegenWorker(
        fake, gemini_client=MagicMock(),
        refine_fn=refine_fn or MagicMock(return_value=_refinement()),
        embed_fn=embed_fn or MagicMock(return_value=[0.5] * 3),
        axes_loader=lambda user_id: {"Cuisine": ["Italian"]},
        batch=5, concurrency=1, **extra,
    )


def _ops(fake, table):
    return [q.ops for q in fake.queries if q.table == table]


def test_claims_with_batch_size():
    fake = FakeSupabase(rpc_responses={"claim_stale_recipes": []})
    assert _worker(fake).run_once() == 0
    assert fake.rpcs == [("claim_stale_recipes", {"p_limit": 5})]


def test_success_writes_back_with_guard():
    row = {**_row(rev=3), "source_url": "https://www.taste.com.au/recipes/scones"}
    fake = FakeSupabase(rpc_responses={"claim_stale_recipes": [row]}, responses={"recipes": [{"id": "r1"}]})
    refine_fn = MagicMock(return_value=_refinement())
    w = _worker(fake, refine_fn=refine_fn)
    assert w.run_once() == 1
    # REFINE got the row's text and its host, and no reader preference (D7)
    kwargs = refine_fn.call_args.kwargs
    assert kwargs["source_host"] == "taste.com.au"
    assert "uom_system" not in kwargs and "measure_preference" not in kwargs
    assert kwargs["user_axes"] == {"Cuisine": ["Italian"]}
    assert refine_fn.call_args.args[0].ingredients == ["1 cup flour"]
    assert not any(q.table == "profiles" for q in fake.queries)
    # write-back guarded by id AND body_rev
    ops = _ops(fake, "recipes")[0]
    names = [o[0] for o in ops]
    assert names == ["update", "eq", "eq"]
    assert ops[0][1][0]["derived_rev"] == 3
    assert ops[0][1][0]["embedding"] == [0.5] * 3
    assert ("eq", ("id", "r1"), {}) in ops and ("eq", ("body_rev", 3), {}) in ops
    assert not any(n == "regen_failed" for n, _ in fake.rpcs)


def test_a_row_without_a_source_url_refines_with_no_host():
    # Review Focus 5: pasted text and books have no host; host-only evidence is then dropped by refine().
    fake = FakeSupabase(rpc_responses={"claim_stale_recipes": [_row()]}, responses={"recipes": [{"id": "r1"}]})
    refine_fn = MagicMock(return_value=_refinement())
    _worker(fake, refine_fn=refine_fn).run_once()
    assert refine_fn.call_args.kwargs["source_host"] is None


def test_the_detected_system_is_written_back():
    refinement = _refinement()
    refinement.source_uom_system_detected = "AU"
    refinement.source_uom_system_evidence = "1 cup (250 ml)"
    fake = FakeSupabase(rpc_responses={"claim_stale_recipes": [_row()]}, responses={"recipes": [{"id": "r1"}]})
    _worker(fake, refine_fn=MagicMock(return_value=refinement)).run_once()
    payload = _ops(fake, "recipes")[0][0][1][0]
    assert (payload["source_uom_system_detected"], payload["source_uom_system_evidence"]) == ("AU", "1 cup (250 ml)")


def test_zero_rows_updated_is_not_a_failure(caplog):
    caplog.set_level(logging.INFO, logger="recipeparser.adapters.regen_worker")
    fake = FakeSupabase(rpc_responses={"claim_stale_recipes": [_row()]},
                        responses={"recipes": []})       # body_rev moved during the run
    assert _worker(fake).run_once() == 1
    assert not any(n == "regen_failed" for n, _ in fake.rpcs)
    assert "edited again" in caplog.text


def test_edited_again_releases_the_claim():
    # Fix Roadmap F-006. The result is dropped because the cook edited the recipe
    # mid-run, but claimed_at was left set, so the NEW revision waited out the
    # whole five-minute lease before it was regenerated. The claim is released as
    # soon as the stale result is dropped.
    fake = FakeSupabase(rpc_responses={"claim_stale_recipes": [_row(rev=3)]},
                        responses={"recipes": []})       # body_rev moved during the run
    assert _worker(fake).run_once() == 1
    ops = _ops(fake, "recipes")
    assert len(ops) == 2, "the guarded write-back, then the release"
    release = ops[1]
    assert release[0] == ("update", ({"claimed_at": None},), {})
    assert ("eq", ("id", "r1"), {}) in release
    assert not any(n == "regen_failed" for n, _ in fake.rpcs)


def test_edited_again_leaves_the_claim_alone_once_the_lease_may_have_passed():
    # The release is unconditional on the row, so it is only safe while no one
    # else can hold a claim on it -- inside our own lease. Past that, another
    # worker may have claimed the new revision, and clearing its claim would let
    # a third run start. The row is claimable again anyway, so nothing is lost.
    fake = FakeSupabase(rpc_responses={"claim_stale_recipes": [_row(rev=3)]},
                        responses={"recipes": []})
    ticks = iter([0.0, 290.0])                            # claimed at 0 s, dropped at 290 s
    assert _worker(fake, clock=lambda: next(ticks)).run_once() == 1
    assert len(_ops(fake, "recipes")) == 1                 # the write-back only


def test_a_failed_release_is_logged_not_recorded_as_a_regen_failure(caplog):
    # regen_failed counts an attempt against the row's CURRENT body_rev -- the new
    # revision, which has not failed at all. A release that cannot be written just
    # leaves the row to its lease.
    class _ReleaseFails(FakeSupabase):
        def table(self, name):
            q = super().table(name)
            real_execute = q.execute
            def execute():
                if q.ops and q.ops[0] == ("update", ({"claimed_at": None},), {}):
                    raise RuntimeError("db unreachable")
                return real_execute()
            q.execute = execute
            return q
    fake = _ReleaseFails(rpc_responses={"claim_stale_recipes": [_row(rev=3)]},
                         responses={"recipes": []})
    assert _worker(fake).run_once() == 1
    assert not any(n == "regen_failed" for n, _ in fake.rpcs)
    assert "could not release the claim on r1" in caplog.text


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


def test_null_ingredient_lines_is_recorded_not_regenerated_to_nothing():
    # Fix Roadmap F-005: a null body column used to regenerate to empty derived
    # ingredients and write back as a success. It must go to derived_error via
    # regen_failed (attempt-capped) with nothing written and no Gemini spend.
    row = _row()
    row["ingredient_lines"] = None
    fake = FakeSupabase(rpc_responses={"claim_stale_recipes": [row]},
                        responses={"recipes": [{"id": "r1"}]})
    refine_fn = MagicMock(return_value=_refinement())
    assert _worker(fake, refine_fn=refine_fn).run_once() == 1
    refine_fn.assert_not_called()
    assert _ops(fake, "recipes") == []
    failures = [p for n, p in fake.rpcs if n == "regen_failed"]
    assert len(failures) == 1 and "ingredient_lines is null" in failures[0]["p_msg"]


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


def test_run_workers_polls_again_at_once_while_a_recat_job_is_held():
    # Fix Roadmap F-008 makes one recategorise poll one batch. Sleeping the full
    # poll interval after every round would then add ten seconds per batch, so a
    # worker that opts in (RecatWorker) keeps the loop busy while it has work.
    from recipeparser.adapters.regen_worker import run_workers
    rounds = []
    class Busy:
        keeps_loop_busy = True
        def run_once(self):
            rounds.append(1)
            if len(rounds) == 3:
                stop.set()
            return 1
    stop = asyncio.Event()
    asyncio.run(asyncio.wait_for(run_workers([Busy()], stop, poll_seconds=60), timeout=5))
    assert len(rounds) == 3


def test_run_workers_interleaves_workers_between_polls():
    # Both workers get a turn every round, so a long recategorise job no longer
    # holds regeneration behind it.
    from recipeparser.adapters.regen_worker import run_workers
    order = []
    class W:
        def __init__(self, name, work):
            self.name, self.work = name, work
            self.keeps_loop_busy = name == "recat"
        def run_once(self):
            order.append(self.name)
            if len(order) == 6:
                stop.set()
            return self.work
    stop = asyncio.Event()
    asyncio.run(asyncio.wait_for(
        run_workers([W("regen", 0), W("recat", 1)], stop, poll_seconds=60), timeout=5))
    assert order == ["regen", "recat"] * 3


def test_run_workers_records_each_workers_last_good_poll():
    # Fix Roadmap F-009: /health's regen_workers is set once at boot, so it cannot
    # say whether either worker is still polling. run_workers stamps each worker's
    # last poll that completed; one that keeps failing keeps its old stamp.
    from recipeparser.adapters.regen_worker import run_workers
    n = {"count": 0}
    class RegenWorker:
        def run_once(self):
            n["count"] += 1
            if n["count"] == 2:
                stop.set()
            return 0
    class RecatWorker:
        def run_once(self):
            raise RuntimeError("supabase down")
    polls = {}
    stop = asyncio.Event()
    asyncio.run(run_workers([RegenWorker(), RecatWorker()], stop, poll_seconds=0.01, polls=polls))
    assert list(polls) == ["RegenWorker"]
    assert polls["RegenWorker"].endswith("+00:00")          # an aware UTC timestamp


def test_run_workers_still_sleeps_between_rounds_of_a_regen_backlog():
    # RegenWorker.run_once returns the rows it claimed, so a backlog returns
    # nonzero on every poll. Letting that keep the loop busy would drain a
    # library-wide re-derive (~1,600 recipes) back to back, with Gemini's quota
    # and cost resting only on _call_with_retry's backoff. It must keep its pause.
    import time

    from recipeparser.adapters.recat_worker import RecatWorker
    from recipeparser.adapters.regen_worker import RegenWorker, run_workers
    assert getattr(RegenWorker, "keeps_loop_busy", False) is False
    assert RecatWorker.keeps_loop_busy is True
    stamps = []
    class RegenLike:
        def run_once(self):
            stamps.append(time.monotonic())
            if len(stamps) == 3:
                stop.set()
            return 5                                        # a full claim every poll
    stop = asyncio.Event()
    asyncio.run(asyncio.wait_for(run_workers([RegenLike()], stop, poll_seconds=0.2), timeout=5))
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert len(gaps) == 2 and all(g >= 0.15 for g in gaps), gaps
