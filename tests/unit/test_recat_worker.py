"""RecatWorker: additive, scoped, cancellable bulk recategorise (spec 6)."""
import uuid
from typing import Any, Dict, List
from unittest.mock import MagicMock

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


def test_no_pending_job():
    fake = FakeSupabase()
    fake.responses["ingestion_jobs"] = []
    assert _worker(fake, MagicMock()).run_once() == 0


def test_job_runs_in_batches_and_inserts_additively():
    fake = _fake_with_job(["running"] * 5, _recipes(25), count=25)
    cat = MagicMock(side_effect=lambda recipes, axes, client: {recipes[0]["id"]: ["Thai", "Nope"]})
    assert _worker(fake, cat).run_once() == 1
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


def test_cancel_between_batches():
    fake = _fake_with_job(["running", "cancelled"], _recipes(25), count=25)
    cat = MagicMock(return_value={})
    _worker(fake, cat).run_once()
    assert cat.call_count == 2
    statuses = [o[0][1][0].get("status") for o in _ops(fake, "ingestion_jobs") if o[0][0] == "update"]
    assert "done" not in statuses


def test_too_many_failed_batches_errors_the_job():
    fake = _fake_with_job(["running"] * 5, _recipes(30), count=30)
    cat = MagicMock(side_effect=RuntimeError("gemini down"))
    _worker(fake, cat).run_once()
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
    assert _worker(fake, cat).run_once() == 1
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
    assert _worker(fake, cat).run_once() == 1
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
    assert _worker(fake, cat).run_once() == 1
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
    assert _worker(fake, cat).run_once() == 1
    cursors = [o[1][1] for ops in _ops(fake, "recipes") for o in ops if o[0] == "gt"]
    assert cursors[0] == NIL_UUID
    assert cursors[1] == _rid(9)                       # then the last id of page 1
    final = _ops(fake, "ingestion_jobs")[-1][0][1][0]
    assert final["status"] == "done" and final["progress_pct"] == 100
