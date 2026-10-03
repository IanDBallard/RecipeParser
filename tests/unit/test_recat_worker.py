"""RecatWorker: additive, scoped, cancellable bulk recategorise (spec 6)."""
import json
import uuid
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest


class _Result:
    def __init__(self, data, count=None):
        self.data, self.count = data, count


class _Query:
    """Records a chained supabase-py query and returns canned data on execute() —
    or raises the exception the fake was told to simulate for this table,
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
        handler = self.fake.handlers.get(self.table)
        return handler(self) if handler else _Result(self.fake.responses.get(self.table, []))


class FakeSupabase:
    def __init__(self):
        self.responses: Dict[str, Any] = {}
        self.handlers: Dict[str, Any] = {}
        # table name -> exception to raise from that query's execute().
        # Simulates infrastructure failures (APIError, network drop) distinct from
        # an application-level exception raised by categorize_fn.
        self.raises: Dict[str, Exception] = {}
        self.queries: List[_Query] = []
    def table(self, name):
        return _Query(self, name)


def _ops(fake, table):
    return [q.ops for q in fake.queries if q.table == table]


CATS = [
    {"id": "ax1", "name": "Cuisine", "parent_id": None},
    {"id": "t1", "name": "Italian", "parent_id": "ax1"},
    {"id": "t2", "name": "Thai", "parent_id": "ax1"},
    {"id": "ax2", "name": "Quick", "parent_id": None},
]


def test_resolve_new_axes():
    from recipeparser.adapters.recat_worker import resolve_new_axes
    axes, ids = resolve_new_axes(CATS, ["t2", "ax2"])
    assert axes == {"Cuisine": ["Thai"], "Quick": ["Quick"]}
    assert ids == {"Thai": "t2", "Quick": "ax2"}


def _fake_with_job(job_status_sequence, recipes, count, category_ids=("t2",)):
    fake = FakeSupabase()
    fake.responses["categories"] = CATS
    statuses = list(job_status_sequence)

    def jobs(q):
        names = [o[0] for o in q.ops]
        if names[0] == "select" and ("eq", ("status", "pending"), {}) in q.ops:
            return _Result([{"id": "j1", "user_id": "u1", "kind": "recategorize",
                             "status": "pending", "params": {"category_ids": list(category_ids)}}])
        if names[0] == "select":                       # cancel check
            return _Result([{"status": statuses.pop(0) if statuses else "running"}])
        if names[0] == "update" and ("eq", ("status", "pending"), {}) in q.ops:
            return _Result([{"id": "j1"}])            # claim succeeded
        return _Result([{"id": "j1"}])
    fake.handlers["ingestion_jobs"] = jobs

    def recipes_h(q):
        if any(o[0] == "select" and o[2].get("count") == "exact" for o in q.ops):
            return _Result([], count=count)
        cursor = next((o[1][1] for o in q.ops if o[0] == "gt"), None)
        page = [r for r in recipes if r["id"] > _uuid_cursor(cursor)][:10]
        return _Result(page)
    fake.handlers["recipes"] = recipes_h
    return fake


def _uuid_cursor(value):
    """
    recipes.id is a uuid column. PostgREST renders `.gt("id", "")` as `id=gt.`
    and Postgres then fails the whole page with
    `invalid input syntax for type uuid: ""`. The double must be no more
    permissive than the database, or an empty-string cursor looks like a valid
    floor here and only breaks in production.
    """
    if not isinstance(value, str):
        raise AssertionError(f"cursor must be a uuid string, got {value!r}")
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise ValueError(f'invalid input syntax for type uuid: "{value}"') from None
    return value


def _rid(i):
    """A real uuid that still sorts by index, so paging order is predictable."""
    return f"00000000-0000-4000-8000-{i:012d}"


def _recipes(n):
    return [{"id": _rid(i), "title": f"R{i}", "ingredient_lines": [], "direction_steps": []}
            for i in range(n)]


def _worker(fake, categorize_fn):
    from recipeparser.adapters.recat_worker import RecatWorker
    return RecatWorker(fake, gemini_client=MagicMock(), categorize_fn=categorize_fn, batch_size=10)


_TERMINAL = {"done", "error", "cancelled"}


def _finished(fake):
    """True once the job row has been given a terminal status."""
    return any(
        ops[0][0] == "update" and ops[0][1][0].get("status") in _TERMINAL
        for ops in _ops(fake, "ingestion_jobs")
    )


def _run_to_end(worker, fake, limit=50):
    """
    Poll until the job is finished; return how many polls it took. One poll is one
    batch (Fix Roadmap F-008), so a job spans several. Bounded, because the fake
    offers the same pending job for ever and a regression must fail, not hang.
    """
    for n in range(1, limit + 1):
        assert worker.run_once() == 1
        if _finished(fake):
            return n
    raise AssertionError(f"job not finished after {limit} polls")


def test_no_pending_job():
    fake = FakeSupabase()
    fake.responses["ingestion_jobs"] = []
    assert _worker(fake, MagicMock()).run_once() == 0


def test_job_runs_in_batches_and_inserts_additively():
    fake = _fake_with_job(["running"] * 5, _recipes(25), count=25)
    cat = MagicMock(side_effect=lambda recipes, axes, client, parents=None: {recipes[0]["id"]: ["Thai", "Nope"]})
    assert _run_to_end(_worker(fake, cat), fake) == 4             # 3 batches, then the empty page
    assert cat.call_count == 3                                   # 10 + 10 + 5
    assert cat.call_args.args[1] == {"Cuisine": ["Thai"]}        # only the new tag offered
    inserts = _ops(fake, "recipe_categories")
    assert len(inserts) == 3
    rows = inserts[0][0][1][0]
    assert rows == [{"id": rows[0]["id"], "recipe_id": _rid(0), "category_id": "t2", "user_id": "u1"}]
    assert inserts[0][0][2] == {"on_conflict": "recipe_id,category_id", "ignore_duplicates": True}
    final = _ops(fake, "ingestion_jobs")[-1][0][1][0]
    assert final["status"] == "done" and final["stage"] == "DONE" and final["progress_pct"] == 100
    assert final["recipe_count"] == 3


def test_a_whole_axis_job_shows_the_tree_and_writes_only_the_most_specific_tag():
    # Cayenne F-205: a nested tag reaches the model under its parent, and a parent
    # picked beside its child is not written; the axis row itself is never offered.
    fake = _fake_with_job(["running"] * 3, _recipes(1), count=1, category_ids=("ax1", "t1", "t2", "t3"))
    fake.responses["categories"] = CATS + [{"id": "t3", "name": "Isan", "parent_id": "t2"}]
    cat = MagicMock(side_effect=lambda recipes, axes, client, parents=None: {
        recipes[0]["id"]: ["Cuisine", "Thai", "Isan"]})
    _run_to_end(_worker(fake, cat), fake)
    assert cat.call_args.args[1] == {"Cuisine": ["Italian", "Thai", "Isan"]}
    assert cat.call_args.kwargs["parents"] == {"Isan": "Thai"}
    rows = _ops(fake, "recipe_categories")[0][0][1][0]
    assert [r["category_id"] for r in rows] == ["t3"]


def test_cancel_between_batches_finishes_the_job_as_cancelled():
    # The old assertion was only that "done" never appeared, which was true before
    # the fix as well: the worker returned without writing a terminal state at all,
    # leaving the row at stage CATEGORIZING and mid-flight progress -- exactly what
    # a worker that died looks like. What matters is that it FINISHES.
    fake = _fake_with_job(["running", "cancelled"], _recipes(25), count=25)
    cat = MagicMock(return_value={})
    assert _run_to_end(_worker(fake, cat), fake) == 2
    assert cat.call_count == 2
    final = _ops(fake, "ingestion_jobs")[-1][0][1][0]
    assert final["status"] == "cancelled"
    # DONE, not ERROR: a cook stopped it, it did not fail.
    assert final["stage"] == "DONE"
    # The counts it reached survive, so the screen can say "N recipes tagged so far".
    assert "recipe_count" in final and "progress_pct" in final
    statuses = [o[0][1][0].get("status") for o in _ops(fake, "ingestion_jobs") if o[0][0] == "update"]
    assert "done" not in statuses


def test_too_many_failed_batches_errors_the_job():
    fake = _fake_with_job(["running"] * 5, _recipes(30), count=30)
    cat = MagicMock(side_effect=RuntimeError("gemini down"))
    _run_to_end(_worker(fake, cat), fake)
    final = _ops(fake, "ingestion_jobs")[-1][0][1][0]
    assert final["status"] == "error" and "3 of 3 batches failed" in final["error_message"]


def test_infrastructure_failure_on_upsert_is_caught_and_recorded():
    # .execute() itself raising on the recipe_categories upsert (an APIError /
    # network failure from the real postgrest-py client) must be caught the
    # same way an application-level categorize_fn exception is — counted as a
    # failed batch, not left to crash the run.
    fake = _fake_with_job(["running"], _recipes(5), count=5)
    fake.raises["recipe_categories"] = RuntimeError("db unreachable")
    cat = MagicMock(return_value={_rid(0): ["Thai"]})
    _run_to_end(_worker(fake, cat), fake)
    final = _ops(fake, "ingestion_jobs")[-1][0][1][0]
    assert final["status"] == "error" and "1 of 1 batches failed" in final["error_message"]
    assert final["recipe_count"] == 0


def test_recipe_count_is_distinct_recipes_not_tag_rows_single_axis():
    # One recipe matching two tags WITHIN one axis must contribute 1 to
    # recipe_count, not 2 — recipe_count is "recipes processed", not "junction
    # rows inserted". filter_batch_result returns a list of tags per recipe,
    # so this already reproduces the overcount without needing multiple axes.
    fake = _fake_with_job(["running"], _recipes(1), count=1, category_ids=["t1", "t2"])
    cat = MagicMock(return_value={_rid(0): ["Italian", "Thai"]})
    _run_to_end(_worker(fake, cat), fake)
    rows = _ops(fake, "recipe_categories")[0][0][1][0]
    assert len(rows) == 2                                          # both tags inserted
    final = _ops(fake, "ingestion_jobs")[-1][0][1][0]
    assert final["status"] == "done" and final["recipe_count"] == 1  # but 1 recipe


def test_recipe_count_is_distinct_recipes_not_tag_rows_multi_axis():
    # Same overcount, but the two matched tags come from different axes
    # (Cuisine's Thai and the standalone Quick axis) — the scenario the plan
    # review named explicitly, on top of the simpler single-axis case above.
    fake = _fake_with_job(["running"], _recipes(1), count=1, category_ids=["t2", "ax2"])
    cat = MagicMock(return_value={_rid(0): ["Thai", "Quick"]})
    _run_to_end(_worker(fake, cat), fake)
    rows = _ops(fake, "recipe_categories")[0][0][1][0]
    assert len(rows) == 2
    final = _ops(fake, "ingestion_jobs")[-1][0][1][0]
    assert final["status"] == "done" and final["recipe_count"] == 1


def test_double_rejects_a_non_uuid_cursor():
    # Guards the guard: the fake must reject exactly what Postgres rejects.
    with pytest.raises(ValueError, match="invalid input syntax for type uuid"):
        _uuid_cursor("")


def test_fresh_job_pages_from_the_nil_uuid():
    # A job whose params carry no cursor must still send a valid uuid as the
    # page floor. `.gt("id", "")` renders as `id=gt.`, which Postgres fails with
    # `invalid input syntax for type uuid: ""` — so every fresh job died on its
    # first page and was marked 'error' before a single batch ran.
    from recipeparser.adapters.recat_worker import NIL_UUID
    fake = _fake_with_job(["running"] * 3, _recipes(15), count=15)
    cat = MagicMock(return_value={})
    _run_to_end(_worker(fake, cat), fake)
    cursors = [o[1][1] for ops in _ops(fake, "recipes") for o in ops if o[0] == "gt"]
    assert cursors[0] == NIL_UUID
    assert cursors[1] == _rid(9)                       # then the last id of page 1
    final = _ops(fake, "ingestion_jobs")[-1][0][1][0]
    assert final["status"] == "done" and final["progress_pct"] == 100


def test_real_categorize_batch_failure_errors_the_job():
    # The default categorize_fn is gemini.categorize_batch. It used to catch
    # Exception and return {}, so RecatWorker's per-batch handler never fired
    # for a Gemini failure: no rows, no exception, `failed` stayed 0, and a job
    # run against a completely broken Gemini finished 'done' at 100% with
    # recipe_count 0. test_too_many_failed_batches_errors_the_job injects a
    # raising categorize_fn — something the real function was constructed never
    # to do — so it passed either way. This one drives the real function with
    # only _call_with_retry patched.
    from recipeparser.adapters.recat_worker import RecatWorker
    fake = _fake_with_job(["running"] * 5, _recipes(30), count=30)
    worker = RecatWorker(fake, gemini_client=MagicMock(), batch_size=10)
    with patch("recipeparser.gemini._call_with_retry", side_effect=RuntimeError("gemini down")):
        _run_to_end(worker, fake)
    assert _ops(fake, "recipe_categories") == []
    final = _ops(fake, "ingestion_jobs")[-1][0][1][0]
    assert final["status"] == "error" and "3 of 3 batches failed" in final["error_message"]
    assert final["recipe_count"] == 0


def test_real_categorize_batch_success_finishes_the_job():
    # The other half: with the real categorize_batch and a well-formed reply the
    # job still completes and the tags land, so the fix above is not simply
    # "everything now fails".
    from recipeparser.adapters.recat_worker import RecatWorker
    fake = _fake_with_job(["running"], _recipes(2), count=2)
    reply = MagicMock(text=json.dumps({"results": [{"recipe_id": _rid(0), "tags": ["Thai"]},
                                                   {"recipe_id": _rid(1), "tags": []}]}))
    worker = RecatWorker(fake, gemini_client=MagicMock(), batch_size=10)
    with patch("recipeparser.gemini._call_with_retry", return_value=reply):
        _run_to_end(worker, fake)
    rows = _ops(fake, "recipe_categories")[0][0][1][0]
    assert [r["recipe_id"] for r in rows] == [_rid(0)]
    final = _ops(fake, "ingestion_jobs")[-1][0][1][0]
    assert final["status"] == "done" and final["recipe_count"] == 1


# ── Stage 7C: the four defects that bite on the first real run ──────────────

THREE_DEEP = [
    {"id": "A", "name": "Cuisine", "parent_id": None},
    {"id": "F", "name": "Asian", "parent_id": "A"},
    {"id": "L", "name": "Thai", "parent_id": "F"},
]


def test_resolve_new_axes_uses_the_root_not_the_immediate_parent():
    # The bug this fixes: ingest's _build_axes flattens every descendant into its
    # ROOT's axis, so it offered Thai under "Cuisine". resolve_new_axes read
    # parent_id once and offered the same tag under "Asian". Two descriptions of
    # one taxonomy, in two prompts that are meant to be interchangeable.
    from recipeparser.adapters.recat_worker import resolve_new_axes
    axes, ids = resolve_new_axes(THREE_DEEP, ["L"])
    assert axes == {"Cuisine": ["Thai"]}
    assert ids == {"Thai": "L"}


def test_resolve_new_axes_and_build_axes_agree_on_a_three_deep_tree():
    # The two paths must name the same axis for the same tag. This is the
    # assertion that would have caught the divergence, and it is why the walk
    # now lives in one module.
    from recipeparser.adapters.recat_worker import resolve_new_axes
    from recipeparser.core.taxonomy import axes_from_rows
    axes, _ = resolve_new_axes(THREE_DEEP, ["L"])
    ingest_axes = axes_from_rows(THREE_DEEP)
    assert list(axes) == ["Cuisine"]
    assert "Thai" in ingest_axes["Cuisine"]


def test_resolve_new_axes_raises_on_a_duplicate_name():
    # D6: the unique (user_id, name) index makes this unrepresentable today, so it
    # cannot fire yet. It is asserted now so that scoping names per axis fails
    # loudly instead of last-write-wins silently choosing a category.
    from recipeparser.adapters.recat_worker import resolve_new_axes
    rows = [{"id": "a", "name": "Quick", "parent_id": None},
            {"id": "b", "name": "Quick", "parent_id": None}]
    with pytest.raises(ValueError, match="Quick"):
        resolve_new_axes(rows, ["a", "b"])


def test_a_stale_running_job_is_reclaimed_and_resumes_from_its_cursor():
    # Without the lease a job a restart left `running` is never touched again: the
    # poll asks only for `pending`, so it sits at whatever progress it reached.
    fake = FakeSupabase()
    fake.responses["categories"] = CATS
    # updated_at is never null here: the stale poll filters on `updated_at < cutoff`.
    stale = {"id": "j1", "user_id": "u1", "kind": "recategorize", "status": "running",
             "updated_at": "2026-09-01T10:00:00+00:00",
             "params": {"category_ids": ["t2"], "cursor": _rid(9)}}

    def jobs(q):
        names = [o[0] for o in q.ops]
        if names[0] == "select" and ("eq", ("status", "pending"), {}) in q.ops:
            return _Result([])                                  # nothing waiting
        if names[0] == "select" and ("eq", ("status", "running"), {}) in q.ops:
            return _Result([stale])                             # the abandoned one
        if names[0] == "select":
            return _Result([{"status": "running"}])             # cancel check
        return _Result([{"id": "j1"}])
    fake.handlers["ingestion_jobs"] = jobs

    def recipes_h(q):
        if any(o[0] == "select" and o[2].get("count") == "exact" for o in q.ops):
            return _Result([], count=25)
        cursor = next((o[1][1] for o in q.ops if o[0] == "gt"), None)
        return _Result([r for r in _recipes(25) if r["id"] > _uuid_cursor(cursor)][:10])
    fake.handlers["recipes"] = recipes_h

    assert _worker(fake, MagicMock(return_value={})).run_once() == 1
    # Claimed by compare-and-swap on `running`, not on `pending`.
    claims = [o for o in _ops(fake, "ingestion_jobs")
              if o[0][0] == "update" and ("eq", ("status", "running"), {}) in o]
    assert claims, "a stale running job must be claimed on its running status"
    # It resumed rather than restarting: the first page asked for ids above the
    # stored cursor, not above the nil uuid.
    first_gt = next(o[1][1] for q in fake.queries if q.table == "recipes"
                    for o in q.ops if o[0] == "gt")
    assert first_gt == _rid(9)


def test_a_fresh_running_job_is_not_reclaimed():
    # Only a job past the lease is fair game; the query must carry the cutoff.
    fake = FakeSupabase()
    fake.responses["categories"] = CATS

    def jobs(q):
        if ("eq", ("status", "pending"), {}) in q.ops:
            return _Result([])
        return _Result([])                                       # nothing stale either
    fake.handlers["ingestion_jobs"] = jobs
    assert _worker(fake, MagicMock()).run_once() == 0
    stale_queries = [q for q in fake.queries if q.table == "ingestion_jobs"
                     and ("eq", ("status", "running"), {}) in q.ops]
    assert stale_queries, "the poll must look for a stale running job"
    assert any(o[0] == "lt" and o[1][0] == "updated_at" for o in stale_queries[0].ops), \
        "the stale query must be bounded by updated_at, or it would steal a live job"


def test_a_batch_that_fails_twice_is_recorded_in_skipped():
    fake = _fake_with_job(["running"], _recipes(5), count=5)
    cat = MagicMock(side_effect=RuntimeError("gemini down"))
    _run_to_end(_worker(fake, cat), fake)
    assert cat.call_count == 2, "the batch must be retried exactly once"
    final = _ops(fake, "ingestion_jobs")[-1][0][1][0]
    assert final["skipped_count"] == 5
    assert [e["recipe_id"] for e in final["skipped"]] == [_rid(i) for i in range(5)]
    assert "gemini down" in final["skipped"][0]["reason"]


def test_a_batch_that_fails_once_then_succeeds_is_not_recorded():
    fake = _fake_with_job(["running"], _recipes(5), count=5)
    cat = MagicMock(side_effect=[RuntimeError("blip"), {_rid(0): ["Thai"]}])
    _run_to_end(_worker(fake, cat), fake)
    assert cat.call_count == 2
    final = _ops(fake, "ingestion_jobs")[-1][0][1][0]
    assert final["status"] == "done"
    assert final["skipped_count"] == 0 and final["skipped"] == []
    assert final["recipe_count"] == 1


def test_a_job_whose_categories_have_all_gone_errors():
    # Finishing `done` here reads as "checked everything, nothing matched".
    # Nothing was checked at all.
    fake = _fake_with_job(["running"], _recipes(5), count=5, category_ids=("ghost",))
    assert _run_to_end(_worker(fake, MagicMock()), fake) == 1
    final = _ops(fake, "ingestion_jobs")[-1][0][1][0]
    assert final["status"] == "error"
    assert final["error_message"] == "The categories no longer exist"


def test_a_job_with_some_missing_categories_proceeds_on_the_rest():
    fake = _fake_with_job(["running"] * 3, _recipes(5), count=5, category_ids=("t2", "ghost"))
    cat = MagicMock(return_value={})
    _run_to_end(_worker(fake, cat), fake)
    assert cat.call_args.args[1] == {"Cuisine": ["Thai"]}     # the ghost is not offered
    final = _ops(fake, "ingestion_jobs")[-1][0][1][0]
    assert final["status"] == "done"


# ── Fix Roadmap F-008: one batch per poll ───────────────────────────────────
#
# run_once used to claim a job and run it to the end. run_workers awaits each
# worker in turn, so a large recategorise held the loop for the whole job:
# regeneration starved behind it and shutdown waited it out.


def _pending_polls(fake):
    return [q for q in fake.queries if q.table == "ingestion_jobs" and q.ops[0][0] == "select"
            and ("eq", ("status", "pending"), {}) in q.ops]


def test_one_poll_processes_one_batch():
    fake = _fake_with_job(["running"] * 5, _recipes(25), count=25)
    cat = MagicMock(return_value={_rid(0): ["Thai"]})
    worker = _worker(fake, cat)
    assert worker.run_once() == 1
    assert cat.call_count == 1, "one poll must hand back after one batch"
    assert not _finished(fake)
    progress = [o[0][1][0] for o in _ops(fake, "ingestion_jobs")
                if o[0][0] == "update" and "params" in o[0][1][0]]
    assert len(progress) == 1
    assert progress[0]["params"]["cursor"] == _rid(9)          # resumable from here
    assert progress[0]["progress_pct"] == 33 and progress[0]["recipe_count"] == 1


def test_the_next_poll_continues_the_held_job_from_its_cursor():
    from recipeparser.adapters.recat_worker import NIL_UUID
    fake = _fake_with_job(["running"] * 5, _recipes(25), count=25)
    cat = MagicMock(return_value={})
    worker = _worker(fake, cat)
    worker.run_once()
    claims = len(_pending_polls(fake))
    worker.run_once()
    assert len(_pending_polls(fake)) == claims, "a held job is continued, not claimed again"
    cursors = [o[1][1] for ops in _ops(fake, "recipes") for o in ops if o[0] == "gt"]
    assert cursors == [NIL_UUID, _rid(9)]
    assert cat.call_count == 2


def test_counts_carry_across_polls():
    # matched, skipped and the failed-batch tally live across polls, so the final
    # done/error verdict is the one the job would have reached in a single call.
    fake = _fake_with_job(["running"] * 5, _recipes(25), count=25)
    outcomes = [{_rid(0): ["Thai"]}, RuntimeError("down"), RuntimeError("down"), {_rid(20): ["Thai"]}]
    cat = MagicMock(side_effect=outcomes)
    _run_to_end(_worker(fake, cat), fake)
    final = _ops(fake, "ingestion_jobs")[-1][0][1][0]
    # 1 failed batch of 3 is more than a tenth: error, with both matches and the
    # ten skipped ids of the middle batch kept.
    assert final["status"] == "error" and "1 of 3 batches failed" in final["error_message"]
    assert final["recipe_count"] == 2
    assert final["skipped_count"] == 10
    assert [e["recipe_id"] for e in final["skipped"]] == [_rid(i) for i in range(10, 20)]


def test_a_crash_mid_job_errors_it_and_frees_the_worker():
    # The progress write itself failing is an infrastructure fault, as before: the
    # job ends `error`, and the worker lets go of it instead of continuing it.
    fake = _fake_with_job(["running"] * 5, _recipes(25), count=25)
    base = fake.handlers["ingestion_jobs"]
    calls = {"n": 0}

    def jobs(q):
        if q.ops[0][0] == "update" and "params" in q.ops[0][1][0]:
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("db unreachable")
        return base(q)
    fake.handlers["ingestion_jobs"] = jobs
    worker = _worker(fake, MagicMock(return_value={}))
    worker.run_once()
    worker.run_once()
    final = _ops(fake, "ingestion_jobs")[-1][0][1][0]
    assert final["status"] == "error" and "db unreachable" in final["error_message"]
    polls = len(_pending_polls(fake))
    worker.run_once()
    assert len(_pending_polls(fake)) == polls + 1, "the next poll looks for new work"


# ── Fix Roadmap batch 17: reclaim, resume and the terminal write ────────────

_STALE_TS = "2026-09-01T10:00:00.123456+00:00"


class _JobRow:
    """
    A stateful ingestion_jobs row. An UPDATE applies only when every `.eq` filter
    matches the row as it is NOW, and answers with the rows it changed -- which is
    what PostgREST's `return=representation` gives supabase-py, and what the
    worker's compare-and-swap writes read as "won" or "lost".
    """
    def __init__(self, **row):
        self.row = dict(row)
        self.stale_snapshot = dict(row)            # what a stale-lease poll read

    def handler(self, q):
        names = [o[0] for o in q.ops]
        if names[0] == "select" and ("eq", ("status", "pending"), {}) in q.ops:
            return _Result([])
        if names[0] == "select" and ("eq", ("status", "running"), {}) in q.ops:
            return _Result([dict(self.stale_snapshot)])
        if names[0] == "select":
            return _Result([{"status": self.row["status"]}])
        payload = q.ops[0][1][0]
        filters = [o[1] for o in q.ops if o[0] == "eq"]
        if all(self.row.get(col) == val for col, val in filters):
            self.row.update(payload)
            return _Result([dict(self.row)])
        return _Result([])


def _stale_fake(row, recipes, total):
    fake = FakeSupabase()
    fake.responses["categories"] = CATS
    fake.handlers["ingestion_jobs"] = row.handler

    def recipes_h(q):
        if any(o[0] == "select" and o[2].get("count") == "exact" for o in q.ops):
            upto = next((o[1][1] for o in q.ops if o[0] == "lte"), None)
            if upto is None:
                return _Result([], count=total)
            return _Result([], count=sum(1 for r in recipes if r["id"] <= _uuid_cursor(upto)))
        cursor = next((o[1][1] for o in q.ops if o[0] == "gt"), None)
        return _Result([r for r in recipes if r["id"] > _uuid_cursor(cursor)][:10])
    fake.handlers["recipes"] = recipes_h
    return fake


def _stale_row(**over):
    row = {"id": "j1", "user_id": "u1", "kind": "recategorize", "status": "running",
           "stage": "CATEGORIZING", "updated_at": _STALE_TS,
           "params": {"category_ids": ["t2"], "cursor": _rid(9)},
           "progress_pct": 33, "recipe_count": 4, "skipped": [], "skipped_count": 0}
    row.update(over)
    return _JobRow(**row)


def test_two_workers_reclaiming_one_stale_job_cannot_both_win_it():
    # F-112. Both read the same stale row. The first claim rewrites status to the
    # `running` it already was, so a swap on status alone lets the second win too
    # and two workers tag the same library side by side. The swap must also match
    # the `updated_at` that was read, which the first claim has since moved.
    row = _stale_row()
    fake = _stale_fake(row, _recipes(25), 25)
    first = _worker(fake, MagicMock(return_value={}))._claim()
    second = _worker(fake, MagicMock(return_value={}))._claim()
    assert first is not None
    assert second is None, "the second reclaim of the same stale row must lose"
    claim = next(q for q in fake.queries if q.table == "ingestion_jobs" and q.ops[0][0] == "update")
    assert ("eq", ("updated_at", _STALE_TS), {}) in claim.ops


def test_a_reclaimed_job_resumes_its_count_and_progress():
    # F-111. The job reached recipe 10 of 25 (one batch of three, 4 recipes tagged)
    # before its worker died. The reclaim resumed from the cursor but counted from
    # 0, so the 4 recipes tagged before the restart dropped out of recipe_count and
    # the bar went back to 33% for what is really the second batch.
    row = _stale_row()
    fake = _stale_fake(row, _recipes(25), 25)
    cat = MagicMock(return_value={_rid(10): ["Thai"]})
    assert _worker(fake, cat).run_once() == 1
    assert row.row["recipe_count"] == 5                  # 4 before the restart + 1 now
    assert row.row["progress_pct"] == 66                 # batch 2 of 3, not batch 1
    assert row.row["params"]["cursor"] == _rid(19)


def test_a_reclaimed_job_keeps_its_failed_batches_in_the_verdict():
    # F-111, the verdict's half: a batch that failed before the restart is in
    # `skipped` (ten ids), so it still counts against the job. Without it the
    # resumed job divides only its own failures by every batch the job ran.
    skipped = [{"recipe_id": _rid(i), "reason": "down"} for i in range(10)]
    row = _stale_row(recipe_count=0, skipped=skipped, skipped_count=10)
    fake = _stale_fake(row, _recipes(25), 25)
    worker = _worker(fake, MagicMock(return_value={}))
    for _ in range(5):
        worker.run_once()
        if row.row["status"] != "running":
            break
    assert row.row["status"] == "error"
    assert "1 of 3 batches failed" in row.row["error_message"]


def test_a_cancel_after_the_last_check_is_not_overwritten_by_done():
    # F-113. The cook presses Stop after the worker's last cancel check but before
    # it writes `done`. The terminal write must move the job only out of
    # `running`, so the cancel stands -- and the job still finishes coherently:
    # stage DONE, its counts intact, so the screen says "Stopped. N recipes
    # tagged so far" rather than showing a job stuck mid-flight.
    row = _stale_row(params={"category_ids": ["t2"], "cursor": _rid(19)},
                     progress_pct=66, recipe_count=2)
    fake = _stale_fake(row, _recipes(25), 25)
    cat = MagicMock(return_value={_rid(20): ["Thai"]})
    worker = _worker(fake, cat)
    assert worker.run_once() == 1                        # the last batch; check says running
    row.row["status"] = "cancelled"                      # Stop lands here
    assert worker.run_once() == 1                        # the empty page: finish
    assert row.row["status"] == "cancelled"
    assert row.row["stage"] == "DONE"
    assert row.row["recipe_count"] == 3
    assert row.row["progress_pct"] == 99                 # where it stopped, not 100
    assert worker.run_once() == 0                        # and the worker let go of it
