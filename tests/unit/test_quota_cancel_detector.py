"""config/architecture/detectors/quota_cancel.py - the fail-loud.quota-cancel-propagate rule."""

import ast
import importlib.util
import textwrap
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "config" / "architecture" / "detectors" / "quota_cancel.py"
_spec = importlib.util.spec_from_file_location("quota_cancel_detector", _PATH)
qc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(qc)

LISTED = ["JobCancelledError", "DailyQuotaExhaustedError"]


def found(src: str, added=None, ancestors=("RecipeParserError",)):
    return qc.uncovered_handlers(ast.parse(textwrap.dedent(src)), added, LISTED, list(ancestors))


@pytest.mark.parametrize("clause", ["except:", "except Exception:", "except BaseException:",
                                    "except (ValueError, Exception):", "except builtins.Exception:",
                                    "except RecipeParserError:", "except Exception as exc:"])
def test_a_catching_clause_that_carries_on_is_reported(clause):
    assert found(f"try:\n    work()\n{clause}\n    log()\n") == [(3, LISTED)]


def test_a_narrow_clause_is_not_reported():
    assert found("try:\n    work()\nexcept ValueError:\n    log()\n") == []


@pytest.mark.parametrize("body", ["raise", "log()\n    raise", "if bad:\n        raise", "raise exc"])
def test_a_clause_that_re_raises_is_clean(body):
    assert found(f"try:\n    work()\nexcept Exception as exc:\n    {body}\n") == []


def test_raising_another_type_is_not_letting_it_through():
    assert found("try:\n    work()\nexcept Exception as exc:\n    raise ValueError() from exc\n") == [(3, LISTED)]


def test_an_earlier_re_raising_clause_covers_both():
    src = """
    try:
        work()
    except (JobCancelledError, DailyQuotaExhaustedError):
        raise
    except Exception:
        log()
    """
    assert found(src) == []


def test_an_earlier_clause_covering_one_names_the_other():
    src = """
    try:
        work()
    except JobCancelledError:
        raise
    except Exception:
        log()
    """
    assert found(src) == [(6, ["DailyQuotaExhaustedError"])]


def test_an_earlier_re_raising_ancestor_covers_both():
    src = """
    try:
        work()
    except RecipeParserError:
        raise
    except Exception:
        log()
    """
    assert found(src) == []


def test_a_clause_naming_a_listed_exception_is_deliberate_handling():
    assert found("try:\n    work()\nexcept JobCancelledError:\n    mark_cancelled()\n") == []


def test_a_re_raise_inside_a_nested_def_does_not_count():
    src = """
    try:
        work()
    except Exception:
        def later():
            raise
    """
    assert found(src) == [(4, LISTED)]


def test_a_bare_raise_in_a_nested_except_re_raises_the_inner_error_not_this_one():
    src = """
    try:
        work()
    except Exception:
        try:
            cleanup()
        except OSError:
            raise
    """
    assert found(src) == [(4, LISTED)]


def test_except_star_is_checked_like_except():
    assert found("try:\n    work()\nexcept* Exception:\n    log()\n") == [(3, LISTED)]


def test_only_added_except_lines_are_reported():
    src = "try:\n    work()\nexcept Exception:\n    log()\n"
    assert found(src, added=frozenset({4})) == []
    assert found(src, added=frozenset({3})) == [(3, LISTED)]
    assert found(src, added=frozenset()) == []


def test_without_ancestors_an_ancestor_clause_is_narrow():
    assert found("try:\n    work()\nexcept RecipeParserError:\n    log()\n", ancestors=()) == []


@pytest.mark.parametrize("params", [{}, {"exceptions": []}, {"exceptions": "JobCancelledError"},
                                    {"exceptions": ["JobCancelledError", 3]}])
def test_bad_exceptions_param_is_refused(params):
    with pytest.raises(ValueError, match="params.exceptions"):
        qc._str_list(params, "exceptions", required=True)


def test_an_absent_optional_list_is_empty_but_a_scalar_is_refused():
    assert qc._str_list({}, "ancestors", required=False) == []
    with pytest.raises(ValueError, match="params.ancestors"):
        qc._str_list({"ancestors": "RecipeParserError"}, "ancestors", required=False)


def test_roots_limit_the_paths_checked():
    assert qc._under("recipeparser/core/pipeline.py", ["recipeparser"])
    assert not qc._under("scripts/x.py", ["recipeparser"])
    assert not qc._under("recipeparser_extra/x.py", ["recipeparser"])
