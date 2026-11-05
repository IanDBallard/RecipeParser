import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "probe_yield_kinds", Path(__file__).resolve().parents[3] / "scripts" / "probe_yield_kinds.py"
)
probe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(probe)

CASES = [
    ("a", "Crust", "component", "full", "shortcrust"),
    ("b", "Stew", "servings", None, None),
    ("c", "Crumble", "servings", "crumble", None),
]


def test_score_counts_matches_per_run_and_agreement_across_runs():
    runs = [
        {"a": ("component", "full", "shortcrust"), "b": ("servings", None, None), "c": ("servings", "crumble", None)},
        {"a": ("component", "full", "shortcrust"), "b": ("pieces", None, None), "c": ("servings", "none", None)},
    ]
    s = probe.score(CASES, runs)
    assert s["matches"] == [3, 1]
    assert s["agree"] == 1  # only a gave the same answer both times
    assert [m["id"] for m in s["misses"]] == ["b", "c"]
    assert s["misses"][0]["answers"] == [("servings", None, None), ("pieces", None, None)]


def test_a_blank_top_or_pastry_is_not_scored():
    run = {
        "a": ("component", "full", "shortcrust"),
        "b": ("servings", "full", "puff"),
        "c": ("servings", "crumble", "crumb"),
    }
    assert probe.score(CASES, [run])["matches"] == [3]


def test_a_given_top_or_pastry_must_match_and_the_kind_alone_is_not_enough():
    run = {"a": ("component", "none", "shortcrust"), "b": ("servings", None, None), "c": ("servings", "crumble", None)}
    s = probe.score(CASES, [run])
    assert s["matches"] == [2] and [m["id"] for m in s["misses"]] == ["a"]
    run["a"] = ("component", "full", "puff")
    assert probe.score(CASES, [run])["matches"] == [2]


def test_a_recipe_missing_from_a_run_is_a_miss_not_a_crash():
    s = probe.score(CASES[:1], [{}])
    assert s["matches"] == [0] and s["agree"] == 0 and s["misses"][0]["answers"] == ["(not read)"]


def test_answer_of_reads_the_checked_spec():
    spec = {"kind": "servings", "container": {"top": "lattice", "pastry": "puff"}}
    assert probe.answer_of(spec) == ("servings", "lattice", "puff")
    assert probe.answer_of({"kind": "pieces", "container": None}) == ("pieces", None, None)
    assert probe.answer_of(None) == (None, None, None)


def test_the_probe_reads_the_rows_yield_columns():
    assert all(c in probe.COLUMNS for c in ("servings_min", "servings_max", "servings_note"))
