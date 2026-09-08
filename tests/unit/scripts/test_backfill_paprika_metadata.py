"""The backfill's decision-making, with no network in sight."""
from __future__ import annotations

from typing import Any, Dict

from scripts.backfill_paprika_metadata import normalise_title, plan_updates

_FIELDS = ("source", "notes", "rating", "nutritional_info", "description", "difficulty")


def _row(title: str = "Chicken Pie", recipe_id: str = "r1", **over: Any) -> Dict[str, Any]:
    """A recipes row as PostgREST returns it: all six null unless stated."""
    row: Dict[str, Any] = {"id": recipe_id, "title": title}
    row.update({field: None for field in _FIELDS})
    row.update(over)
    return row


class TestNormaliseTitle:
    def test_casefolds_and_drops_whitespace(self):
        assert normalise_title("  Chicken   Pie  ") == "chickenpie"
        assert normalise_title("CHICKEN PIE") == "chickenpie"

    def test_survives_a_missing_title(self):
        assert normalise_title(None) == ""
        assert normalise_title("") == ""

    def test_sees_past_the_punctuation_the_pipeline_rewrote(self):
        # All five are real pairs from the 2026-03-23 archive and the live library.
        # The pipeline straightened quotes, spaced an en dash and closed up a hyphen,
        # so 25 of the 82 entries that looked absent were only punctuated differently.
        assert normalise_title("Chongqing \u201cSmall\u201d Noodles") == normalise_title('Chongqing "Small" Noodles')
        assert normalise_title("Leshan \u201cBear\u2019s Paw\u201d Tofu") == normalise_title("Leshan \"Bear's Paw\" Tofu")
        assert normalise_title("brown butter\u2013braised giblets") == normalise_title("brown butter braised giblets")
        assert normalise_title("Homestyle Tofu") == normalise_title("Home-Style Tofu")
        assert normalise_title("all\u2019anziana") == normalise_title("all anziana")

    def test_does_not_see_past_a_real_difference(self):
        # "pasta in brodo" reached the library as "pasta in broro" -- a misread, not
        # punctuation. Matching it would be guessing which recipe the source belongs to.
        assert normalise_title("pasta in brodo") != normalise_title("pasta in broro")


class TestPlanUpdates:
    def test_fills_the_null_columns_from_the_archive(self):
        entries = [{"name": "Chicken Pie", "source": "Bon Appetit", "rating": 4}]

        plan = plan_updates(entries, [_row()])

        assert len(plan.updates) == 1
        assert plan.updates[0].recipe_id == "r1"
        assert plan.updates[0].fields == {"source": "Bon Appetit", "rating": 4}

    def test_never_overwrites_a_value_the_row_already_has(self):
        # The rating is editable from the kitchen page; a backfill that clobbers
        # what the cook set is data loss, not a repair.
        entries = [{"name": "Chicken Pie", "source": "Bon Appetit", "rating": 4}]

        plan = plan_updates(entries, [_row(rating=2)])

        assert plan.updates[0].fields == {"source": "Bon Appetit"}

    def test_a_second_run_changes_nothing(self):
        entries = [{"name": "Chicken Pie", "source": "Bon Appetit", "rating": 4}]

        plan = plan_updates(entries, [_row(source="Bon Appetit", rating=4)])

        assert plan.updates == []
        assert plan.untouched == 1

    def test_absent_in_the_archive_means_absent_here_too(self):
        # "" and 0 are how Paprika writes "nothing"; neither is a value to write.
        entries = [{"name": "Plain", "source": "", "rating": 0, "notes": "   "}]

        plan = plan_updates(entries, [_row(title="Plain")])

        assert plan.updates == []
        assert plan.untouched == 1

    def test_matches_across_case_and_spacing(self):
        entries = [{"name": "chicken   PIE", "source": "Bon Appetit"}]

        plan = plan_updates(entries, [_row(title="Chicken Pie")])

        assert len(plan.updates) == 1

    def test_skips_a_title_that_matches_two_rows(self):
        entries = [{"name": "Chicken Pie", "source": "Bon Appetit"}]
        rows = [_row(recipe_id="r1"), _row(recipe_id="r2")]

        plan = plan_updates(entries, rows)

        assert plan.updates == []
        assert plan.ambiguous == ["Chicken Pie"]

    def test_skips_a_title_that_appears_twice_in_the_archive(self):
        entries = [
            {"name": "Chicken Pie", "source": "Bon Appetit"},
            {"name": "Chicken Pie", "source": "Serious Eats"},
        ]

        plan = plan_updates(entries, [_row()])

        assert plan.updates == []
        assert plan.ambiguous == ["Chicken Pie"]

    def test_reports_an_entry_with_no_row_rather_than_creating_one(self):
        # The twenty recipes the 2026-09-04 import lost are a separate repair.
        entries = [{"name": "Never Imported", "source": "Bon Appetit"}]

        plan = plan_updates(entries, [_row()])

        assert plan.updates == []
        assert plan.unmatched == ["Never Imported"]

    def test_an_entry_with_no_name_is_reported_rather_than_matched(self):
        # A nameless entry has no key at all; it must not collide with a nameless row.
        entries = [{"source": "Bon Appetit"}]

        plan = plan_updates(entries, [_row()])

        assert plan.updates == []
        assert plan.unmatched == [""]
