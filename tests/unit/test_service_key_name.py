"""One service-key variable name, and a loud failure for the old one (spec 4.7)."""
from __future__ import annotations

from pathlib import Path

import pytest

from recipeparser.adapters.api import check_service_key_name
from recipeparser.io.category_sources.supabase_source import SupabaseCategorySource

ROOT = Path(__file__).resolve().parents[2]

# Written as a concatenation, not a literal, so this test file does not match
# the scan it performs below (see test_no_module_reads_the_legacy_name).
_LEGACY_KEY_NAME = "SUPABASE_SERVICE" + "_KEY"

# File types the legacy name has actually turned up in (source, docs, and the
# writer/category-source docstrings) plus the config/launcher formats a fix
# like this could plausibly hide in.
_SCAN_GLOBS = (
    "*.py",
    "*.md",
    "*.yml",
    "*.yaml",
    "*.ps1",
    "*.toml",
    "*.cfg",
    "Dockerfile",
    "docker-compose.yml",
)

# .git/ is not source. .superpowers/ is scratch SDD working state that
# legitimately quotes the old name while describing this very task.
# docs/ingestion-repair/ is this task's own checked-in design-doc trail
# (plan, spec, ledger) — the permanent record of the bug being fixed, which
# necessarily narrates the old name as history. None of the three is a
# module anything reads the key from.
_EXCLUDED_DIRS = (".git", ".superpowers", "docs/ingestion-repair")

_THIS_FILE = Path(__file__).resolve()


def _is_excluded(path: Path) -> bool:
    rel = path.relative_to(ROOT).as_posix()
    return any(rel == excluded or rel.startswith(excluded + "/") for excluded in _EXCLUDED_DIRS)


def test_no_module_reads_the_legacy_name():
    hits = []
    for pattern in _SCAN_GLOBS:
        for f in ROOT.rglob(pattern):
            if not f.is_file() or f.resolve() == _THIS_FILE or _is_excluded(f):
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if _LEGACY_KEY_NAME in text:
                hits.append(str(f))

    assert hits == [], f"legacy {_LEGACY_KEY_NAME} still read in:\n" + "\n".join(hits)


def test_category_source_reads_the_canonical_name(monkeypatch):
    monkeypatch.delenv(_LEGACY_KEY_NAME, raising=False)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "canonical-value")

    assert SupabaseCategorySource()._key == "canonical-value"


def test_only_the_legacy_name_set_is_a_loud_failure(monkeypatch):
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.setenv(_LEGACY_KEY_NAME, "legacy-value")

    with pytest.raises(RuntimeError, match="SUPABASE_SERVICE_ROLE_KEY"):
        check_service_key_name()


def test_neither_set_is_not_an_error(monkeypatch):
    """A machine with no Supabase config at all is a valid dev setup; only the
    half-configured case is fatal, because that is the one that half works."""
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.delenv(_LEGACY_KEY_NAME, raising=False)

    check_service_key_name()
