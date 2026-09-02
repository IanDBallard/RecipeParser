"""Regression tests for the auth-bypass guard.

The bug these lock down: the dev launcher shipped DISABLE_AUTH=1 with a
hardcoded TEST_USER_ID, so a server started for local work never checked the
bearer token the client sent and filed every ingested recipe under one fixed
account. Two halves of the fix are covered here — the API refusing to boot on a
half-specified bypass, and the launcher never enabling one unasked.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from recipeparser.adapters.api import (  # noqa: E402
    AuthConfigurationError,
    _resolve_auth_mode,
)

import start_server  # noqa: E402

_UUID = "e7536d0a-c265-46e7-be58-c432cbd7bab9"


# ---------------------------------------------------------------------------
# _resolve_auth_mode
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("env", [
    {},
    {"DISABLE_AUTH": "0"},
    {"DISABLE_AUTH": ""},
    {"DISABLE_AUTH": "false"},
    # A stale TEST_USER_ID on its own must not unauthenticate anything.
    {"TEST_USER_ID": _UUID},
])
def test_verifying_by_default(env: dict[str, str]) -> None:
    assert _resolve_auth_mode(env) == (False, "")


@pytest.mark.parametrize("flag", ["1", "true", "TRUE", "yes", "on", " 1 "])
def test_bypass_accepts_truthy_flags_with_uuid(flag: str) -> None:
    assert _resolve_auth_mode({"DISABLE_AUTH": flag, "TEST_USER_ID": _UUID}) == (True, _UUID)


@pytest.mark.parametrize("env", [
    {"DISABLE_AUTH": "1"},
    {"DISABLE_AUTH": "1", "TEST_USER_ID": ""},
    {"DISABLE_AUTH": "1", "TEST_USER_ID": "   "},
    {"DISABLE_AUTH": "1", "TEST_USER_ID": "test-user"},
])
def test_bypass_without_a_uuid_subject_raises(env: dict[str, str]) -> None:
    """No fallback identity: a half-configured bypass fails, it does not degrade."""
    with pytest.raises(AuthConfigurationError):
        _resolve_auth_mode(env)


@pytest.mark.parametrize("tier_var", ["APP_ENV", "ENVIRONMENT"])
@pytest.mark.parametrize("tier", ["production", "PROD", "staging", " stage "])
def test_bypass_refused_on_protected_tier(tier_var: str, tier: str) -> None:
    with pytest.raises(AuthConfigurationError):
        _resolve_auth_mode({"DISABLE_AUTH": "1", "TEST_USER_ID": _UUID, tier_var: tier})


def test_bypass_allowed_on_unprotected_tier() -> None:
    env = {"DISABLE_AUTH": "1", "TEST_USER_ID": _UUID, "APP_ENV": "development"}
    assert _resolve_auth_mode(env) == (True, _UUID)


# ---------------------------------------------------------------------------
# start_server launcher
# ---------------------------------------------------------------------------

def test_launcher_defaults_to_verifying(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISABLE_AUTH", raising=False)
    monkeypatch.delenv("TEST_USER_ID", raising=False)
    args = start_server.parse_args([])
    assert args.disable_auth is False
    assert args.host == "0.0.0.0"

    env = start_server.build_env(args)
    assert "DISABLE_AUTH" not in env
    assert "TEST_USER_ID" not in env
    assert _resolve_auth_mode(env) == (False, "")


def test_launcher_strips_inherited_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale shell export or .env line must not leak into the server."""
    monkeypatch.setenv("DISABLE_AUTH", "1")
    monkeypatch.setenv("TEST_USER_ID", _UUID)
    env = start_server.build_env(start_server.parse_args([]))
    assert "DISABLE_AUTH" not in env
    assert "TEST_USER_ID" not in env


def test_launcher_bypass_requires_test_user_id() -> None:
    with pytest.raises(SystemExit):
        start_server.parse_args(["--disable-auth"])


def test_launcher_rejects_non_uuid_test_user_id() -> None:
    with pytest.raises(SystemExit):
        start_server.parse_args(["--disable-auth", "--test-user-id", "test-user"])


def test_launcher_rejects_test_user_id_without_bypass() -> None:
    with pytest.raises(SystemExit):
        start_server.parse_args(["--test-user-id", _UUID])


def test_launcher_bypass_is_explicit_and_loopback_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISABLE_AUTH", raising=False)
    monkeypatch.delenv("TEST_USER_ID", raising=False)
    args = start_server.parse_args(["--disable-auth", "--test-user-id", _UUID])
    assert args.host == "127.0.0.1"
    assert _resolve_auth_mode(start_server.build_env(args)) == (True, _UUID)


def test_launcher_bypass_host_override_still_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    args = start_server.parse_args(
        ["--disable-auth", "--test-user-id", _UUID, "--host", "0.0.0.0"]
    )
    assert args.host == "0.0.0.0"
