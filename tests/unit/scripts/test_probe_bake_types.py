import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "probe_bake_types", Path(__file__).resolve().parents[3] / "scripts" / "probe_bake_types.py"
)
probe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(probe)


def test_score_counts_matches_per_run_and_agreement_across_runs():
    cases = [("a", "Pie", "pie-savoury"), ("b", "Lasagne", None), ("c", "Sponge", "sponge")]
    runs = [
        {"a": "pie-savoury", "b": None, "c": "butter-cake"},
        {"a": "pie-savoury", "b": "pie-savoury", "c": "butter-cake"},
    ]
    s = probe.score(cases, runs)
    assert s["matches"] == [2, 1]
    assert s["agree"] == 2  # a and c gave the same answer both times; b did not
    assert [m["id"] for m in s["misses"]] == ["b", "c"]
    assert s["misses"][1]["answers"] == ["butter-cake", "butter-cake"]


def test_score_treats_an_empty_expected_as_no_bake_type():
    s = probe.score([("a", "Roast", None)], [{"a": None}])
    assert s["matches"] == [1] and s["misses"] == []


def test_a_recipe_missing_from_a_run_is_a_miss_not_a_crash():
    s = probe.score([("a", "Pie", "pie-savoury")], [{}])
    assert s["matches"] == [0] and s["misses"][0]["answers"] == ["(not read)"]
