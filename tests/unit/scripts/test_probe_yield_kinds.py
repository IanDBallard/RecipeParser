import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "probe_yield_kinds", Path(__file__).resolve().parents[3] / "scripts" / "probe_yield_kinds.py"
)
probe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(probe)

CASES = [
    ("a", "Crust", "component", "full", "shortcrust", None, None),
    ("b", "Stew", "servings", None, None, None, None),
    ("c", "Crumble", "servings", "crumble", None, None, None),
]


def test_score_counts_matches_per_run_and_agreement_across_runs():
    runs = [
        {"a": ("component", "full", "shortcrust", None, None), "b": ("servings", None, None, None, None), "c": ("servings", "crumble", None, None, None)},
        {"a": ("component", "full", "shortcrust", None, None), "b": ("pieces", None, None, None, None), "c": ("servings", "none", None, None, None)},
    ]
    s = probe.score(CASES, runs)
    assert s["matches"] == [3, 1]
    assert s["agree"] == 1  # only a gave the same answer both times
    assert [m["id"] for m in s["misses"]] == ["b", "c"]
    assert s["misses"][0]["answers"] == [("servings", None, None, None, None), ("pieces", None, None, None, None)]


def test_a_blank_top_or_pastry_is_not_scored():
    run = {
        "a": ("component", "full", "shortcrust", None, None),
        "b": ("servings", "full", "puff", None, None),
        "c": ("servings", "crumble", "crumb", None, None),
    }
    assert probe.score(CASES, [run])["matches"] == [3]


def test_a_given_top_or_pastry_must_match_and_the_kind_alone_is_not_enough():
    run = {"a": ("component", "none", "shortcrust", None, None), "b": ("servings", None, None, None, None), "c": ("servings", "crumble", None, None, None)}
    s = probe.score(CASES, [run])
    assert s["matches"] == [2] and [m["id"] for m in s["misses"]] == ["a"]
    run["a"] = ("component", "full", "puff", None, None)
    assert probe.score(CASES, [run])["matches"] == [2]


def test_a_recipe_missing_from_a_run_is_a_miss_not_a_crash():
    s = probe.score(CASES[:1], [{}])
    assert s["matches"] == [0] and s["agree"] == 0 and s["misses"][0]["answers"] == ["(not read)"]


def test_answer_of_reads_the_checked_spec():
    spec = {"kind": "servings", "container": {"top": "lattice", "pastry": "puff"}}
    assert probe.answer_of(spec) == ("servings", "lattice", "puff", None, None)
    assert probe.answer_of({"kind": "pieces", "container": None}) == ("pieces", None, None, None, None)
    assert probe.answer_of(None) == (None, None, None, None, None)


def test_the_probe_reads_the_rows_yield_columns():
    assert all(c in probe.COLUMNS for c in ("servings_min", "servings_max", "servings_note"))


BOULE = ("d", "Boule", "pieces", None, None, 1, 900)


def test_a_given_count_and_size_must_match():
    assert probe.score([BOULE], [{"d": ("pieces", None, None, 1, 900)}])["matches"] == [1]
    assert probe.score([BOULE], [{"d": ("pieces", None, None, 2, 900)}])["matches"] == [0]
    assert probe.score([BOULE], [{"d": ("pieces", None, None, 1, 450)}])["matches"] == [0]


def test_a_blank_count_or_size_is_not_scored():
    blank = ("e", "Rolls", "pieces", None, None, None, None)
    assert probe.score([blank], [{"e": ("pieces", None, None, 7, 55)}])["matches"] == [1]
    count_only = ("f", "Naan", "pieces", None, None, 6, None)
    assert probe.score([count_only], [{"f": ("pieces", None, None, 6, 120)}])["matches"] == [1]


def test_answer_of_reads_count_and_size_from_pieces_or_area():
    pieces = {"kind": "pieces", "container": None, "pieces": {"noun": "roll", "count": 8, "weight_g": 90}}
    assert probe.answer_of(pieces) == ("pieces", None, None, 8, 90)
    wide = {"kind": "pieces", "pieces": {"noun": "pizza", "count": 2, "diameter_mm": 305}}
    assert probe.answer_of(wide) == ("pieces", None, None, 2, 305)
    area = {"kind": "component", "container": {"top": "none"}, "area": {"noun": "round", "count": 4, "diameter_mm": 300}}
    assert probe.answer_of(area) == ("component", "none", None, 4, 300)


def test_read_cases_reads_the_optional_columns(tmp_path):
    full = tmp_path / "a.csv"
    full.write_text(
        "id,title,expected_kind,expected_top,expected_pastry,expected_count,expected_size\n"
        "x,Boule,pieces,,,1,900\ny,Stew,servings,,,,\n",
        encoding="utf-8",
    )
    assert probe.read_cases(full) == [
        ("x", "Boule", "pieces", None, None, 1, 900),
        ("y", "Stew", "servings", None, None, None, None),
    ]
    old = tmp_path / "b.csv"
    old.write_text("id,title,expected_kind,expected_top,expected_pastry\nx,Boule,pieces,,\n", encoding="utf-8")
    assert probe.read_cases(old) == [("x", "Boule", "pieces", None, None, None, None)]
