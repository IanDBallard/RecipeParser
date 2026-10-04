"""scripts/backfill_title_case.py — plans only real changes and writes one guarded transaction."""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "backfill_title_case", Path(__file__).parents[3] / "scripts" / "backfill_title_case.py")
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)
plan_titles, render_sql = mod.plan_titles, mod.render_sql

A = "00000000-0000-0000-0000-00000000000a"
B = "00000000-0000-0000-0000-00000000000b"


def test_only_titles_whose_case_changes_are_planned():
    rows = [{"id": A, "title": "MOM'S MEATLOAF"}, {"id": B, "title": "Beef Stew"}]
    assert plan_titles(rows) == [(A, "MOM'S MEATLOAF", "Mom's Meatloaf")]


def test_the_sql_disables_the_body_rev_trigger_around_the_update_and_re_enables_it():
    sql = render_sql([(A, "MOM'S MEATLOAF", "Mom's Meatloaf")], commit=False)
    off = sql.index("disable trigger recipes_own_body_rev")
    update = sql.index("update public.recipes")
    on = sql.index("enable trigger recipes_own_body_rev")
    assert off < update < on


def test_quotes_are_escaped_and_the_update_is_guarded_by_the_old_title():
    sql = render_sql([(A, "MOM'S MEATLOAF", "Mom's Meatloaf")], commit=False)
    assert "'MOM''S MEATLOAF'" in sql and "'Mom''s Meatloaf'" in sql
    assert "r.title = v.old_title" in sql


def test_the_transaction_rolls_back_unless_asked_to_commit():
    plan = [(A, "X", "Y")]
    assert render_sql(plan, commit=False).rstrip().endswith("rollback;")
    assert render_sql(plan, commit=True).rstrip().endswith("commit;")
