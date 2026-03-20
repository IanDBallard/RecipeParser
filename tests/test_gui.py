"""
Tests for recipeparser.gui — run-config logic used by the Parse tab.

GUI tests here are logic-only: we test the function that turns free-tier and
concurrency settings into (rpm, concurrency) for the pipeline. We do not
instantiate CustomTkinter widgets or require a display; full UI tests would
need a headless CTk or display server.
"""
import pytest

from recipeparser.adapters.gui import _parse_run_config


class TestParseRunConfig:

    def test_free_tier_returns_rpm_5_concurrency_1(self):
        rpm, concurrency = _parse_run_config(True, "1")
        assert rpm == 5
        assert concurrency == 1

    def test_free_tier_ignores_concurrency_str(self):
        """When free tier is on, concurrency is always 1 regardless of dropdown."""
        rpm, concurrency = _parse_run_config(True, "10")
        assert rpm == 5
        assert concurrency == 1

    def test_paid_tier_no_rpm_concurrency_used(self):
        rpm, concurrency = _parse_run_config(False, "5")
        assert rpm is None
        assert concurrency == 5

    def test_paid_tier_concurrency_clamped_to_10(self):
        rpm, concurrency = _parse_run_config(False, "15")
        assert rpm is None
        assert concurrency == 10

    def test_paid_tier_concurrency_clamped_to_1(self):
        rpm, concurrency = _parse_run_config(False, "0")
        assert rpm is None
        assert concurrency == 1

    def test_paid_tier_concurrency_1_and_10_valid(self):
        _, c1 = _parse_run_config(False, "1")
        _, c10 = _parse_run_config(False, "10")
        assert c1 == 1
        assert c10 == 10
