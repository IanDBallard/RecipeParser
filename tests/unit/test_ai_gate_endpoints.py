"""Every AI endpoint is gated and attributed; nothing else is gated."""
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from recipeparser.adapters import ai_gate, api


@pytest.fixture()
def client(monkeypatch):
    api.app.dependency_overrides[api._verify_supabase_jwt] = lambda: {"sub": "u1"}
    yield TestClient(api.app, raise_server_exceptions=False)
    api.app.dependency_overrides.clear()


def _refuse(code, detail):
    def check(user_id, *, imports=False, embed=False):
        raise HTTPException(code, detail=detail)
    return check


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("post", "/embed", {"text": "cake"}),
        ("post", "/jobs", {"text": "Boil water."}),
        ("post", "/jobs/recategorize", {"category_ids": ["c1"]}),
        ("post", "/recipes/r1/image/generate", {"title": "Cake"}),
        ("post", "/shopping/classify", {"ingredients": [{"key": "k", "text": "1 egg"}], "known_foods": []}),
    ],
)
def test_paused_refuses_every_ai_endpoint_with_503(client, monkeypatch, method, path, body) -> None:
    monkeypatch.setattr(api._ai_gate, "check", _refuse(503, ai_gate.PAUSED_DETAIL))
    resp = getattr(client, method)(path, json=body)
    assert (resp.status_code, resp.json()["detail"]) == (503, ai_gate.PAUSED_DETAIL)


def test_file_jobs_are_gated_as_imports(client, monkeypatch) -> None:
    seen = {}

    def check(user_id, *, imports=False, embed=False):
        seen.update(imports=imports, embed=embed)
        raise HTTPException(429, detail="You've used all 5 of your beta recipe imports.")

    monkeypatch.setattr(api._ai_gate, "check", check)
    resp = client.post("/jobs/file", files={"file": ("a.txt", b"x", "text/plain")})
    assert resp.status_code == 429
    assert seen == {"imports": True, "embed": False}


def test_embed_is_gated_with_the_rate_limit(client, monkeypatch) -> None:
    seen = {}
    monkeypatch.setattr(api._ai_gate, "check", lambda user_id, *, imports=False, embed=False: seen.update(imports=imports, embed=embed))
    with patch("recipeparser.adapters.api._get_client", return_value=MagicMock()), \
         patch("recipeparser.gemini.get_embeddings", return_value=[0.0] * 1536):
        assert client.post("/embed", json={"text": "cake"}).status_code == 200
    assert seen == {"imports": False, "embed": True}


def test_status_and_cancel_are_not_gated(client, monkeypatch) -> None:
    monkeypatch.setattr(api._ai_gate, "check", _refuse(503, ai_gate.PAUSED_DETAIL))
    assert client.get("/jobs/nope").status_code == 404
    assert client.post("/jobs/nope/cancel").status_code == 404


def test_embed_records_under_search(client, monkeypatch) -> None:
    monkeypatch.setattr(api._ai_gate, "check", lambda *a, **k: None)
    captured = []
    real = api.usage.attributed

    def spy(user_id, action, job_id=None, *, get_client):
        captured.append((user_id, action))
        return real(user_id, action, job_id, get_client=get_client)

    monkeypatch.setattr(api.usage, "attributed", spy)
    with patch("recipeparser.adapters.api._get_client", return_value=MagicMock()), \
         patch("recipeparser.gemini.get_embeddings", return_value=[0.0] * 1536):
        client.post("/embed", json={"text": "cake"})
    assert captured == [("u1", "search")]


def test_each_endpoint_uses_its_own_gate_variant(client, monkeypatch) -> None:
    calls = []

    def check(user_id, *, imports=False, embed=False):
        calls.append((imports, embed))
        raise HTTPException(503, detail=ai_gate.PAUSED_DETAIL)

    monkeypatch.setattr(api._ai_gate, "check", check)
    expected = {
        "/jobs": (True, False),
        "/jobs/file": (True, False),
        "/embed": (False, True),
        "/jobs/recategorize": (False, False),
        "/recipes/r1/image/generate": (False, False),
        "/shopping/classify": (False, False),
    }
    for path, flags in expected.items():
        calls.clear()
        if path == "/jobs/file":
            client.post(path, files={"file": ("a.txt", b"x", "text/plain")})
        else:
            client.post(path, json={"text": "x", "title": "x"})
        assert calls == [flags], path


def _real_gate(*, exempt: bool) -> ai_gate.AiGate:
    sb = MagicMock()

    def table(name):
        q = MagicMock()
        data = [{"paused": True}] if name == "ai_settings" else [
            {"recipes_processed": 9999, "recipe_cap": 5, "exempt": exempt}
        ]
        q.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = data
        return q

    sb.table.side_effect = table
    return ai_gate.AiGate(lambda: sb)


@pytest.mark.parametrize("path", ["/embed", "/jobs/recategorize", "/jobs/file"])
def test_exempt_user_passes_every_dependency_while_paused(client, monkeypatch, path) -> None:
    # /embed -> _require_ai_embed, /jobs/recategorize -> _require_ai, /jobs/file -> _require_ai_import
    monkeypatch.setattr(api, "_ai_gate", _real_gate(exempt=True))
    if path == "/jobs/file":
        resp = client.post(path, files={"file": ("a.txt", b"x", "text/plain")})
    else:
        resp = client.post(path, json={"text": "cake", "category_ids": []})
    assert resp.status_code != 503 or resp.json().get("detail") != ai_gate.PAUSED_DETAIL
    assert resp.status_code != 429


@pytest.mark.parametrize("path", ["/embed", "/jobs/recategorize", "/jobs/file"])
def test_non_exempt_user_is_refused_by_every_dependency_while_paused(client, monkeypatch, path) -> None:
    monkeypatch.setattr(api, "_ai_gate", _real_gate(exempt=False))
    if path == "/jobs/file":
        resp = client.post(path, files={"file": ("a.txt", b"x", "text/plain")})
    else:
        resp = client.post(path, json={"text": "cake", "category_ids": []})
    assert (resp.status_code, resp.json()["detail"]) == (503, ai_gate.PAUSED_DETAIL)


# ---------------------------------------------------------------------------
# A Cayenne restore bypasses the import gate (owner ruling, 2026-10-15)
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402
from contextlib import nullcontext  # noqa: E402

from recipeparser.adapters.ai_gate import AiGate  # noqa: E402
from recipeparser.io.category_sources import restore_paths as rp  # noqa: E402
from recipeparser.core.fsm import PipelineController  # noqa: E402
from recipeparser.core.models import Chunk, InputType  # noqa: E402


def _fake_db(*, paused=False, processed=0, cap=10, exempt=False):
    sb = MagicMock()

    def table(name):
        q = MagicMock()
        data = [{"paused": paused}] if name == "ai_settings" else [
            {"recipes_processed": processed, "recipe_cap": cap, "exempt": exempt}
        ]
        q.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = data
        return q

    sb.table.side_effect = table
    return sb


def _restore_chunk() -> Chunk:
    return Chunk(text="", input_type=InputType.PAPRIKA_CAYENNE, pre_parsed=MagicMock(), pre_parsed_embedding=[0.0])


def _flow_a_chunk() -> Chunk:
    return Chunk(text="Cake. Mix.", input_type=InputType.PAPRIKA_LEGACY)


def _upload(client, monkeypatch, name, tmp_path=None, **db):
    """POST /jobs/file with a real gate over a fake database; the job itself is recorded, not run."""
    monkeypatch.setattr(api, "_ai_gate", AiGate(lambda: _fake_db(**db)))
    started = []

    async def fake_run(*args, **kwargs):
        started.append((args, kwargs))

    monkeypatch.setattr(api, "_run_ingestion_job", fake_run)
    monkeypatch.setattr(api, "_create_ingestion_job", lambda *a, **k: None)
    if tmp_path is not None:  # the stub never reads, and the read owns the unlink
        monkeypatch.setattr(api.tempfile, "tempdir", str(tmp_path))
    resp = client.post("/jobs/file", files={"file": (name, b"x", "application/octet-stream")})
    return resp, started


@pytest.mark.parametrize("name", ["lib.paprikarecipes", "LIB.PaprikaRecipes"])
@pytest.mark.parametrize("db", [{"paused": True}, {"processed": 10, "cap": 10}])
def test_a_paprikarecipes_upload_is_not_refused_at_the_endpoint(client, monkeypatch, tmp_path, name, db) -> None:
    resp, started = _upload(client, monkeypatch, name, tmp_path, **db)
    assert resp.status_code == 202
    assert len(started) == 1
    assert started[0][1].get("gate_after_read") is True


def test_a_pdf_at_the_cap_is_still_refused_at_the_endpoint(client, monkeypatch, tmp_path) -> None:
    resp, started = _upload(client, monkeypatch, "book.pdf", tmp_path, processed=10, cap=10)
    assert (resp.status_code, resp.json()["detail"]) == (429, ai_gate.CAP_DETAIL.format(cap=10))
    assert started == []


def test_a_paprikarecipes_upload_still_needs_a_valid_jwt(monkeypatch, tmp_path) -> None:
    def reject():
        raise HTTPException(401, detail="bad token")

    api.app.dependency_overrides[api._verify_supabase_jwt] = reject
    try:
        anon = TestClient(api.app, raise_server_exceptions=False)
        resp, started = _upload(anon, monkeypatch, "lib.paprikarecipes", tmp_path)
    finally:
        api.app.dependency_overrides.clear()
    assert resp.status_code == 401
    assert started == []


def _run_deferred(monkeypatch, chunks, **db):
    """Run api._run_ingestion_job as a deferred job; return (pipeline_cls, finalized)."""
    monkeypatch.setattr(api, "_ai_gate", AiGate(lambda: _fake_db(**db)))
    finalized = []
    monkeypatch.setattr(api, "_finalize_ingestion_job", lambda jid, payload: finalized.append(payload))
    monkeypatch.setattr(api, "_update_total_chunks", lambda *a, **k: None)
    monkeypatch.setattr(api, "_make_progress_writer", lambda jid: (lambda pct: None))
    monkeypatch.setattr(api.usage, "attributed", lambda *a, **k: nullcontext())
    with patch.object(api, "RecipePipeline") as pipeline_cls, \
         patch.object(api, "SupabaseCategorySource") as cat_src, \
         patch.object(api, "SupabaseImageStore"):
        cat_src.return_value.load_category_ids.return_value = {}

        async def _read():
            return MagicMock(), chunks

        asyncio.run(
            api._run_ingestion_job(
                "job-1", "user-1", PipelineController(), _read, "lib.paprikarecipes", "File job",
                gate_after_read=True,
            )
        )
    return pipeline_cls, finalized


@pytest.mark.parametrize("db", [{"paused": True}, {"processed": 10, "cap": 10}])
def test_a_restore_only_file_runs_at_the_cap_and_while_paused(monkeypatch, db) -> None:
    pipeline_cls, finalized = _run_deferred(monkeypatch, [_restore_chunk(), _restore_chunk()], **db)
    pipeline_cls.return_value.run.assert_called_once()
    assert finalized[0]["status"] == "done"


def test_a_flow_a_file_at_the_cap_ends_in_the_cap_sentence(monkeypatch) -> None:
    pipeline_cls, finalized = _run_deferred(monkeypatch, [_flow_a_chunk()], processed=10, cap=10)
    pipeline_cls.return_value.run.assert_not_called()
    assert (finalized[0]["status"], finalized[0]["error_message"]) == ("error", ai_gate.CAP_DETAIL.format(cap=10))


def test_a_flow_a_file_while_paused_ends_in_the_paused_sentence(monkeypatch) -> None:
    pipeline_cls, finalized = _run_deferred(monkeypatch, [_flow_a_chunk()], paused=True)
    pipeline_cls.return_value.run.assert_not_called()
    assert (finalized[0]["status"], finalized[0]["error_message"]) == ("error", ai_gate.PAUSED_DETAIL)


def test_one_ai_chunk_among_restores_is_enough_to_refuse(monkeypatch) -> None:
    pipeline_cls, finalized = _run_deferred(monkeypatch, [_restore_chunk(), _flow_a_chunk()], paused=True)
    pipeline_cls.return_value.run.assert_not_called()
    assert finalized[0]["error_message"] == ai_gate.PAUSED_DETAIL


def test_an_exempt_user_runs_a_flow_a_file_paused_and_over_the_cap(monkeypatch) -> None:
    pipeline_cls, finalized = _run_deferred(
        monkeypatch, [_flow_a_chunk()], paused=True, processed=50, cap=10, exempt=True
    )
    pipeline_cls.return_value.run.assert_called_once()
    assert finalized[0]["status"] == "done"


def test_an_unreadable_allowance_fails_a_flow_a_file_closed_as_paused(monkeypatch) -> None:
    sb = MagicMock()
    sb.table.side_effect = RuntimeError("db down")
    monkeypatch.setattr(api, "_ai_gate", AiGate(lambda: sb))
    finalized = []
    monkeypatch.setattr(api, "_finalize_ingestion_job", lambda jid, payload: finalized.append(payload))
    monkeypatch.setattr(api.usage, "attributed", lambda *a, **k: nullcontext())
    with patch.object(api, "RecipePipeline") as pipeline_cls, patch.object(api, "SupabaseCategorySource"):
        async def _read():
            return MagicMock(), [_flow_a_chunk()]

        asyncio.run(
            api._run_ingestion_job("j", "u", PipelineController(), _read, "lib.paprikarecipes", "File job",
                                   gate_after_read=True)
        )
    pipeline_cls.return_value.run.assert_not_called()
    assert finalized[0]["error_message"] == ai_gate.PAUSED_DETAIL


def test_needs_ai_is_the_stage_decision_that_sets_ai_processed() -> None:
    from recipeparser.core.pipeline import RecipePipeline, chunk_needs_ai

    pipeline = RecipePipeline(client=MagicMock(), controller=PipelineController(), category_source=MagicMock())
    odd = [
        _restore_chunk(),
        Chunk(text="", input_type=InputType.PAPRIKA_CAYENNE, pre_parsed=MagicMock()),
        Chunk(text="", input_type=InputType.PAPRIKA_CAYENNE),
        _flow_a_chunk(),
        Chunk(text="x", input_type=InputType.PAPRIKA_LEGACY, pre_parsed=MagicMock()),
    ]
    for chunk in odd:
        assert chunk_needs_ai(chunk) == ("REFINE" in pipeline._get_stages(chunk))
    assert [chunk_needs_ai(c) for c in odd] == [False, False, False, True, True]


def _embedding_less_restore() -> Chunk:
    return Chunk(text="", input_type=InputType.PAPRIKA_CAYENNE, pre_parsed=MagicMock())


def test_an_embedding_less_restore_while_paused_ends_in_the_paused_sentence(monkeypatch) -> None:
    pipeline_cls, finalized = _run_deferred(monkeypatch, [_embedding_less_restore()], paused=True)
    pipeline_cls.return_value.run.assert_not_called()
    assert (finalized[0]["status"], finalized[0]["error_message"]) == ("error", ai_gate.PAUSED_DETAIL)


def test_an_embedding_less_restore_at_the_cap_still_runs(monkeypatch) -> None:
    pipeline_cls, finalized = _run_deferred(monkeypatch, [_embedding_less_restore()], processed=10, cap=10)
    pipeline_cls.return_value.run.assert_called_once()
    assert finalized[0]["status"] == "done"


def test_chunk_calls_ai_is_any_stage_but_assemble() -> None:
    from recipeparser.core.pipeline import chunk_calls_ai

    assert [chunk_calls_ai(c) for c in (_restore_chunk(), _embedding_less_restore(), _flow_a_chunk())] == [False, True, True]


# ── Categories created on restore (plan B task 4) ────────────────────────────

def _cayenne_chunk(paths, grid=None, title="Cake") -> Chunk:
    from recipeparser.models import CayenneRecipe

    pre = CayenneRecipe(
        title=title, structured_ingredients=[], tokenized_directions=[],
        category_paths=paths, grid_categories=grid or {},
    )
    return Chunk(text="", input_type=InputType.PAPRIKA_CAYENNE, pre_parsed=pre, pre_parsed_embedding=[0.0])


def _drive_restore(monkeypatch, chunks, ensure, calls):
    monkeypatch.setattr(api, "ensure_category_paths", ensure)
    monkeypatch.setattr(api, "_get_creds", lambda: ("https://x", "k"))
    monkeypatch.setattr(api, "_ai_gate", AiGate(lambda: _fake_db()))
    finalized = []
    monkeypatch.setattr(api, "_finalize_ingestion_job", lambda jid, payload: finalized.append(payload))
    monkeypatch.setattr(api, "_update_total_chunks", lambda *a, **k: None)
    monkeypatch.setattr(api, "_make_progress_writer", lambda jid: (lambda pct: None))
    monkeypatch.setattr(api.usage, "attributed", lambda *a, **k: nullcontext())
    with patch.object(api, "RecipePipeline") as pipeline_cls, \
         patch.object(api, "SupabaseCategorySource") as cat_src, \
         patch.object(api, "SupabaseImageStore"):
        cat_src.return_value.load_category_ids.side_effect = lambda uid: calls.append("load") or {}

        async def _read():
            return MagicMock(), chunks

        asyncio.run(api._run_ingestion_job(
            "job-1", "user-1", PipelineController(), _read, "lib.paprikarecipes", "File job", gate_after_read=True,
        ))
    return pipeline_cls, finalized


def test_run_ingestion_job_creates_categories_before_loading_ids(monkeypatch) -> None:
    calls = []

    def ensure(uid, paths, *, supabase_url, service_key):
        calls.append("ensure")
        assert uid == "user-1"
        return {tuple(p): p[-1] for p in paths}

    chunk = _cayenne_chunk([["Course", "Dessert", "Cake"], ["Course", "Soup"]])
    pipeline_cls, finalized = _drive_restore(monkeypatch, [chunk], ensure, calls)
    assert calls == ["ensure", "load"]
    assert chunk.pre_parsed.grid_categories == {"restore": ["Cake", "Soup"]}
    assert finalized[0]["status"] == "done"


def test_v1_archive_links_through_grid_categories(monkeypatch) -> None:
    calls = []
    chunk = _cayenne_chunk([], grid={"Cuisine": ["Italian"]})
    _drive_restore(monkeypatch, [chunk], lambda *a, **k: calls.append("ensure"), calls)
    assert calls == ["load"]
    assert chunk.pre_parsed.grid_categories == {"Cuisine": ["Italian"]}


def test_a_path_cut_short_by_depth_is_reported_on_the_cut_recipe(monkeypatch) -> None:
    calls = []
    written = []
    monkeypatch.setattr(api, "write_recipe_to_supabase", lambda recipe, uid, **kw: written.append(recipe) or "rid")
    fine = _cayenne_chunk([["A"]], title="Fine")
    cut = _cayenne_chunk([["A", "B", "C", "D", "E"]], title="Cut")
    pipeline_cls, _ = _drive_restore(
        monkeypatch, [fine, cut],
        lambda uid, paths, **k: {tuple(p): ("D" if len(p) == 5 else p[-1]) for p in paths}, calls,
    )
    assert cut.pre_parsed.grid_categories == {"restore": ["D"]}
    sink = pipeline_cls.return_value.run.call_args.kwargs["on_result"].__self__
    sink.on_result(MagicMock(title="Fine"), ai_processed=False)
    assert sink.refused_link_count == 0
    sink.on_result(MagicMock(title="Cut"), ai_processed=False)
    assert len(written) == 2
    assert sink.refused_link_count == 1
    assert sink.refused_links[0] == {
        "label": "Cut", "category_id": "E", "reason": "category 'E' is nested too deep to restore",
    }
    assert sink.skipped_count == 0


def test_empty_paths_keep_the_chunks_own_grid(monkeypatch) -> None:
    calls = []
    chunk = _cayenne_chunk([[]], grid={"Cuisine": ["Italian"]})
    _drive_restore(monkeypatch, [chunk], lambda *a, **k: calls.append("ensure"), calls)
    assert calls == ["load"]
    assert chunk.pre_parsed.grid_categories == {"Cuisine": ["Italian"]}


def test_a_category_creation_failure_does_not_fail_the_restore(monkeypatch) -> None:
    calls = []

    def ensure(uid, paths, **k):
        raise RuntimeError("supabase down")

    chunk = _cayenne_chunk([["Course", "Cake"]])
    pipeline_cls, finalized = _drive_restore(monkeypatch, [chunk], ensure, calls)
    assert calls == ["load"]
    assert chunk.pre_parsed.grid_categories == {"restore": ["Cake"]}
    pipeline_cls.return_value.run.assert_called_once()
    assert finalized[0]["status"] == "done"


def test_a_padded_category_name_still_links(monkeypatch) -> None:
    calls = []

    def ensure(uid, paths, *, supabase_url, service_key):
        return {rp.clean_path(p): rp.clean_path(p)[-1] for p in paths}

    chunk = _cayenne_chunk([[" Course ", " Cake "]])
    _drive_restore(monkeypatch, [chunk], ensure, calls)
    assert chunk.pre_parsed.grid_categories == {"restore": ["Cake"]}


def test_a_job_without_a_restore_chunk_never_builds_the_picture_source(monkeypatch) -> None:
    built = []
    monkeypatch.setattr(api, "_picture_source", lambda: built.append(1))
    _run_deferred(monkeypatch, [_flow_a_chunk()])
    assert built == []
    _run_deferred(monkeypatch, [_restore_chunk()])
    assert built == [1]
