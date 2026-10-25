"""Whether a picture an import would store is a photograph of food (Cayenne Fix Roadmap F-209).

The pixel checks in ``io/readers/photo_check.py`` refuse a blank crop or an ink ornament, but no
colour rule tells a site's logo, an advert or a family in a car from a plated dish: a beta tester's
"Roast Duck Breasts with Pomegranate-Chile Sauce" (2026-10-25, a web page saved as a PDF) was
stored with the page's stock photo of a family in an SUV. So every picture an import would store
is shown to the model once, small, and asked one question with a typed answer.

The policy lives here and the model call is injected (``AskFood``), so the rules are tested with
stubbed verdicts and the same function serves any later pass over pictures already stored (F-210):

- food (a dish, its ingredients, food being cooked) is kept; anything else is refused, and the
  recipe goes without a picture (the app shows its placeholder);
- a check that cannot run (the call failed or timed out, or the bytes do not decode) keeps the
  picture, as before this check existed, and says so in the log. It never fails the import;
- a page with several candidates is tried in order, at most ``MAX_PICTURE_CANDIDATES``, and the
  first that passes is the one stored.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import Callable, Iterable, Literal, Optional, Tuple, TypeVar

from PIL import Image, ImageOps
from pydantic import BaseModel

from recipeparser.exceptions import JobCancelledError

log = logging.getLogger(__name__)

#: What the picture shows, as the model answers it. The first three are food.
FoodKind = Literal[
    "dish",         # a prepared or plated dish, a bake, a drink
    "ingredients",  # raw ingredients or produce
    "cooking",      # food being prepared or cooked
    "people",
    "vehicle",
    "logo",
    "advert",
    "text",         # a page, a menu, a graphic of words
    "drawing",      # an illustration or drawing, even of food
    "blank",        # an empty or nearly uniform crop
    "decorative",   # a border, a pattern, an ornament
    "other",        # any other scene or object
]

FOOD_KINDS = frozenset({"dish", "ingredients", "cooking"})

#: The long edge the picture is reduced to before it is sent: plenty to tell a dish from a car,
#: and a few tens of kilobytes on the wire. The call also asks for the model's low media
#: resolution, so the image costs the fewest input tokens the model bills for one.
FOOD_CHECK_MAX_EDGE_PX = 512
FOOD_CHECK_JPEG_QUALITY = 80

#: A page offering several pictures (og:image, the recipe's JSON-LD image, the page's own images;
#: a book page's other photos) has at most this many asked about, one call each.
MAX_PICTURE_CANDIDATES = 3


class FoodVerdict(BaseModel):
    """The model's answer: one kind, nothing else, so the reply is a few tokens."""

    kind: FoodKind


#: The model call: (downscaled JPEG bytes, mime type) -> verdict. Raises on any failure.
AskFood = Callable[[bytes, str], FoodVerdict]


@dataclass(frozen=True)
class PictureVerdict:
    """What to do with one picture, and why."""

    keep: bool
    #: False when the check could not run and the picture was kept by default.
    checked: bool
    kind: Optional[str]
    reason: str


def picture_for_check(data: bytes, content_type: str) -> Optional[Tuple[bytes, str]]:
    """The picture as the check sends it: upright, flattened onto white, at most
    FOOD_CHECK_MAX_EDGE_PX on its long edge (never enlarged), a JPEG. None when it will not decode."""
    try:
        with Image.open(io.BytesIO(data)) as source:
            source.draft("RGB", (FOOD_CHECK_MAX_EDGE_PX, FOOD_CHECK_MAX_EDGE_PX))
            upright = ImageOps.exif_transpose(source)
            rgba = upright.convert("RGBA")
    except Exception as exc:  # noqa: BLE001 -- any decoder failure means "cannot judge"
        log.info("food check: could not decode a %s picture (%s).", content_type, exc)
        return None
    flat = Image.new("RGB", rgba.size, (255, 255, 255))
    flat.paste(rgba, mask=rgba.getchannel("A"))
    flat.thumbnail((FOOD_CHECK_MAX_EDGE_PX, FOOD_CHECK_MAX_EDGE_PX), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    flat.save(out, "JPEG", quality=FOOD_CHECK_JPEG_QUALITY)
    return out.getvalue(), "image/jpeg"


def check_picture(data: bytes, content_type: str, ask: AskFood) -> PictureVerdict:
    """Keep or refuse one picture. Never raises, except for a cancelled job."""
    small = picture_for_check(data, content_type)
    if small is None:
        return PictureVerdict(True, False, None, "not checked: the picture does not decode")
    try:
        verdict = ask(*small)
    except JobCancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 -- a failed check never costs the picture or the import
        log.warning("food check: could not check a picture (%s: %s); storing it unchecked.",
                    type(exc).__name__, exc)
        return PictureVerdict(True, False, None, f"not checked: {type(exc).__name__}")
    if verdict.kind in FOOD_KINDS:
        return PictureVerdict(True, True, verdict.kind, f"food ({verdict.kind})")
    return PictureVerdict(False, True, verdict.kind, f"not food ({verdict.kind})")


C = TypeVar("C")


def first_food_picture(
    candidates: Iterable[C],
    load: Callable[[C], Optional[Tuple[bytes, str]]],
    ask: AskFood,
    *,
    limit: int = MAX_PICTURE_CANDIDATES,
) -> Optional[Tuple[C, bytes, str]]:
    """The first of ``candidates`` that is food, with its bytes and type, or None.

    ``load`` turns a candidate into (bytes, content type), or None when it cannot be had (a failed
    download); such a candidate is passed over and costs no call. At most ``limit`` candidates are
    asked about. A candidate whose check cannot run is taken, as the first candidate always was.
    """
    asked = 0
    for candidate in candidates:
        if asked >= limit:
            break
        loaded = load(candidate)
        if loaded is None:
            continue
        asked += 1
        verdict = check_picture(loaded[0], loaded[1], ask)
        if verdict.keep:
            return candidate, loaded[0], loaded[1]
        log.info("food check: refused a picture candidate: %s.", verdict.reason)
    return None
