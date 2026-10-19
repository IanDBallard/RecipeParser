"""A real cancel for ingest jobs, bounded Gemini retries, and a reaper that names the right cause.

Cayenne F-426, the first beta bug report (2026-10-18): "When I press cancel the process does not
stop." A photo import's OCR sat in Gemini's 504 DEADLINE_EXCEEDED retry ladder. Three cancels
answered 200 while the job's controller logged "invalid transition (idle, 'cancel') — ignored":
the photo's OCR is the job's *read*, before the pipeline moves the controller out of IDLE. The
photo had been doubled to 6000x8000 and sent as a lossless PNG, and the API was OOM-killed on the
fifth attempt; the reaper then ended the row as "stopped when the server restarted".

No live writes and no real API calls: every Supabase client here is a fake, every Gemini client a
stub, and every wait is cut short by a cancel or patched out.
"""
from __future__ import annotations

import asyncio
import datetime
import io
import os
import threading
from contextlib import nullcontext
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest
from google.genai import errors as genai_errors

os.environ.setdefault("DISABLE_AUTH", "1")
os.environ.setdefault("TEST_USER_ID", "00000000-0000-4000-8000-000000000001")

from recipeparser import gemini  # noqa: E402
from recipeparser.core import job_signal  # noqa: E402
from recipeparser.core.job_signal import JobSignal  # noqa: E402
from recipeparser.exceptions import AIServiceTimeoutError, JobCancelledError  # noqa: E402

OWNER = os.environ["TEST_USER_ID"]


def _deadline() -> Exception:
    """The error Gemini sent on 2026-10-18, as google-genai raises it."""
    return genai_errors.ServerError(
        504, {"error": {"code": 504, "message": "Deadline expired before operation could complete.",
                        "status": "DEADLINE_EXCEEDED"}}
    )


class _Clock:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# ── JobSignal ─────────────────────────────────────────────────────────────────

def test_outside_a_job_every_checkpoint_is_a_no_op():
    assert job_signal.current() is None
    job_signal.checkpoint()
    job_signal.touch()
    with job_signal.shielded():
        pass


def test_a_checkpoint_raises_once_the_job_is_cancelled_and_not_while_shielded():
    signal = JobSignal()
    with job_signal.active(signal):
        job_signal.checkpoint()
        signal.cancel()
        with job_signal.shielded():
            job_signal.checkpoint()  # the wind-down after a cancel runs to its end
        with pytest.raises(JobCancelledError):
            job_signal.checkpoint()


def test_a_wait_wakes_at_once_on_a_cancel():
    signal = JobSignal()
    threading.Timer(0.05, signal.cancel).start()
    started = datetime.datetime.now()
    with pytest.raises(JobCancelledError):
        signal.wait(30)
    assert (datetime.datetime.now() - started).total_seconds() < 5


def test_the_signal_reaches_worker_threads_through_to_thread():
    signal = JobSignal()
    seen: List[Any] = []

    async def main():
        with job_signal.active(signal):
            await asyncio.to_thread(lambda: seen.append(job_signal.current()))

    asyncio.run(main())
    assert seen == [signal]


# ── the Gemini retry ladder ───────────────────────────────────────────────────

def test_a_cancel_during_the_back_off_wait_stops_the_retries():
    """The incident's shape: 504, a wait, and the owner cancels inside the wait."""
    signal = JobSignal()
    client = MagicMock()

    def _first_call(**_kw):
        threading.Timer(0.05, signal.cancel).start()
        raise _deadline()

    client.models.generate_content.side_effect = _first_call
    with job_signal.active(signal), pytest.raises(JobCancelledError):
        gemini._call_with_retry(client, "m", "prompt", {})
    assert client.models.generate_content.call_count == 1, "no attempt after the cancel"


def test_a_job_cancelled_between_retries_makes_no_further_call():
    signal = JobSignal()
    client = MagicMock()
    calls = []

    def _call(**_kw):
        calls.append(1)
        signal.cancel()
        raise _deadline()

    client.models.generate_content.side_effect = _call
    with job_signal.active(signal), pytest.raises(JobCancelledError):
        gemini._call_with_retry(client, "m", "prompt", {})
    assert len(calls) == 1


def test_the_transient_retry_budget_bounds_a_run_of_504s(monkeypatch):
    """Each 504 took ~40 s on 2026-10-18; the ladder alone would have run for minutes."""
    clock = _Clock()
    monkeypatch.setattr(gemini, "_clock", clock)
    monkeypatch.setattr(gemini.time, "sleep", lambda s: setattr(clock, "now", clock.now + s))
    client = MagicMock()

    def _slow_504(**_kw):
        clock.now += 40
        raise _deadline()

    client.models.generate_content.side_effect = _slow_504
    with pytest.raises(genai_errors.ServerError):
        gemini._call_with_retry(client, "m", "prompt", {})
    attempts = client.models.generate_content.call_count
    assert attempts < gemini.MAX_RETRIES + 1, "the budget, not the ladder, ended it"
    # 40 + 2 + 40 + 4 + 40 + 8 = 134 s; a fourth attempt would end past the 180 s budget's start line.
    assert attempts == 4
    assert clock.now - 1000.0 <= gemini.TRANSIENT_RETRY_BUDGET_SECS + 40


def test_the_budget_does_not_cut_short_a_rate_limit_ladder(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(gemini, "_clock", clock)
    monkeypatch.setattr(gemini.time, "sleep", lambda s: setattr(clock, "now", clock.now + s))
    client = MagicMock()

    def _slow_429(**_kw):
        clock.now += 60
        error = {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}}
        raise genai_errors.ClientError(429, error)

    client.models.generate_content.side_effect = _slow_429
    with pytest.raises(genai_errors.ClientError):
        gemini._call_with_retry(client, "m", "prompt", {})
    assert client.models.generate_content.call_count == gemini.MAX_RETRIES + 1


def test_every_attempt_and_every_wait_is_a_sign_of_life():
    """The heartbeat stays on through retries: each attempt and each wait touches the signal."""
    clock = _Clock()
    signal = JobSignal(clock=clock)
    client = MagicMock()
    idle_at_each_call: List[float] = []

    def _call(**_kw):
        idle_at_each_call.append(signal.idle_seconds())
        clock.now += 300  # five minutes inside one call: longer than the lease
        if len(idle_at_each_call) < 3:
            raise _deadline()
        return MagicMock(text="ok", candidates=[], usage_metadata=None)

    client.models.generate_content.side_effect = _call
    with job_signal.active(signal), patch.object(signal, "wait", side_effect=lambda s: signal.touch()):
        gemini._call_with_retry(client, "m", "prompt", {})
    assert idle_at_each_call == [0.0, 0.0, 0.0], "every attempt starts with a fresh sign of life"


# ── the vision OCR ────────────────────────────────────────────────────────────

def test_a_photo_that_keeps_timing_out_fails_with_the_cooks_sentence(monkeypatch):
    monkeypatch.setattr(gemini.time, "sleep", lambda _s: None)
    client = MagicMock()
    client.models.generate_content.side_effect = _deadline()
    image = gemini.VisionImage(b"jpeg", "image/jpeg", 10, 10)
    with pytest.raises(AIServiceTimeoutError) as excinfo:
        gemini.extract_text_from_images([image], client, count=1, what="photo")
    assert str(excinfo.value) == "The AI service took too long to read this photo. Try again in a few minutes."


def test_the_job_row_carries_the_timeout_sentence_alone():
    import recipeparser.adapters.api as api

    exc = AIServiceTimeoutError(gemini.ai_timeout_sentence("photo"))
    assert api._job_error_message("IMG_0339.jpg", exc) == (
        "The AI service took too long to read this photo. Try again in a few minutes."
    )


def test_a_vision_call_carries_its_own_tighter_timeout():
    client = MagicMock()
    client.models.generate_content.return_value = MagicMock(text="Cake", usage_metadata=None)
    gemini.extract_text_from_images([gemini.VisionImage(b"x", "image/jpeg", 1, 1)], client, count=1)
    config = client.models.generate_content.call_args.kwargs["config"]
    assert config["http_options"]["timeout"] == gemini.VISION_TIMEOUT_SECS * 1000


def test_a_cancel_is_not_swallowed_as_a_failed_page():
    signal = JobSignal()
    signal.cancel()
    client = MagicMock()
    with job_signal.active(signal), pytest.raises(JobCancelledError):
        gemini.extract_text_from_images([gemini.VisionImage(b"x", "image/jpeg", 1, 1)], client, count=1)
    client.models.generate_content.assert_not_called()


def _phone_photo(tmp_path, orientation: int | None = None) -> str:
    """A 4000x3000 JPEG, the size of a phone camera's, with some text-like detail."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (4000, 3000), "white")
    draw = ImageDraw.Draw(img)
    for y in range(100, 2900, 60):
        draw.text((100, y), "1 cup flour, 2 eggs, mix and bake at 350F for 30 minutes " * 4, fill="black")
    path = tmp_path / "IMG_0339.jpg"
    kwargs: Dict[str, Any] = {"quality": 92}
    if orientation is not None:
        exif = Image.Exif()
        exif[0x0112] = orientation
        kwargs["exif"] = exif
    img.save(path, "JPEG", **kwargs)
    return str(path)


def test_a_4000x3000_photo_is_sent_at_2000px_as_a_jpeg_of_a_few_mb(tmp_path):
    from recipeparser.io.readers.image import ImageReader

    client = MagicMock()
    client.models.generate_content.return_value = MagicMock(text="Cake", usage_metadata=None)
    ImageReader(client).read(_phone_photo(tmp_path))

    part = client.models.generate_content.call_args.kwargs["contents"][0]
    from PIL import Image

    sent = Image.open(io.BytesIO(part.inline_data.data))
    assert part.inline_data.mime_type == "image/jpeg"
    assert max(sent.size) <= 2000
    assert sent.size == (2000, 1500)
    assert len(part.inline_data.data) < 3_000_000


def test_the_photo_is_turned_upright_from_its_exif_orientation(tmp_path):
    """Orientation 6 (rotate 90): a landscape sensor image of a portrait page."""
    from PIL import Image

    from recipeparser.io.readers.image import photo_for_vision

    image = photo_for_vision(_phone_photo(tmp_path, orientation=6))
    assert image is not None
    assert (image.width, image.height) == (1500, 2000)
    assert Image.open(io.BytesIO(image.data)).size == (1500, 2000)


def test_a_pdf_page_still_renders_at_2x_but_never_past_the_cap():
    import fitz

    doc = fitz.open()
    doc.new_page(width=595, height=842)
    doc.new_page(width=3000, height=4000)
    small = gemini.render_page_for_vision(doc[0])
    big = gemini.render_page_for_vision(doc[1])
    assert (small.width, small.height) == (1190, 1684)
    assert max(big.width, big.height) <= 2000
    doc.close()


# ── the pipeline: between stages and between chunks ───────────────────────────

def _pipeline(concurrency: int = 1):
    from recipeparser.core.fsm import PipelineController
    from recipeparser.core.pipeline import RecipePipeline
    from recipeparser.core.rate_limiter import GlobalRateLimiter

    GlobalRateLimiter().reset()
    source = MagicMock()
    source.load_axes.return_value = {}
    return RecipePipeline(
        client=MagicMock(),
        controller=PipelineController(),
        category_source=source,
        concurrency=concurrency,
        rpm=9999,
        categorize_fn=lambda *a, **k: {},
    )


def test_a_cancel_between_chunks_keeps_what_was_written_and_skips_nothing():
    from recipeparser.core.models import Chunk, InputType
    from recipeparser.core.stages.extract import Extraction
    from recipeparser.models import RecipeExtraction

    signal = JobSignal()
    written: List[Any] = []
    skipped: List[Any] = []

    def _on_result(result, _needs_tags):
        written.append(result)
        signal.cancel()  # the owner presses Cancel once the first recipe is in

    chunks = [Chunk(text=f"chunk {i}", input_type=InputType.URL) for i in range(4)]
    extraction = Extraction([RecipeExtraction(name="Pie", ingredients=["x"], directions=["y"])], [])

    def _extract(chunk_text, **_kw):
        if chunk_text != "chunk 0":
            # The second chunk is mid-EXTRACT when the cancel comes: hold it there until it does.
            signal._cancelled.wait(5)
        return extraction

    with job_signal.active(signal), \
            patch("recipeparser.core.pipeline.extract", side_effect=_extract) as extract, \
            patch("recipeparser.core.pipeline.refine", return_value=MagicMock()), \
            patch("recipeparser.core.pipeline.embed", return_value=[0.1]), \
            patch("recipeparser.core.pipeline.assemble", side_effect=lambda **k: MagicMock()):
        _pipeline().run(chunks, on_result=_on_result, on_skip=lambda *a: skipped.append(a))

    assert len(written) == 1, "the recipe written before the cancel stays; none after it"
    assert skipped == [], "a chunk stopped by the cancel is not a skipped chunk"
    assert extract.call_count <= 2, "the chunks still queued never started"


def test_a_cancel_between_stages_stops_the_chunk_before_its_next_stage():
    from recipeparser.core.models import Chunk, InputType
    from recipeparser.core.stages.extract import Extraction
    from recipeparser.models import RecipeExtraction

    signal = JobSignal()

    def _extract(**_kw):
        signal.cancel()  # pressed while EXTRACT was running
        return Extraction([RecipeExtraction(name="Pie", ingredients=["x"], directions=["y"])], [])

    skipped: List[Any] = []
    with job_signal.active(signal), \
            patch("recipeparser.core.pipeline.extract", side_effect=_extract), \
            patch("recipeparser.core.pipeline.refine") as refine:
        _pipeline().run([Chunk(text="t", input_type=InputType.URL)], on_skip=lambda *a: skipped.append(a))

    refine.assert_not_called()
    assert skipped == []


# ── the job runner ────────────────────────────────────────────────────────────

def _run_job(monkeypatch, read, job_id="job-c"):
    import recipeparser.adapters.api as api
    from recipeparser.core.fsm import PipelineController

    finalized: List[Dict[str, Any]] = []
    monkeypatch.setattr(api, "_finalize_ingestion_job", lambda jid, payload: finalized.append(payload))
    monkeypatch.setattr(api.usage, "attributed", lambda *a, **k: nullcontext())
    controller = PipelineController()
    api._job_signals[job_id] = JobSignal()
    api._active_jobs[job_id] = (OWNER, controller)
    asyncio.run(api._run_ingestion_job(job_id, OWNER, controller, read, "IMG_0339.jpg", "File job"))
    assert job_id not in api._active_jobs and job_id not in api._job_signals
    return finalized


def test_a_photo_cancelled_mid_retry_ends_cancelled_not_failed(monkeypatch):
    """End to end: the photo's OCR is in the 504 ladder and the owner cancels it."""
    import recipeparser.adapters.api as api

    client = MagicMock()

    def _first(**_kw):
        api._job_signals["job-c"].cancel()  # what POST /jobs/{id}/cancel does in this process
        raise _deadline()

    client.models.generate_content.side_effect = _first

    async def _read():
        image = gemini.VisionImage(b"jpeg", "image/jpeg", 2000, 1500)
        text = await asyncio.to_thread(
            gemini.extract_text_from_images, [image], client, count=1, what="photo"
        )
        raise AssertionError(f"the read must not finish: {text}")

    finalized = _run_job(monkeypatch, _read)
    assert finalized == [{"status": "cancelled", "stage": "DONE", "updated_at": finalized[0]["updated_at"]}]
    assert client.models.generate_content.call_count == 1


def test_a_cancel_that_lands_as_the_read_finishes_still_ends_cancelled(monkeypatch):
    import recipeparser.adapters.api as api

    async def _read():
        api._job_signals["job-c"].cancel()
        return MagicMock(), []

    finalized = _run_job(monkeypatch, _read)
    assert finalized[0]["status"] == "cancelled"


def test_a_photo_that_kept_timing_out_ends_as_an_error_with_the_sentence(monkeypatch):
    monkeypatch.setattr(job_signal, "sleep", lambda _s: None)  # inside a job the waits are the signal's
    client = MagicMock()
    client.models.generate_content.side_effect = _deadline()

    async def _read():
        image = gemini.VisionImage(b"jpeg", "image/jpeg", 2000, 1500)
        await asyncio.to_thread(gemini.extract_text_from_images, [image], client, count=1, what="photo")

    finalized = _run_job(monkeypatch, _read)
    assert finalized[0]["status"] == "error"
    assert finalized[0]["error_message"] == (
        "The AI service took too long to read this photo. Try again in a few minutes."
    )


# ── the cancel endpoint ───────────────────────────────────────────────────────

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
        if any(op[0] == "select" for op in self.ops):
            return _Result(self.fake.rows)
        return _Result([])


class FakeSupabase:
    def __init__(self, rows: List[Dict[str, Any]]):
        self.rows = rows
        self.queries: List[_Query] = []

    def table(self, name):
        return _Query(self, name)

    def updates(self) -> List[_Query]:
        return [q for q in self.queries if any(op[0] == "update" for op in q.ops)]


@pytest.fixture
def api_client(monkeypatch):
    from fastapi.testclient import TestClient

    import recipeparser.adapters.api as api

    api._active_jobs.clear()
    api._job_signals.clear()
    with TestClient(api.app) as client:
        yield api, client
    api._active_jobs.clear()
    api._job_signals.clear()


def _register_running(api, job_id="job-r"):
    from recipeparser.core.fsm import PipelineController

    controller = PipelineController()
    api._active_jobs[job_id] = (OWNER, controller)
    api._job_signals[job_id] = JobSignal()
    return controller, api._job_signals[job_id]


def test_cancel_marks_the_row_cancelled_at_once_and_signals_the_job(api_client, monkeypatch):
    api, client = api_client
    fake = FakeSupabase([{"id": "job-r", "status": "running", "kind": "ingest"}])
    monkeypatch.setattr(api, "_get_supabase_service_client", lambda: fake)
    controller, signal = _register_running(api)

    response = client.post("/jobs/job-r/cancel")

    assert response.status_code == 200
    assert response.json() == {"job_id": "job-r", "status": "cancelled"}
    assert signal.cancelled, "the job stops at its next checkpoint, even while still reading"
    (update,) = fake.updates()
    payload = next(op[1][0] for op in update.ops if op[0] == "update")
    assert payload["status"] == "cancelled"
    assert ("eq", ("user_id", OWNER), {}) in update.ops
    assert ("in_", ("status", ["pending", "running"]), {}) in update.ops


def test_cancel_in_the_read_phase_does_not_poke_the_idle_controller(api_client, monkeypatch, caplog):
    """The incident logged "invalid transition (idle, 'cancel') — ignored" three times."""
    api, client = api_client
    monkeypatch.setattr(api, "_get_supabase_service_client", lambda: FakeSupabase(
        [{"id": "job-r", "status": "running", "kind": "ingest"}]
    ))
    controller, signal = _register_running(api)
    assert client.post("/jobs/job-r/cancel").status_code == 200
    assert "invalid transition" not in caplog.text
    assert signal.cancelled


def test_cancel_a_finished_job_is_409(api_client, monkeypatch):
    api, client = api_client
    fake = FakeSupabase([{"id": "job-d", "status": "done", "kind": "ingest"}])
    monkeypatch.setattr(api, "_get_supabase_service_client", lambda: fake)
    response = client.post("/jobs/job-d/cancel")
    assert response.status_code == 409
    assert fake.updates() == [], "nothing is written over a finished job"


def test_cancel_a_job_that_is_not_there_is_404(api_client, monkeypatch):
    api, client = api_client
    monkeypatch.setattr(api, "_get_supabase_service_client", lambda: FakeSupabase([]))
    assert client.post("/jobs/nope/cancel").status_code == 404


def test_cancel_someone_elses_running_job_is_404(api_client, monkeypatch):
    api, client = api_client
    from recipeparser.core.fsm import PipelineController

    monkeypatch.setattr(api, "_get_supabase_service_client", lambda: FakeSupabase([]))
    api._active_jobs["theirs"] = ("someone-else", PipelineController())
    api._job_signals["theirs"] = JobSignal()
    assert client.post("/jobs/theirs/cancel").status_code == 404
    assert not api._job_signals["theirs"].cancelled


def test_a_row_still_running_after_its_process_died_can_be_cancelled(api_client, monkeypatch):
    api, client = api_client
    fake = FakeSupabase([{"id": "orphan", "status": "running", "kind": "ingest"}])
    monkeypatch.setattr(api, "_get_supabase_service_client", lambda: fake)
    assert client.post("/jobs/orphan/cancel").status_code == 200
    assert len(fake.updates()) == 1


def test_recategorize_cancel_still_writes_the_row(api_client, monkeypatch):
    api, client = api_client
    fake = FakeSupabase([{"id": "rc", "status": "pending", "kind": "recategorize"}])
    monkeypatch.setattr(api, "_get_supabase_service_client", lambda: fake)
    response = client.post("/jobs/rc/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert len(fake.updates()) == 1


def test_a_stage_write_cannot_flip_a_cancelled_row_back_to_running(monkeypatch):
    import recipeparser.adapters.api as api

    fake = FakeSupabase([])
    monkeypatch.setattr(api, "_live_writes_blocked", lambda: False)
    monkeypatch.setenv("SUPABASE_URL", "https://example.invalid")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-only")
    with patch("supabase.create_client", return_value=fake):
        api._make_stage_callback("job-s")("REFINING")
    (update,) = fake.updates()
    assert ("in_", ("status", ["pending", "running"]), {}) in update.ops


def test_finalize_never_overwrites_a_row_ended_as_an_error(monkeypatch):
    import recipeparser.adapters.api as api

    fake = FakeSupabase([])
    monkeypatch.setattr(api, "_live_writes_blocked", lambda: False)
    monkeypatch.setattr(api, "_get_supabase_service_client", lambda: fake)
    api._finalize_ingestion_job("job-f", {"status": "cancelled"})
    (update,) = fake.updates()
    assert ("in_", ("status", ["pending", "running", "cancelled"]), {}) in update.ops


# ── liveness: the heartbeat, the stall check and the reaper's words ───────────

NOW = datetime.datetime(2026, 10, 18, 16, 25, 5, tzinfo=datetime.timezone.utc)


def _update_payloads(fake: FakeSupabase) -> List[Dict[str, Any]]:
    return [next(op[1][0] for op in q.ops if op[0] == "update") for q in fake.updates()]


def test_a_job_still_doing_things_keeps_its_heartbeat_through_a_long_retry():
    from recipeparser.adapters import ingest_liveness as il

    clock = _Clock()
    signal = JobSignal(clock=clock)
    clock.now += 4 * 60  # four minutes into a 504 ladder...
    signal.touch()       # ...and the ladder has just started another attempt
    clock.now += 4 * 60
    fake = FakeSupabase([])
    il.tick(fake, {"live": signal}, NOW)
    payloads = _update_payloads(fake)
    assert payloads[0] == {"updated_at": payloads[0]["updated_at"]}, "the heartbeat touched it"
    assert il.STALLED_MESSAGE not in [p.get("error_message") for p in payloads]
    assert not signal.cancelled


def test_a_hung_job_is_ended_as_stopped_responding_not_as_a_restart():
    from recipeparser.adapters import ingest_liveness as il

    clock = _Clock()
    signal = JobSignal(clock=clock)
    clock.now += il.STALL_MINUTES * 60 + 1
    fake = FakeSupabase([])
    il.tick(fake, {"hung": signal}, NOW)

    stalled = [q for q in fake.updates() if ("in_", ("id", ["hung"]), {}) in q.ops]
    (end,) = stalled
    payload = next(op[1][0] for op in end.ops if op[0] == "update")
    assert payload["error_message"] == il.STALLED_MESSAGE
    assert payload["error_message"].startswith("The import stopped responding")
    assert ("in_", ("status", ["pending", "running"]), {}) in end.ops
    assert signal.cancelled, "if it ever wakes, it stops"


def test_only_a_row_no_live_process_owns_is_ended_as_a_restart():
    from recipeparser.adapters import ingest_liveness as il

    fake = FakeSupabase([])
    il.tick(fake, {}, NOW)
    (reap,) = fake.updates()
    payload = next(op[1][0] for op in reap.ops if op[0] == "update")
    assert payload["error_message"] == il.INTERRUPTED_MESSAGE
    assert "server restarted" in il.INTERRUPTED_MESSAGE
    assert "server restarted" not in il.STALLED_MESSAGE


def test_a_paused_job_is_never_stalled():
    from recipeparser.adapters import ingest_liveness as il

    fake = FakeSupabase([])
    il.tick(fake, {"paused": None}, NOW)
    assert all(p.get("error_message") != il.STALLED_MESSAGE for p in _update_payloads(fake))


def test_a_row_cancelled_on_another_instance_stops_the_job_here():
    from recipeparser.adapters import ingest_liveness as il

    signal = JobSignal()
    fake = FakeSupabase([{"id": "elsewhere"}])  # the select for cancelled rows answers this id
    il.tick(fake, {"elsewhere": signal}, NOW)
    assert signal.cancelled


def test_the_api_view_hands_liveness_each_jobs_signal(api_client):
    api, _client = api_client
    from recipeparser.core.fsm import PipelineController

    _controller, signal = _register_running(api, "j1")
    paused = PipelineController()
    paused.transition("start")
    paused.request_pause()
    api._active_jobs["j2"] = (OWNER, paused)
    api._job_signals["j2"] = JobSignal()
    assert api._liveness_view() == {"j1": signal, "j2": None}
