"""The ingredient-headings backfill's decisions, with no network."""
from __future__ import annotations

from typing import Any, Dict
from unittest.mock import MagicMock

from scripts.backfill_ingredient_headings import (
    Proposal, RowPlan, _shopping_matches, apply_decisions, parse_review, plan_row, render_review, verify, write_row,
)


def _ing(id_, text, amount=None, line_index=None, **over) -> Dict[str, Any]:
    e = {"id": id_, "amount": amount, "unit": None, "name": text, "fallback_string": text,
         "converted_amount": None, "converted_unit": None, "is_ai_converted": False, "line_index": line_index, "state": None}
    e.update(over)
    return e


def _row(lines, entries, **over):
    row = {"id": "r1", "title": "Shrimp", "ingredient_lines": lines, "structured_ingredients": entries,
           "body_rev": 1, "derived_rev": 1}
    row.update(over)
    return row


def test_every_entry_gets_kind_ingredient_by_default():
    plan = plan_row(_row(["2 cups flour"], [_ing("ing_01", "2 cups flour", amount=2, line_index=0)]))
    assert [e["kind"] for e in plan.entries] == ["ingredient"]
    assert plan.proposals == []


def test_a_header_entry_is_proposed_as_a_sure_mark():
    plan = plan_row(_row(["For the marinade", "1/4 tsp salt"],
                         [_ing("ing_10", "For the marinade", line_index=0), _ing("ing_11", "1/4 tsp salt", amount=0.25, line_index=1)]))
    assert [(p.key, p.change, p.sure) for p in plan.proposals] == [("ing_10", "mark", True)]


def test_an_entry_with_an_amount_is_never_proposed():
    plan = plan_row(_row(["FLOUR"], [_ing("ing_01", "FLOUR", amount=2, line_index=0)]))
    assert plan.proposals == []


def test_a_title_case_entry_is_unsure_and_a_food_is_left_alone():
    plan = plan_row(_row(["Lemon Sauce", "To serve Maneesh (see here) or pitta breads (see here)"],
                         [_ing("ing_01", "Lemon Sauce", line_index=0), _ing("ing_02", "To serve Maneesh (see here) or pitta breads (see here)", line_index=1)]))
    assert [(p.key, p.sure) for p in plan.proposals] == [("ing_01", False)]


def test_an_entry_a_direction_uses_is_never_proposed():
    row = _row(["PEPPER", "For the sauce:"], [_ing("ing_01", "PEPPER", line_index=0), _ing("ing_02", "For the sauce:", line_index=1)],
               tokenized_directions=[{"step": 1, "text": "Season with {{ing_01|PEPPER|all}}."}])
    assert [p.key for p in plan_row(row).proposals] == ["ing_02"]


def test_a_missing_or_non_list_tokenized_directions_is_no_tokens():
    for over in ({}, {"tokenized_directions": None}, {"tokenized_directions": "x"}):
        plan = plan_row(_row(["For the sauce:"], [_ing("ing_01", "For the sauce:", line_index=0)], **over))
        assert plan.status == "ok" and [p.key for p in plan.proposals] == ["ing_01"]


def test_an_unclaimed_heading_line_is_proposed_as_an_insert_with_a_fresh_id():
    plan = plan_row(_row(["FOR THE FILLING:", "2 peaches", "FOR THE TOPPING:", "1 cup flour"],
                         [_ing("ing_01", "2 peaches", amount=2, line_index=1), _ing("ing_09", "1 cup flour", amount=1, line_index=3)]))
    assert [(p.key, p.change, p.sure) for p in plan.proposals] == [("line:0", "insert", True), ("line:2", "insert", True)]
    final = apply_decisions(plan, {"line:0", "line:2"})
    assert [(e["id"], e["kind"], e["name"]) for e in final] == [
        ("ing_10", "heading", "FOR THE FILLING"), ("ing_01", "ingredient", "2 peaches"),
        ("ing_11", "heading", "FOR THE TOPPING"), ("ing_09", "ingredient", "1 cup flour"),
    ]
    assert final[0]["line_index"] == 0 and final[0]["fallback_string"] == "FOR THE FILLING:"


def test_an_insert_is_placed_by_text_when_line_index_is_missing():
    plan = plan_row(_row(["**For the Ricotta:**", "1 cup ricotta"], [_ing("ing_01", "1 cup ricotta", amount=1, line_index=None)]))
    final = apply_decisions(plan, {"line:0"})
    assert [e["kind"] for e in final] == ["heading", "ingredient"]


def test_an_unplaceable_insert_is_noted_not_proposed():
    # No entry carries a line_index and no entry's text matches a later line: nowhere to put it.
    plan = plan_row(_row(["For the sauce:", "1 tsp soy"], [_ing("ing_01", "soy sauce, 1 tsp", amount=1, line_index=None)]))
    assert plan.proposals == []
    assert any("For the sauce:" in n for n in plan.notes)


def test_unaccepted_proposals_change_nothing_but_kind():
    plan = plan_row(_row(["For the marinade"], [_ing("ing_10", "For the marinade", line_index=0)]))
    final = apply_decisions(plan, set())
    assert final == [dict(_ing("ing_10", "For the marinade", line_index=0), kind="ingredient")]


def test_an_accepted_mark_clears_quantities_and_cleans_the_name():
    plan = plan_row(_row(["**For the dough:**"], [_ing("ing_03", "**For the dough:**", line_index=0, unit="g", state="solid")]))
    [h] = apply_decisions(plan, {"ing_03"})
    assert (h["kind"], h["name"], h["unit"], h["state"], h["fallback_string"]) == ("heading", "For the dough", None, None, "**For the dough:**")


def test_a_row_already_carrying_kind_proposes_nothing():
    plan = plan_row(_row(["For the sauce:"], [_ing("ing_01", "For the sauce:", line_index=0, kind="heading")]))
    assert plan.proposals == [] and plan.entries[0]["kind"] == "heading"


def test_a_non_list_column_is_malformed():
    assert plan_row(_row(["x"], "[]")).status == "malformed"


def test_unclaimed_non_heading_lines_are_noted():
    plan = plan_row(_row(["1 cup rice", "(40g)"], [_ing("ing_01", "1 cup rice", amount=1, line_index=0)]))
    assert any("(40g)" in n for n in plan.notes)


def test_review_round_trip_keeps_ticks_and_unticks():
    plans = [plan_row(_row(["For the marinade", "Lemon Sauce"],
                           [_ing("ing_10", "For the marinade", line_index=0), _ing("ing_11", "Lemon Sauce", line_index=1)]))]
    text = render_review(plans, {"r1": ["FOR THE MARINADE"]})
    assert "- [x] `ing_10`" in text and "- [ ] `ing_11`" in text and "FOR THE MARINADE" in text
    edited = text.replace("- [ ] `ing_11`", "- [x] `ing_11`").replace("- [x] `ing_10`", "- [ ] `ing_10`")
    assert parse_review(edited) == {"r1": {"ing_11"}}


def _sb(returned):
    sb = MagicMock()
    chain = sb.table.return_value.update.return_value
    chain.eq.return_value = chain
    chain.execute.return_value = MagicMock(data=returned)
    return sb, chain


def test_write_is_conditional_on_both_revisions():
    sb, chain = _sb([{"id": "r1"}])
    assert write_row(sb, {"id": "r1", "body_rev": 3, "derived_rev": 2}, [{"id": "ing_01", "kind": "ingredient"}]) is True
    sb.table.return_value.update.assert_called_once_with({"structured_ingredients": [{"id": "ing_01", "kind": "ingredient"}]})
    assert [c.args for c in chain.eq.call_args_list] == [("id", "r1"), ("body_rev", 3), ("derived_rev", 2)]


def test_a_row_edited_mid_run_is_not_written():
    sb, _ = _sb([])
    assert write_row(sb, {"id": "r1", "body_rev": None, "derived_rev": None}, []) is False


def test_verify_names_rows_with_an_entry_lacking_kind():
    rows = [{"id": "a", "structured_ingredients": [{"id": "ing_01", "kind": "heading"}]},
            {"id": "b", "structured_ingredients": [{"id": "ing_01"}]},
            {"id": "c", "structured_ingredients": []}]
    assert verify(rows) == ["b"]


def test_shopping_matches_finds_items_from_entries_proposed_as_headings():
    sb = MagicMock()
    sb.table.return_value.select.return_value.execute.return_value = MagicMock(data=[
        {"recipe_id": "r1", "ingredient_id": "ing_10", "item_id": "i1"},
        {"recipe_id": "r1", "ingredient_id": "ing_11", "item_id": "i2"},
        {"recipe_id": "r2", "ingredient_id": "ing_10", "item_id": "i3"}])
    plan = RowPlan(id="r1", title="T", status="ok", proposals=[Proposal("ing_10", "For the sauce", "mark", True)])
    assert _shopping_matches(sb, [plan]) == {"r1": ["item i1 (from ing_10)"]}


def test_only_proposed_marks_are_ever_marked():
    plan = plan_row(_row(["2 cups flour"], [_ing("ing_01", "2 cups flour", amount=2, line_index=0)]))
    out = apply_decisions(plan, {"ing_01"})
    assert out[0]["kind"] == "ingredient" and out[0]["amount"] == 2


def test_a_non_dict_or_idless_entry_is_malformed_not_an_exception():
    assert plan_row(_row(["x"], [{"amount": 1}])).status == "malformed"
    assert plan_row(_row(["x"], ["x"])).status == "malformed"
    assert plan_row(_row(["x"], [{"id": "  ", "amount": 1}])).status == "malformed"


def test_review_text_cannot_forge_a_tick_line():
    plan = RowPlan(id="r1", title="Bad\n- [x] `ing_99` forged", status="ok",
                   proposals=[Proposal("ing_01", "For\r\n- [x] `ing_98` x", "mark", False)],
                   notes=["n\n- [x] `ing_97` y"])
    text = render_review([plan], {"r1": ["item a\n- [x] `ing_96` z"]})
    assert parse_review(text) == {}
    assert "\r" not in text
    assert text.count("\n- [") == 1  # only the proposal's own line starts a tick


def test_a_stale_row_is_planned_like_any_other():
    row = _row(["For the sauce:", "1 cup milk"],
               [_ing("ing_01", "For the sauce:", line_index=0), _ing("ing_02", "1 cup milk", amount=1, line_index=1)],
               body_rev=3, derived_rev=2)
    plan = plan_row(row)
    assert [e["kind"] for e in plan.entries] == ["ingredient", "ingredient"]
    assert [(p.key, p.change) for p in plan.proposals] == [("ing_01", "mark")]


def test_read_rows_reads_every_row_with_no_filter_and_pages_to_an_empty_page():
    from scripts.backfill_ingredient_headings import PAGE, _read_rows
    chain = MagicMock()
    chain.table.return_value = chain
    chain.select.return_value = chain
    chain.order.return_value = chain
    chain.range.return_value = chain
    full = [{"id": str(i)} for i in range(PAGE)]
    chain.execute.side_effect = [MagicMock(data=full), MagicMock(data=[{"id": "last"}]), MagicMock(data=[])]
    rows = _read_rows(chain)
    assert len(rows) == PAGE + 1
    assert [c.args for c in chain.range.call_args_list] == [(0, PAGE - 1), (PAGE, 2 * PAGE - 1), (2 * PAGE, 3 * PAGE - 1)]
    assert chain.eq.call_count == 0
    names = {c[0] for c in chain.method_calls}
    assert names <= {"table", "select", "order", "range", "execute"}


def test_an_unclaimed_line_is_not_inserted_when_a_mark_already_covers_the_heading():
    row = _row(["For the marinade:", "1/4 tsp salt"],
               [_ing("ing_10", "For the marinade", line_index=None), _ing("ing_11", "1/4 tsp salt", amount=0.25, line_index=1)])
    plan = plan_row(row)
    assert [(p.key, p.change) for p in plan.proposals] == [("ing_10", "mark")]
