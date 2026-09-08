"""Model migration + cost visibility (Gemini 2.5 Flash retirement, 2026-10-16).

Three things this covers, none of which existed before:

1. The model name lives in one config constant (GEMINI_MODEL /
   GEMINI_EMBEDDING_MODEL), not scattered string literals — asserted
   per-call-site in tests/test_gemini.py, tests/test_gemini_cayenne.py and
   here.
2. Every generate_content call disables thinking by default (THINKING_BUDGET
   = 0): these are bounded extraction/classification tasks, not open-ended
   reasoning, and thinking tokens bill at the output rate for nothing.
3. Every reply's usage_metadata is logged, so a real per-call token count
   exists instead of an estimate from prompt length.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from recipeparser.config import THINKING_BUDGET
from recipeparser.gemini import _call_with_retry, _finalize_config, _log_usage_metadata


def _client(*replies, side_effect=None):
    client = MagicMock()
    if side_effect is not None:
        client.models.generate_content.side_effect = side_effect
    else:
        client.models.generate_content.side_effect = list(replies)
    return client


def test_thinking_budget_defaults_to_zero():
    """Regression guard: THINKING_BUDGET must default to 0 unless the
    GEMINI_THINKING_BUDGET env var overrides it (not set in this test run)."""
    assert THINKING_BUDGET == 0


def test_finalize_config_sets_thinking_budget_alongside_the_timeout():
    config = _finalize_config({"temperature": 0.1})

    assert config["thinking_config"]["thinking_budget"] == THINKING_BUDGET
    assert config["http_options"]["timeout"] > 0
    # The caller's own keys must survive untouched.
    assert config["temperature"] == 0.1


def test_call_with_retry_sends_the_thinking_budget_to_generate_content():
    client = _client(SimpleNamespace(text="ok", candidates=[]))

    _call_with_retry(client, model="some-model", contents="hi", config={})

    _, kwargs = client.models.generate_content.call_args
    assert kwargs["config"]["thinking_config"]["thinking_budget"] == 0


def test_log_usage_metadata_logs_the_real_token_counts(caplog):
    usage = SimpleNamespace(
        prompt_token_count=120,
        candidates_token_count=45,
        thoughts_token_count=0,
        total_token_count=165,
    )
    response = SimpleNamespace(usage_metadata=usage)

    with caplog.at_level("INFO"):
        _log_usage_metadata(response, "Gemini extraction")

    assert any(
        "Gemini extraction usage" in r.message and "prompt=120" in r.message and "total=165" in r.message
        for r in caplog.records
    )


def test_log_usage_metadata_is_a_silent_no_op_without_usage_metadata():
    """A stub response with no usage_metadata (e.g. SimpleNamespace in the
    older timeout/retry tests) must not raise — this is best-effort logging,
    never a hard dependency of the call path."""
    response = SimpleNamespace(text="ok")

    _log_usage_metadata(response, "Gemini extraction")  # must not raise


def test_call_with_retry_logs_usage_on_a_successful_call(caplog):
    usage = SimpleNamespace(
        prompt_token_count=10,
        candidates_token_count=5,
        thoughts_token_count=None,
        total_token_count=15,
    )
    client = _client(SimpleNamespace(text="ok", candidates=[], usage_metadata=usage))

    with caplog.at_level("INFO"):
        _call_with_retry(client, model="some-model", contents="hi", config={}, what="Table normalisation")

    assert any("Table normalisation usage" in r.message for r in caplog.records)
