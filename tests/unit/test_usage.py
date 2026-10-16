# tests/unit/test_usage.py
from concurrent.futures import ThreadPoolExecutor
import contextvars
from types import SimpleNamespace
from unittest.mock import MagicMock

from recipeparser import usage


def _meta(p=10, c=5, t=2):
    return SimpleNamespace(prompt_token_count=p, candidates_token_count=c, thoughts_token_count=t)


def test_record_without_context_only_logs() -> None:
    usage.record("refine", "m", _meta())  # no context: nothing raised, nothing kept
    assert usage.ai_context.get() is None


def test_attributed_records_and_flushes_one_insert() -> None:
    sb = MagicMock()
    with usage.attributed("u1", "import", "j1", get_client=lambda: sb) as ctx:
        usage.record("extract", "gemini-x", _meta())
        usage.record("refine", "gemini-x", None)
        assert len(ctx.records) == 2
    sb.table.assert_called_once_with("ai_usage")
    rows = sb.table.return_value.insert.call_args.args[0]
    assert rows[0] == {
        "user_id": "u1", "action": "import", "step": "extract", "job_id": "j1", "recipe_id": None,
        "model": "gemini-x", "calls": 1, "prompt_tokens": 10, "output_tokens": 5, "thought_tokens": 2,
    }
    assert rows[1]["prompt_tokens"] is None
    assert usage.ai_context.get() is None


def test_flush_failure_is_swallowed() -> None:
    sb = MagicMock()
    sb.table.return_value.insert.return_value.execute.side_effect = RuntimeError("down")
    with usage.attributed("u1", "search", get_client=lambda: sb):
        usage.record("embed", "e", _meta())
    # no exception escaped


def test_unconfigured_database_skips_the_flush() -> None:
    with usage.attributed("u1", "search", get_client=lambda: None):
        usage.record("embed", "e", _meta())


def test_empty_buffer_writes_nothing() -> None:
    sb = MagicMock()
    with usage.attributed("u1", "classify", get_client=lambda: sb):
        pass
    sb.table.assert_not_called()


def test_records_from_a_pool_worker_land_in_the_callers_buffer() -> None:
    sb = MagicMock()
    with usage.attributed("u1", "import", "j1", get_client=lambda: sb) as ctx:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(contextvars.copy_context().run, usage.record, f"s{i}", "m", _meta()) for i in range(20)]
            for f in futures:
                f.result()
        assert len(ctx.records) == 20


def test_flush_runs_when_the_body_raises() -> None:
    sb = MagicMock()
    try:
        with usage.attributed("u1", "picture", get_client=lambda: sb):
            usage.record("picture", "img", _meta())
            raise ValueError("boom")
    except ValueError:
        pass
    sb.table.return_value.insert.assert_called_once()
