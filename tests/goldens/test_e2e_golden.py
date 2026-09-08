"""End-to-end goldens (spec §6.4).

Reader → RecipePipeline at the production pool size → both zip writers, with
recorded Gemini replies.  Comparison is a multiset sorted by title, so a
regression that made output depend on completion order fails here.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import zipfile
from pathlib import Path
from typing import Dict, List

import pytest

from recipeparser import gemini
from recipeparser.core.fsm import PipelineController
from recipeparser.core.pipeline import MAX_CONCURRENT_API_CALLS, RecipePipeline
from recipeparser.core.ports import CategorySource
from recipeparser.core.rate_limiter import GlobalRateLimiter
from recipeparser.io.readers.epub import EpubReader
from recipeparser.io.readers.paprika import PaprikaReader
from recipeparser.io.writers.cayenne_zip import CayenneZipWriter
from recipeparser.io.writers.paprika_zip import PaprikaWriter
from tests.goldens.conftest import FIXED_AXES
from tests.goldens.paths import E2E_DIR, corpus_path

E2E_FIXTURES = ("dual-units.epub", "phases-bakers.epub", "legacy-photo.paprikarecipes")

PLACEHOLDER = "<fixed>"


class _FixedAxesSource(CategorySource):
    """The taxonomy the recordings were made against."""

    def load_axes(self, user_id: str = "") -> Dict[str, List[str]]:
        return FIXED_AXES

    def load_category_ids(self, user_id: str = "") -> Dict[str, str]:
        return {}


@pytest.fixture(autouse=True)
def _no_retry_sleeps(monkeypatch):
    monkeypatch.setattr(gemini.time, "sleep", lambda *_: None)


def _read(fixture: str):
    if fixture.endswith(".epub"):
        return EpubReader().read(str(corpus_path(fixture)))
    return PaprikaReader().read(str(corpus_path(fixture)))


def _normalise(value):
    """Replace everything that legitimately differs per run.

    uid, hash and created are generated at write time; photo_data and the
    embedding are large and are compared by digest.
    """
    if isinstance(value, dict):
        out = {}
        for key, inner in value.items():
            if key in ("uid", "hash", "created"):
                out[key] = PLACEHOLDER
            elif key == "photo_data" and inner:
                out[key] = "sha256:" + hashlib.sha256(str(inner).encode("utf-8")).hexdigest()
            elif key == "embedding" and inner:
                digest = hashlib.sha256(json.dumps(inner).encode("utf-8")).hexdigest()
                out[key] = {"len": len(inner), "sha256": digest}
            else:
                out[key] = _normalise(inner)
        return out
    if isinstance(value, list):
        return [_normalise(v) for v in value]
    return value


def _zip_manifest(path: Path) -> dict:
    """Entry names plus the parsed JSON inside each entry, sorted by name."""
    entries = {}
    with zipfile.ZipFile(path) as zf:
        for name in sorted(zf.namelist()):
            raw = zf.read(name)
            try:
                payload = json.loads(gzip.decompress(raw))
            except gzip.BadGzipFile:
                payload = json.loads(raw)
            entries[name] = _normalise(payload)
    return {"names": sorted(entries), "entries": entries}


def _assert_golden(fixture: str, name: str, actual: dict, update: bool) -> None:
    path = E2E_DIR / fixture / f"{name}.json"
    rendered = json.dumps(actual, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if update:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
        return
    assert path.exists(), (
        f"No e2e golden at {path}. Create it with:\n"
        "    pytest tests/goldens/test_e2e_golden.py --update-goldens"
    )
    assert json.loads(rendered) == json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("fixture", E2E_FIXTURES)
# Narrow, message-matched ignores for ebooklib's own noise only (same marks
# test_stages_golden.py uses) -- not ``ignore::UserWarning`` wholesale, which
# would also swallow the ``prompt_sha256 mismatch`` UserWarning golden_client.py
# raises on purpose.
@pytest.mark.filterwarnings(
    "ignore:In the future version we will turn default option ignore_ncx:UserWarning"
)
@pytest.mark.filterwarnings(
    "ignore:This search incorrectly ignores the root element, "
    "and will be fixed in a future version.:FutureWarning"
)
# The pipeline maps uom_system="US" to units="us" (recipeparser.core.pipeline.
# _uom_to_units_key), while Task 8 recorded extract-stage replies under
# units="book" (tests/goldens/test_stages_golden.py).  That text lives before
# the body marker in build_extract_prompt, so the body key -- and therefore
# replay -- is unaffected, but the *full* prompt hash golden_client.py checks
# differs by design.  This ignore is scoped to extract-stage mismatches only:
# refine calls here use uom_system="US" too (matching what was recorded), and
# extract-stage drift for any *other* reason is still caught by
# test_stages_golden.py, which exercises the same recordings under the
# matching units="book".
@pytest.mark.filterwarnings(
    r"ignore:prompt_sha256 mismatch for .*extract-\d+\.json:UserWarning"
)
# The refine prompt gained a phase-preservation rule (see
# gemini.build_refine_prompt). Only phases-bakers.epub was re-recorded against
# it — that is the fixture the rule exists for, and re-recording the other 517
# refine replies would have cost 517 paid API calls to obtain replies whose
# content the rule does not change. Those recordings therefore no longer match
# their prompt's sha256. Replay is unaffected: recordings key off the prompt
# BODY (the raw recipe), which the rule does not touch. Scoped to refine-NN by
# filename so extract/table/vision drift still warns.
@pytest.mark.filterwarnings(
    r"ignore:prompt_sha256 mismatch for .*refine-\d+\.json:UserWarning"
)
def test_e2e_golden(fixture, golden_client, tmp_path, update_goldens):
    GlobalRateLimiter().reset()
    progress: List[tuple] = []
    pipeline = RecipePipeline(
        client=golden_client(fixture),
        controller=PipelineController(),
        category_source=_FixedAxesSource(),
        uom_system="US",
        measure_preference="Volume",
        concurrency=MAX_CONCURRENT_API_CALLS,
        rpm=9999,
    )
    results = pipeline.run(
        _read(fixture),
        on_progress=lambda stage, done, total: progress.append((stage, done, total)),
    )
    assert results, f"{fixture} produced no recipes"
    assert progress, "on_progress never fired"
    assert progress[-1][1] == progress[-1][2], "progress did not reach completion"

    ordered = sorted(results, key=lambda r: r.title)
    _assert_golden(
        fixture,
        "ingest",
        {"recipes": [_normalise(r.model_dump()) for r in ordered]},
        update_goldens,
    )

    paprika_path = tmp_path / "out.paprikarecipes"
    PaprikaWriter(paprika_path).write(ordered)
    _assert_golden(fixture, "paprika", _zip_manifest(paprika_path), update_goldens)

    cayenne_path = tmp_path / "out.cayenne.paprikarecipes"
    CayenneZipWriter(cayenne_path).write(ordered)
    _assert_golden(fixture, "cayenne", _zip_manifest(cayenne_path), update_goldens)


@pytest.mark.parametrize("fixture", E2E_FIXTURES)
@pytest.mark.filterwarnings(
    "ignore:In the future version we will turn default option ignore_ncx:UserWarning"
)
@pytest.mark.filterwarnings(
    "ignore:This search incorrectly ignores the root element, "
    "and will be fixed in a future version.:FutureWarning"
)
# See test_e2e_golden's comment: default uom_system="US" -> units="us" here
# too, so the same known, body-key-safe extract-stage mismatch applies.
@pytest.mark.filterwarnings(
    r"ignore:prompt_sha256 mismatch for .*extract-\d+\.json:UserWarning"
)
# The refine prompt gained a phase-preservation rule (see
# gemini.build_refine_prompt). Only phases-bakers.epub was re-recorded against
# it — that is the fixture the rule exists for, and re-recording the other 517
# refine replies would have cost 517 paid API calls to obtain replies whose
# content the rule does not change. Those recordings therefore no longer match
# their prompt's sha256. Replay is unaffected: recordings key off the prompt
# BODY (the raw recipe), which the rule does not touch. Scoped to refine-NN by
# filename so extract/table/vision drift still warns.
@pytest.mark.filterwarnings(
    r"ignore:prompt_sha256 mismatch for .*refine-\d+\.json:UserWarning"
)
def test_the_result_is_the_same_at_pool_size_one(fixture, golden_client, update_goldens):
    """Order independence, asserted rather than assumed."""
    if update_goldens:
        pytest.skip("nothing to compare while regenerating")

    def _run(concurrency: int):
        GlobalRateLimiter().reset()
        pipeline = RecipePipeline(
            client=golden_client(fixture),
            controller=PipelineController(),
            category_source=_FixedAxesSource(),
            concurrency=concurrency,
            rpm=9999,
        )
        results = pipeline.run(_read(fixture))
        return [_normalise(r.model_dump()) for r in sorted(results, key=lambda r: r.title)]

    assert _run(1) == _run(MAX_CONCURRENT_API_CALLS)


@pytest.mark.filterwarnings(
    "ignore:In the future version we will turn default option ignore_ncx:UserWarning"
)
@pytest.mark.filterwarnings(
    "ignore:This search incorrectly ignores the root element, "
    "and will be fixed in a future version.:FutureWarning"
)
# See test_e2e_golden's comment: default uom_system="US" -> units="us" here
# too, so the same known, body-key-safe extract-stage mismatch applies.
@pytest.mark.filterwarnings(
    r"ignore:prompt_sha256 mismatch for .*extract-\d+\.json:UserWarning"
)
# The refine prompt gained a phase-preservation rule (see
# gemini.build_refine_prompt). Only phases-bakers.epub was re-recorded against
# it — that is the fixture the rule exists for, and re-recording the other 517
# refine replies would have cost 517 paid API calls to obtain replies whose
# content the rule does not change. Those recordings therefore no longer match
# their prompt's sha256. Replay is unaffected: recordings key off the prompt
# BODY (the raw recipe), which the rule does not touch. Scoped to refine-NN by
# filename so extract/table/vision drift still warns.
@pytest.mark.filterwarnings(
    r"ignore:prompt_sha256 mismatch for .*refine-\d+\.json:UserWarning"
)
def test_the_cayenne_archive_round_trips_back_through_the_reader(golden_client, tmp_path):
    """Flow B: what CayenneZipWriter writes, PaprikaReader must restore for free."""
    GlobalRateLimiter().reset()
    pipeline = RecipePipeline(
        client=golden_client("dual-units.epub"),
        controller=PipelineController(),
        category_source=_FixedAxesSource(),
        concurrency=MAX_CONCURRENT_API_CALLS,
        rpm=9999,
    )
    results = pipeline.run(_read("dual-units.epub"))
    out = tmp_path / "roundtrip.paprikarecipes"
    CayenneZipWriter(out).write(results)

    restored = PaprikaReader().read(str(out))
    assert len(restored) == len(results)
    assert all(c.pre_parsed is not None for c in restored)
    assert sorted(c.pre_parsed.title for c in restored) == sorted(r.title for r in results)
