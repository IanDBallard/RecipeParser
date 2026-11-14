"""The look goldens script's scoring and report. Nothing here calls Anthropic or loads a page."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_SPEC = importlib.util.spec_from_file_location("look_goldens", _ROOT / "scripts" / "look_goldens.py")
assert _SPEC is not None and _SPEC.loader is not None
goldens = importlib.util.module_from_spec(_SPEC)
sys.modules["look_goldens"] = goldens  # dataclasses look the module up by name
_SPEC.loader.exec_module(goldens)


HOSTS = ("food52.com", "nytimes.com")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://food52.com/recipes/1", True),
        ("https://www.food52.com/recipes/1", True),
        ("https://cooking.nytimes.com/recipes/1", True),
        ("http://food52.com/recipes/1", False),
        ("https://notfood52.com/recipes/1", False),
        ("https://evil.example/food52.com", False),
        ("https://evil.com\\@food52.com/recipes/1", False),  # a browser reads evil.com
        ("https://food52.com@evil.com/recipes/1", False),
    ],
)
def test_look_goldens_scores_host(url: str, expected: bool) -> None:
    assert goldens.score_host(url, HOSTS) is expected


def test_score_candidates_scores_every_offered_candidate() -> None:
    """The raw offers are scored before the checks, so the report shows what the checks removed."""
    tried: list[str] = []
    on = goldens.RawCandidate(gap=0, url="https://www.food52.com/recipes/1/", title="A", note="")
    unread = goldens.RawCandidate(gap=0, url="https://food52.com/recipes/2", title="C", note="")
    off = goldens.RawCandidate(gap=1, url="https://evil.example/x", title="B", note="")
    kept = [goldens.RawCandidate(gap=0, url="https://food52.com/recipes/1", title="A", note="")]
    scored = goldens.score_candidates([on, unread, off], kept, HOSTS, lambda u: tried.append(u) or True)
    assert [(s.on_host, s.loads, s.kept) for s in scored] == [
        (True, True, True),
        (True, True, False),
        (False, None, False),
    ]
    assert tried == [on.url, unread.url]


def test_yaml_has_ten_looks_each_with_gaps_and_hosts() -> None:
    entries = goldens.load_goldens(_ROOT / "scripts" / "look_goldens.yaml")
    assert len(entries) == 10 and len({e.id for e in entries}) == 10
    refused = {"seriouseats.com", "theguardian.com", "nytimes.com"}
    for e in entries:
        assert e.gaps and 1 <= len(e.hosts) <= 8
        assert not refused & set(e.hosts)
        assert all(h == h.lower() and not h.startswith("www.") for h in e.hosts)


def test_cost_counts_tokens_and_searches() -> None:
    use = goldens.Usage()
    use.add(
        SimpleNamespace(
            input_tokens=1_000_000,
            output_tokens=100_000,
            server_tool_use=SimpleNamespace(web_search_requests=3, web_fetch_requests=5),
        )
    )
    assert (use.searches, use.fetches, use.calls) == (3, 5, 1)
    assert goldens.estimate_cost(use) == pytest.approx(4.0 + 2.0 + 0.03)


def test_report_shows_marks_and_cost() -> None:
    result = goldens.LookResult(id="vegan-side", question="q", seconds=12.0)
    c = goldens.RawCandidate(gap=0, url="https://food52.com/recipes/1", title="Gravy", note="Vegan · 1 hr")
    d = goldens.RawCandidate(gap=0, url="https://evil.example/x", title="Evil", note="")
    result.scored = [goldens.Scored(c, True, False, True), goldens.Scored(d, False, None, False)]
    report = goldens.render_report([result])
    assert "## vegan-side" in report
    assert "host ✓ · loads ✗ · kept ✓ · Gravy" in report
    assert "host ✗ · loads – · kept ✗ · Evil" in report
    assert "Offered 2, kept 1." in report


def test_main_without_yes_exits_before_building_anything(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import anthropic

    def boom(*a: object, **k: object) -> None:
        raise AssertionError("built a client")

    monkeypatch.setattr(anthropic, "Anthropic", boom)
    assert goldens.main(["--out", str(tmp_path / "r.md")]) == 2
    assert not (tmp_path / "r.md").exists()
