import gzip
import json
import zipfile
from pathlib import Path

from recipeparser.io.readers.paprika import PaprikaReader
from scripts.extract_unmatched_paprika import read_members, select_unmatched, summarise, write_archive


def _archive(path: Path, entries: dict[str, dict]) -> Path:
    """A .paprikarecipes: a ZIP of gzip-compressed JSON members, as Paprika exports one."""
    with zipfile.ZipFile(path, "w") as zf:
        for name, entry in entries.items():
            zf.writestr(name, gzip.compress(json.dumps(entry).encode("utf-8")))
    return path


def _row(i: int, title: str) -> dict:
    return {"id": f"r{i}", "title": title}


def test_read_members_keeps_the_name_and_the_bytes_beside_the_entry(tmp_path):
    src = _archive(tmp_path / "e.paprikarecipes", {"a.paprikarecipe": {"name": "Tomato Soup"}, "notes.txt": {}})
    members = read_members(src)
    assert [m[0] for m in members] == ["a.paprikarecipe"]
    assert members[0][2] == {"name": "Tomato Soup"}
    assert gzip.decompress(members[0][1]) == json.dumps({"name": "Tomato Soup"}).encode("utf-8")


def test_select_unmatched_uses_the_restore_rule():
    members = [
        ("a.paprikarecipe", b"a", {"name": "Tomato Soup"}),
        ("b.paprikarecipe", b"b", {"name": "Brown Butter\u2014Braised Leeks"}),
        ("c.paprikarecipe", b"c", {"name": "Lost Cake"}),
        ("d.paprikarecipe", b"d", {"name": "Lost Cake"}),
        ("e.paprikarecipe", b"e", {"name": ""}),
    ]
    rows = [_row(0, "Tomato Soup"), _row(1, "brown butter braised leeks")]
    unmatched = select_unmatched(members, rows)
    # Both Lost Cake members are unmatched (no row shares the title); the blank-named one is skipped.
    assert [m[0] for m in unmatched] == ["c.paprikarecipe", "d.paprikarecipe"]


def test_write_archive_produces_a_paprikarecipes_the_reader_opens(tmp_path):
    src = _archive(
        tmp_path / "e.paprikarecipes",
        {"a.paprikarecipe": {"name": "Kept"}, "c.paprikarecipe": {"name": "Lost Cake"}},
    )
    members = select_unmatched(read_members(src), [_row(0, "Kept")])
    out = tmp_path / "e-unmatched.paprikarecipes"
    assert write_archive(out, members) == 1
    assert [e["name"] for e in PaprikaReader().read_entries(out)] == ["Lost Cake"]


def test_read_members_skips_a_member_whose_gzip_body_is_truncated(tmp_path):
    path = tmp_path / "e.paprikarecipes"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("a.paprikarecipe", gzip.compress(json.dumps({"name": "Kept"}).encode("utf-8")))
        truncated = gzip.compress(json.dumps({"name": "Cut"}).encode("utf-8"))[:12]
        zf.writestr("b.paprikarecipe", truncated)
    members = read_members(path)
    assert [m[0] for m in members] == ["a.paprikarecipe"]


def test_read_members_skips_a_member_whose_bytes_are_not_gzip_or_utf8_json(tmp_path):
    path = tmp_path / "e.paprikarecipes"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("a.paprikarecipe", gzip.compress(json.dumps({"name": "Kept"}).encode("utf-8")))
        zf.writestr("b.paprikarecipe", b"\xff\xfe\x00not json")
    members = read_members(path)
    assert [m[0] for m in members] == ["a.paprikarecipe"]


def test_summarise_counts_titles_members_and_blanks():
    members = [
        ("a.paprikarecipe", b"a", {"name": "Ghost Soup"}),
        ("b.paprikarecipe", b"b", {"name": "Ghost Soup"}),
        ("c.paprikarecipe", b"c", {"name": "Vanished Tart"}),
        ("d.paprikarecipe", b"d", {"name": ""}),
    ]
    rows = [_row(0, "Tomato Soup")]
    summary = summarise(members, rows)
    assert summary.unmatched_members == 3
    assert summary.unmatched_titles == 2
    assert summary.blank_titles == 1
    assert summary.surplus_titles == []


def test_summarise_flags_a_title_with_more_entries_than_rows():
    # Two "Lost Cake" entries, one row: one of them is a lost recipe, but
    # select_unmatched cannot tell which, so it writes neither and this is
    # the only place that reports the group at all.
    members = [
        ("a.paprikarecipe", b"a", {"name": "Lost Cake"}),
        ("b.paprikarecipe", b"b", {"name": "Lost Cake"}),
    ]
    rows = [_row(0, "Lost Cake")]
    summary = summarise(members, rows)
    assert summary.surplus_titles == ["Lost Cake"]
    assert select_unmatched(members, rows) == []
