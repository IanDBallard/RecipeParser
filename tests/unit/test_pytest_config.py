"""The guard that keeps write-mode runs off the xdist workers.

Parallel is the default (``addopts = -n auto`` in pyproject.toml), but three
flags must never be distributed.  ``serial_reason`` is the whole decision,
kept pure so it can be checked without spawning a pytest.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import subprocess
import sys

import pytest
import tomllib

from tests import conftest
from tests.conftest import WORKER_CAP, default_workers, serial_reason

#: Every guarded flag, with the fragment its reason must name so the operator
#: can tell which flag demoted the run.
GUARDED = [
    ("record_gemini", "--record-gemini"),
    ("update_goldens", "--update-goldens"),
    ("update_snapshots", "--snapshot-update"),
]


def _options(**overrides: bool) -> argparse.Namespace:
    """A pytest-like options namespace with every guarded flag off."""
    flags = {name: False for name, _ in GUARDED}
    flags.update(overrides)
    return argparse.Namespace(**flags)


def test_an_ordinary_run_may_be_distributed() -> None:
    assert serial_reason(_options()) is None


@pytest.mark.parametrize("attr,flag", GUARDED)
def test_each_write_mode_flag_forces_serial(attr: str, flag: str) -> None:
    reason = serial_reason(_options(**{attr: True}))
    assert reason is not None, f"{flag} must demote the run to serial"
    assert flag in reason, f"the reason must name {flag}, got: {reason}"


def test_the_reason_says_why_not_just_what() -> None:
    """A bare flag name teaches nobody; the reason must carry the cause."""
    reason = serial_reason(_options(record_gemini=True))
    assert "GlobalRateLimiter" in reason


def test_absent_options_are_not_an_error() -> None:
    """syrupy or the goldens conftest may not have registered their flags."""
    assert serial_reason(argparse.Namespace()) is None


# ──────────────────────────────────────────────────────────────────────────────
# How many workers a run gets when the operator named no count.
#
# Measured on the 759-test suite (8-core/16-thread desktop): serial 9.23s,
# n=4 7.18s, n=6 7.18s, n=8 7.50s, n=16 10.14s.  Past the cap the per-worker
# import cost outruns the gain -- xdist's own "auto" (16 here, since with no
# psutil it counts logical cores) is SLOWER than not distributing at all.
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("cpus,expected", [
    (16, WORKER_CAP),   # the desktop this was tuned on
    (8, WORKER_CAP),
    (4, 4),             # small CI runner: use what it has
    (2, 2),
])
def test_worker_count_tracks_the_machine_up_to_the_cap(cpus: int, expected: int) -> None:
    assert default_workers(cpus) == expected


@pytest.mark.parametrize("cpus", [1, 0, None])
def test_a_machine_that_cannot_gain_from_workers_stays_serial(cpus) -> None:
    """One core (or an unknowable count) pays worker startup for nothing."""
    assert default_workers(cpus) == 0


# ──────────────────────────────────────────────────────────────────────────────
# The wiring.  serial_reason and default_workers only decide; these prove the
# decisions reach a real pytest -- the part that regresses silently if xdist
# changes its internals.
#
# A probe is a subprocess pytest and costs ~4s, so there is exactly ONE, and it
# needs no separate "does xdist really distribute?" control: the header it
# asserts on can only appear when xdist is installed AND -n auto resolved to a
# nonzero count AND the guard then demoted it.  Any of those breaking takes the
# header away, so a vacuous pass is not reachable.
#
# The target is tests/unit/test_models.py -- it executes for real (--collect-only
# never starts workers, so it cannot observe distribution) but builds no
# GoldenClient, so --record-gemini reaches no API and rewrites no file.
# ──────────────────────────────────────────────────────────────────────────────

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: xdist prints this only when it really distributes.  Matched instead of the
#: bare word "workers", which also appears in pytest's own test-name output.
XDIST_BANNER = "created:"


def _probe(*args: str) -> str:
    """Run a real pytest over the harmless model tests; return its output."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/unit/test_models.py",
         "-p", "no:cacheprovider", *args],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    return proc.stdout + proc.stderr


def test_the_auto_count_pyproject_asks_for_is_the_capped_one() -> None:
    """pyproject says "-n auto"; xdist resolves it through our hook, not its own.

    Without this the cap could be correct and still never consulted.
    """
    addopts = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["tool"]["pytest"]["ini_options"]["addopts"]
    assert addopts == ["-n", "auto"]
    assert conftest.pytest_xdist_auto_num_workers(None) == default_workers(os.cpu_count())


def test_record_gemini_beats_xdist_and_runs_serial() -> None:
    """The guard reaches a real pytest, and reaches it before xdist does.

    No -n is passed: the run inherits the parallel default from addopts, which
    is the situation the guard exists for.
    """
    out = _probe("--record-gemini")
    assert "forcing serial" in out, out
    assert "--record-gemini" in out, out
    assert XDIST_BANNER not in out, f"xdist still distributed: {out}"
