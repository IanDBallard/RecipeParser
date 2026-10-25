"""The food check on a picture before it is stored (Cayenne Fix Roadmap F-209).

The model call is a stub returning a typed verdict: these tests hold the policy (what is kept, what
is refused, what happens when the check cannot run), the downscale before the call, and the
first-of-three order a page's candidates are tried in. No real photograph is committed: the images
are synthesised.
"""
from __future__ import annotations

import io
from typing import List, Tuple

import pytest
from PIL import Image

from recipeparser.core.picture_check import (
    FOOD_CHECK_MAX_EDGE_PX,
    FOOD_KINDS,
    MAX_PICTURE_CANDIDATES,
    FoodVerdict,
    check_picture,
    first_food_picture,
    picture_for_check,
)


def _jpeg(width: int = 800, height: int = 600, colour: Tuple[int, int, int] = (200, 120, 40)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(out, "JPEG")
    return out.getvalue()


def _png_rgba(width: int = 300, height: int = 300) -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", (width, height), (10, 200, 10, 128)).save(out, "PNG")
    return out.getvalue()


class _Ask:
    """A stub model: answers each call with the next verdict kind, recording what it was sent."""

    def __init__(self, *kinds: str) -> None:
        self._kinds = list(kinds)
        self.sent: List[Tuple[bytes, str]] = []

    def __call__(self, data: bytes, mime_type: str) -> FoodVerdict:
        self.sent.append((data, mime_type))
        return FoodVerdict(kind=self._kinds.pop(0))  # type: ignore[arg-type]


def _raising(exc: Exception):
    def ask(data: bytes, mime_type: str) -> FoodVerdict:
        raise exc
    return ask


# ── the verdict ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", ["dish", "ingredients", "cooking"])
def test_food_is_kept(kind: str) -> None:
    verdict = check_picture(_jpeg(), "image/jpeg", _Ask(kind))
    assert verdict.keep and verdict.checked and verdict.kind == kind


@pytest.mark.parametrize(
    "kind", ["people", "vehicle", "logo", "advert", "text", "drawing", "blank", "decorative", "other"]
)
def test_anything_that_is_not_food_is_refused(kind: str) -> None:
    """The duck recipe's family in an SUV is `people` or `vehicle`; a site's logo is `logo`."""
    verdict = check_picture(_jpeg(), "image/jpeg", _Ask(kind))
    assert not verdict.keep and verdict.checked and verdict.kind == kind
    assert kind in verdict.reason


def test_the_food_kinds_are_exactly_the_three_kept() -> None:
    assert FOOD_KINDS == frozenset({"dish", "ingredients", "cooking"})


def test_a_verdict_outside_the_typed_kinds_does_not_parse() -> None:
    with pytest.raises(ValueError):
        FoodVerdict(kind="a nice plate of food")  # type: ignore[arg-type]


# ── when the check cannot run, the picture is stored as before ───────────────


def test_a_failed_call_keeps_the_picture_and_says_it_was_not_checked(caplog: pytest.LogCaptureFixture) -> None:
    verdict = check_picture(_jpeg(), "image/jpeg", _raising(RuntimeError("504 DEADLINE_EXCEEDED")))
    assert verdict.keep and not verdict.checked and verdict.kind is None
    assert "504 DEADLINE_EXCEEDED" in caplog.text


def test_a_timeout_keeps_the_picture() -> None:
    verdict = check_picture(_jpeg(), "image/jpeg", _raising(TimeoutError("read timed out")))
    assert verdict.keep and not verdict.checked


def test_bytes_that_do_not_decode_are_kept_unchecked_and_never_sent() -> None:
    ask = _Ask("logo")
    verdict = check_picture(b"\x00\x01 not an image", "image/jpeg", ask)
    assert verdict.keep and not verdict.checked
    assert ask.sent == []


def test_a_cancelled_job_is_not_swallowed_by_the_check() -> None:
    from recipeparser.exceptions import JobCancelledError

    with pytest.raises(JobCancelledError):
        check_picture(_jpeg(), "image/jpeg", _raising(JobCancelledError("cancelled")))


# ── what the model is sent ─────────────────────────────────────────────────────


def test_a_large_picture_is_downscaled_to_a_small_jpeg_before_it_is_sent() -> None:
    ask = _Ask("dish")
    check_picture(_jpeg(4000, 3000), "image/jpeg", ask)
    [(data, mime)] = ask.sent
    assert mime == "image/jpeg"
    with Image.open(io.BytesIO(data)) as sent:
        assert max(sent.size) == FOOD_CHECK_MAX_EDGE_PX
        assert sent.size == (FOOD_CHECK_MAX_EDGE_PX, FOOD_CHECK_MAX_EDGE_PX * 3 // 4)


def test_a_small_picture_is_never_enlarged() -> None:
    data, mime = picture_for_check(_jpeg(200, 150), "image/jpeg")  # type: ignore[misc]
    with Image.open(io.BytesIO(data)) as sent:
        assert sent.size == (200, 150)
    assert mime == "image/jpeg"


def test_transparency_is_laid_on_white_as_the_app_shows_it() -> None:
    data, mime = picture_for_check(_png_rgba(), "image/png")  # type: ignore[misc]
    assert mime == "image/jpeg"
    with Image.open(io.BytesIO(data)) as sent:
        assert sent.mode == "RGB"


def test_the_downscale_answers_none_for_bytes_it_cannot_read() -> None:
    assert picture_for_check(b"garbage", "image/png") is None


# ── a page's candidates, in order, the first that passes ──────────────────────


def test_the_first_candidate_that_is_food_wins_and_later_ones_are_not_asked() -> None:
    ask = _Ask("advert", "dish", "dish")
    candidates = [("ad", _jpeg()), ("hero", _jpeg()), ("thumb", _jpeg())]
    picked = first_food_picture(candidates, lambda c: (c[1], "image/jpeg"), ask)
    assert picked is not None and picked[0][0] == "hero"
    assert len(ask.sent) == 2


def test_no_candidate_that_is_food_is_no_picture() -> None:
    ask = _Ask("logo", "people")
    picked = first_food_picture([("a", _jpeg()), ("b", _jpeg())], lambda c: (c[1], "image/jpeg"), ask)
    assert picked is None


def test_at_most_three_candidates_are_asked() -> None:
    ask = _Ask("logo", "logo", "logo", "dish")
    candidates = [(str(n), _jpeg()) for n in range(5)]
    assert MAX_PICTURE_CANDIDATES == 3
    assert first_food_picture(candidates, lambda c: (c[1], "image/jpeg"), ask) is None
    assert len(ask.sent) == 3


def test_a_candidate_that_will_not_load_is_passed_over_without_a_call() -> None:
    ask = _Ask("dish")
    candidates = [("gone", None), ("hero", _jpeg())]
    picked = first_food_picture(
        candidates, lambda c: (c[1], "image/jpeg") if c[1] is not None else None, ask
    )
    assert picked is not None and picked[0][0] == "hero"
    assert len(ask.sent) == 1


def test_a_check_that_fails_keeps_that_candidate_as_before() -> None:
    """On a failed check the first loadable candidate is stored, which is what shipped before."""
    picked = first_food_picture(
        [("hero", _jpeg()), ("other", _jpeg())], lambda c: (c[1], "image/jpeg"), _raising(RuntimeError("x"))
    )
    assert picked is not None and picked[0][0] == "hero"


def test_the_pick_carries_the_loaded_bytes_and_type() -> None:
    hero = _jpeg()
    picked = first_food_picture([("hero", hero)], lambda c: (c[1], "image/png"), _Ask("dish"))
    assert picked == (("hero", hero), hero, "image/png")
