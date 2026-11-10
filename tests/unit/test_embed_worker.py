"""EmbedWorker: claim_stale_embeddings -> embed -> guarded write-back (Cayenne AI search §6)."""
from typing import Any, Dict, List
from unittest.mock import MagicMock

from recipeparser.core.stages.embed import build_embedding_text


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, fake, table):
        self.fake, self.table, self.ops = fake, table, []

    def __getattr__(self, name):
        def _op(*args, **kwargs):
            self.ops.append((name, args, kwargs))
            return self
        return _op

    def execute(self):
        self.fake.queries.append(self)
        if self.table.startswith("rpc:"):
            return _Result(self.fake.rpc_responses.get(self.table[4:], []))
        responses = self.fake.update_responses
        return _Result(responses.pop(0) if responses else [{"id": "ok"}])


class FakeSupabase:
    def __init__(self, rows=None, update_responses=None):
        self.rpc_responses = {"claim_stale_embeddings": rows or []}
        self.update_responses: List[Any] = list(update_responses or [])
        self.queries: List[_Query] = []
        self.rpcs: List[tuple] = []

    def table(self, name):
        return _Query(self, name)

    def rpc(self, name, params):
        self.rpcs.append((name, params))
        return _Query(self, f"rpc:{name}")


def _row(rid="r1", rev=4):
    return {"id": rid, "user_id": "u1", "title": "Cake", "ingredient_lines": ["1 cup flour"],
            "direction_steps": ["Mix."], "category_names": ["Baking"], "embed_rev": rev}


def _worker(fake, embed_text_fn=None, **kwargs):
    from recipeparser.adapters.embed_worker import EmbedWorker
    return EmbedWorker(fake, MagicMock(),
                       embed_text_fn=embed_text_fn or MagicMock(return_value=[0.1] * 1536),
                       concurrency=1, **kwargs)


def _updates(fake):
    return [q.ops for q in fake.queries if q.table == "recipes"]


def test_claims_embeds_and_writes():
    fake = FakeSupabase(rows=[_row()])
    fn = MagicMock(return_value=[0.1] * 1536)
    assert _worker(fake, embed_text_fn=fn).run_once() == 1
    assert fake.rpcs == [("claim_stale_embeddings", {"p_limit": 10})]
    fn.assert_called_once()
    assert fn.call_args.args[0] == build_embedding_text("Cake", ["Baking"], ["1 cup flour"], ["Mix."])
    ups = _updates(fake)
    assert len(ups) == 1
    payload = ups[0][0][1][0]
    assert payload == {"embedding": [0.1] * 1536, "embedded_rev": 4, "embed_claimed_at": None}
    assert ("eq", ("id", "r1"), {}) in ups[0] and ("eq", ("embed_rev", 4), {}) in ups[0]
    assert not any(n == "embed_failed" for n, _ in fake.rpcs)


def test_write_dropped_when_rev_moved():
    fake = FakeSupabase(rows=[_row()], update_responses=[[], [{"id": "r1"}]])
    assert _worker(fake).run_once() == 1
    assert not any(n == "embed_failed" for n, _ in fake.rpcs)
    ups = _updates(fake)
    assert len(ups) == 2
    assert ups[1][0][1][0] == {"embed_claimed_at": None}
    assert ("eq", ("id", "r1"), {}) in ups[1]
    assert not any(o[1][0] == "embed_rev" for o in ups[1] if o[0] == "eq")


def test_failure_recorded():
    fake = FakeSupabase(rows=[_row("a"), _row("b")])
    calls = {"n": 0}

    def fn(text, client):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return [0.2] * 1536

    assert _worker(fake, embed_text_fn=fn).run_once() == 2
    assert ("embed_failed", {"p_id": "a"}) in fake.rpcs
    assert len(_updates(fake)) == 1  # b still written


def test_failure_recording_never_escapes():
    fake = FakeSupabase(rows=[_row()])

    def rpc(name, params):
        fake.rpcs.append((name, params))
        if name == "embed_failed":
            raise RuntimeError("down")
        return _Query(fake, f"rpc:{name}")
    fake.rpc = rpc
    assert _worker(fake, embed_text_fn=MagicMock(side_effect=RuntimeError("x"))).run_once() == 1


def test_paused_owner_skipped():
    fake = FakeSupabase(rows=[_row()])
    fn = MagicMock()
    assert _worker(fake, embed_text_fn=fn, ai_allowed=lambda _u: False).run_once() == 1
    fn.assert_not_called()
    assert _updates(fake) == []
    assert not any(n == "embed_failed" for n, _ in fake.rpcs)


def test_wrong_dims_is_a_failure():
    fake = FakeSupabase(rows=[_row()])
    assert _worker(fake, embed_text_fn=MagicMock(return_value=[0.1] * 1000)).run_once() == 1
    assert ("embed_failed", {"p_id": "r1"}) in fake.rpcs
    assert _updates(fake) == []


def test_empty_queue():
    fake = FakeSupabase(rows=[])
    fn = MagicMock()
    assert _worker(fake, embed_text_fn=fn).run_once() == 0
    fn.assert_not_called()
