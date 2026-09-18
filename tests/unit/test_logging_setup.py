"""configure_logging — the pipeline's INFO lines reach the console under uvicorn.

uvicorn's --log-level configures only its own loggers; the root logger is left
bare, so before this every ``logger.info`` in the package was invisible and the
API had to shout at WARNING to be seen at all.

pytest wraps every test in its own root-logger capture handlers, so a bare root
cannot be had here; the tests hand the function a fresh logger instead, which is
the same code path with the root as its default.
"""
from __future__ import annotations

import logging

import pytest

from recipeparser.logging_setup import configure_logging


@pytest.fixture
def bare(monkeypatch) -> logging.Logger:
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    logger = logging.Logger("a-bare-root-stand-in")
    logger.setLevel(logging.WARNING)
    return logger


def test_a_bare_logger_gets_one_console_handler_at_info(bare):
    configure_logging(bare)
    assert len(bare.handlers) == 1
    assert isinstance(bare.handlers[0], logging.StreamHandler)
    assert bare.level == logging.INFO


def test_log_level_comes_from_the_environment(bare, monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "debug")
    configure_logging(bare)
    assert bare.level == logging.DEBUG


def test_an_unknown_level_falls_back_to_info(bare, monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "loud")
    configure_logging(bare)
    assert bare.level == logging.INFO


def test_a_logger_someone_else_configured_is_left_alone(bare):
    theirs = logging.NullHandler()
    bare.addHandler(theirs)
    bare.setLevel(logging.ERROR)
    configure_logging(bare)
    assert bare.handlers == [theirs]
    assert bare.level == logging.ERROR


def test_calling_twice_installs_one_handler(bare):
    configure_logging(bare)
    configure_logging(bare)
    assert len(bare.handlers) == 1


def test_the_line_carries_the_logger_name(bare):
    configure_logging(bare)
    record = logging.LogRecord("recipeparser.core.pipeline", logging.INFO, __file__, 1, "Job done", None, None)
    assert bare.handlers[0].format(record) == "INFO recipeparser.core.pipeline: Job done"


def test_the_default_target_is_the_root_logger():
    # The API calls it with no argument; the root is what uvicorn leaves bare.
    assert configure_logging.__defaults__ == (None,)
