import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_SPEC = importlib.util.spec_from_file_location("ask_goldens", _ROOT / "scripts" / "ask_goldens.py")
assert _SPEC is not None and _SPEC.loader is not None
goldens = importlib.util.module_from_spec(_SPEC)
sys.modules["ask_goldens"] = goldens  # dataclasses look the module up by name
_SPEC.loader.exec_module(goldens)

ID_A = "11111111-1111-4111-8111-111111111111"
ID_B = "22222222-2222-4222-8222-222222222222"


def test_yaml_has_thirty_entries_and_enough_out_of_scope():
    entries = goldens.load_goldens(_ROOT / "scripts" / "ask_goldens.yaml")
    assert len(entries) == 30
    assert len({e.id for e in entries}) == 30
    assert sum(1 for e in entries if e.expect_kind == "out_of_scope") >= 5
    assert any(e.question == "How long do I boil an egg?" for e in entries)
    assert {e.expect_kind for e in entries if e.expect_kind} <= {"recipes", "text", "none", "out_of_scope"}


def test_every_previous_names_an_earlier_entry():
    entries = goldens.load_goldens(_ROOT / "scripts" / "ask_goldens.yaml")
    seen: set[str] = set()
    refinements = 0
    for e in entries:
        if e.previous is not None:
            refinements += 1
            assert e.previous in seen
        seen.add(e.id)
    assert 2 <= refinements <= 3


def test_load_rejects_a_forward_previous(tmp_path):
    p = tmp_path / "g.yaml"
    p.write_text("- {id: a, question: q, previous: b}\n- {id: b, question: r}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="previous"):
        goldens.load_goldens(p)


def test_cost_arithmetic():
    use = goldens.Usage(input_tokens=1_000_000, output_tokens=1_000_000, cache_read=1_000_000, cache_write=1_000_000)
    # 4 + 20 + 0.20 + 5
    assert goldens.estimate_cost(use) == pytest.approx(29.20)
    assert goldens.estimate_cost(goldens.Usage()) == 0.0


def test_usage_adds_calls_from_response_usage_objects():
    class U:
        input_tokens = 100
        output_tokens = 50
        cache_read_input_tokens = 400
        cache_creation_input_tokens = None

    total = goldens.Usage()
    total.add(U())
    total.add(U())
    assert (total.input_tokens, total.output_tokens, total.cache_read, total.cache_write) == (200, 100, 800, 0)
    assert total.calls == 2


def _result(**kw):
    base = dict(
        id="e1",
        question="A menu?",
        expect_kind="recipes",
        kind="recipes",
        answer={
            "kind": "recipes",
            "alternatives": [
                {
                    "label": "Fast",
                    "note": "Quick one.",
                    "groups": [
                        {"heading": "Main", "recipe_ids": [ID_A]},
                        {"heading": None, "recipe_ids": [ID_B]},
                    ],
                }
            ],
        },
        titles={ID_A: "Roast chicken"},
        seconds=12.34,
        usage=goldens.Usage(input_tokens=1000, output_tokens=500, calls=3),
        error=None,
    )
    base.update(kw)
    return goldens.Result(**base)


def test_report_shows_kind_titles_note_label_and_cost():
    text = goldens.render_report([_result()])
    assert "e1" in text and "A menu?" in text
    assert "✓" in text
    assert "Roast chicken" in text and ID_B in text  # an unknown title falls back to the id
    assert "Fast" in text and "Quick one." in text and "Main" in text
    assert "12.3" in text and "$0.014" in text


def test_report_marks_a_kind_mismatch_and_prints_text_and_errors():
    said = {"kind": "text", "text": "Four.", "recipe_ids": []}
    wrong = _result(id="e2", expect_kind="none", kind="text", answer=said)
    failed = _result(id="e3", kind=None, answer=None, error="timeout")
    text = goldens.render_report([wrong, failed])
    assert "✗" in text and "Four." in text and "timeout" in text
    assert "Total" in text


def test_total_cost_sums_results():
    a, b = _result(), _result(id="e9")
    assert goldens.total_cost([a, b]) == pytest.approx(2 * goldens.estimate_cost(a.usage))


def test_answer_ids_reads_a_dumped_answer():
    assert goldens.dumped_ids(_result().answer) == [ID_A, ID_B]
    assert goldens.dumped_ids({"kind": "text", "text": "x", "recipe_ids": [ID_B]}) == [ID_B]
    assert goldens.dumped_ids({"kind": "out_of_scope"}) == []
