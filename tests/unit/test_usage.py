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


def _claude(i=100, o=20, cc=30, cr=50):
    return SimpleNamespace(input_tokens=i, output_tokens=o, cache_creation_input_tokens=cc, cache_read_input_tokens=cr)


def test_record_claude_sums_prompt_tokens() -> None:
    with usage.attributed("u1", "search", get_client=lambda: None) as ctx:
        usage.record_claude("agent", "claude-opus-5-5", _claude())
        usage.record_claude("agent", "claude-opus-5-5", _claude(cc=None, cr=None))
        usage.record_claude("agent", "claude-opus-5-5", None)
        rows = list(ctx.records)
    assert rows[0] == {
        "user_id": "u1", "action": "search", "step": "agent", "job_id": None, "recipe_id": None,
        "model": "claude-opus-5-5", "calls": 1, "prompt_tokens": 180, "output_tokens": 20, "thought_tokens": None,
    }
    assert rows[1]["prompt_tokens"] == 100
    assert rows[2]["prompt_tokens"] is None and rows[2]["output_tokens"] is None


def test_record_claude_adds_a_row_per_server_tool_kind() -> None:
    def with_tools(tools: object) -> SimpleNamespace:
        return SimpleNamespace(**vars(_claude()), server_tool_use=tools)

    with usage.attributed("u1", "search", get_client=lambda: None) as ctx:
        usage.record_claude("look", "m", with_tools(SimpleNamespace(web_search_requests=2, web_fetch_requests=3)))
        usage.record_claude("look", "m", with_tools(None))
        usage.record_claude("look", "m", with_tools(SimpleNamespace(web_search_requests=0, web_fetch_requests=1)))
        rows = ctx.snapshot()
    assert [(r["step"], r["calls"], r["prompt_tokens"]) for r in rows] == [
        ("look", 1, 180), ("look_search", 2, None), ("look_fetch", 3, None),
        ("look", 1, 180),
        ("look", 1, 180), ("look_fetch", 1, None),
    ]
    assert all(r["output_tokens"] is None for r in rows if r["step"] != "look")


def test_record_tool_adds_one_call_without_tokens() -> None:
    with usage.attributed("u1", "search", get_client=lambda: None) as ctx:
        usage.record_tool("look_links", "brave-web-search")
        rows = ctx.snapshot()
    assert rows == [{
        "user_id": "u1", "action": "search", "step": "look_links", "job_id": None, "recipe_id": None,
        "model": "brave-web-search", "calls": 1, "prompt_tokens": None, "output_tokens": None, "thought_tokens": None,
    }]
    usage.record_tool("look_links", "brave-web-search")  # outside a context: nothing kept, nothing raised


def test_snapshot_is_a_copy() -> None:
    with usage.attributed("u1", "search", get_client=lambda: None) as ctx:
        usage.record_claude("agent", "m", _claude())
        snap = ctx.snapshot()
        snap.clear()
        assert len(ctx.snapshot()) == 1


def test_record_claude_without_context_keeps_nothing() -> None:
    usage.record_claude("agent", "m", _claude())
    assert usage.ai_context.get() is None
