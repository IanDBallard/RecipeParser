"""
recipeparser/core/pans.py — the tins a recipe names, its bake type and baking step, and the
internal temperatures it states (Cayenne pan-aware scaling: data model, parser contract, D9, D10,
D21, D22, D24). Checks what REFINE and the pans backfill say; a model slip costs the field, never
the recipe. The vocabularies are Cayenne's domain/pans.ts and domain/bakeTypes.ts; keep them alike.

Pure: no I/O, no imports from recipeparser.io or recipeparser.adapters.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional, Sequence, Set, Tuple

from recipeparser.core.fat_tokens import (
    TEMP_PREFIX,
    TOKEN_RE,
    fold,
    is_pan_id,
    is_temp_id,
    locate,
    pan_token,
    parse_temp_value,
)
from recipeparser.core.temperatures import overlaps, token_spans
from recipeparser.models import CayenneRefinement, RecipePan, TokenizedDirection

BAKE_TYPES = (
    "sponge",
    "butter-cake",
    "quick-bread",
    "bar",
    "bread-lean",
    "bread-enriched",
    "cheesecake",
    "custard",
    "pizza",
    "pie-fruit",
    "pie-custard",
    "pie-nut",
    "pie-savoury",
)
PAN_SHAPES = ("round", "square", "rect", "loaf", "ring", "flat-round", "sheet")
PAN_FAMILIES = ("round-cake", "square", "traybake", "sheet", "loaf", "ring", "tart", "pizza", "stovetop")
# R2: tins a source names without dimensions; Cayenne domain/pans.ts keys.
HINT_KEYS = (
    "uk-loaf-1lb",
    "uk-loaf-2lb",
    "us-bundt-6",
    "us-bundt-10",
    "us-bundt-12",
    "us-tube-10",
    "us-jellyroll",
    "us-quarter-sheet",
    "us-half-sheet",
    "us-rect-13x9",
    "uk-traybake-30x23",
    "uk-swissroll-33x23",
    "eu-blech-40x30",
    "eu-gugelhupf-22",
    "au-lamington-30x20",
    "au-slab-30x20",
    "au-bar-25x8",
)
_ID = re.compile(r"pan_\d{2}")
_HEADING = re.compile(r"\*\*[^*]+\*\*:?")
_MM_MIN, _MM_MAX = 10.0, 1000.0
_ML_MAX = 10000.0
_CORE_MIN_C, _CORE_MAX_C = 30.0, 110.0


def _mm(value: Optional[float]) -> Optional[float]:
    return value if value is not None and _MM_MIN <= value <= _MM_MAX else None


def normalise_pans(pans: Iterable[RecipePan]) -> Tuple[List[RecipePan], List[str]]:
    kept: List[RecipePan] = []
    warnings: List[str] = []
    seen: Set[str] = set()
    for p in pans:
        if not _ID.fullmatch(p.id) or p.id in seen:
            warnings.append(f"tin {p.id!r} dropped: bad or repeated id")
            continue
        dims = p.dims_mm.model_copy(update={k: _mm(getattr(p.dims_mm, k)) for k in ("d", "w", "l", "depth")})
        capacity = p.capacity_ml if p.capacity_ml is not None and 0 < p.capacity_ml <= _ML_MAX else None
        hint = p.catalog_hint if p.catalog_hint in HINT_KEYS else None
        if dims.d is None and not (dims.l and dims.w) and capacity is None and hint is None:
            warnings.append(f"tin {p.id!r} ({p.raw!r}) dropped: nothing gives its size")
            continue
        seen.add(p.id)
        kept.append(
            p.model_copy(
                update={
                    "count": p.count if p.count >= 1 else 1,
                    "shape": p.shape if p.shape in PAN_SHAPES else None,
                    "family": p.family if p.family in PAN_FAMILIES else None,
                    "dims_mm": dims,
                    "capacity_ml": capacity,
                    "catalog_hint": hint,
                    "edited": False,
                }
            )
        )
    return kept, warnings


def _plain(text: str) -> str:
    return " ".join(text.split())


def check_pan_tokens(
    directions: Sequence[TokenizedDirection], ids: Set[str], source: str
) -> Tuple[List[TokenizedDirection], List[str]]:
    """A pan token whose id is not a kept tin, or whose words the source does not write, becomes its words."""
    plain = _plain(source)
    warnings: List[str] = []

    def check(m: "re.Match[str]") -> str:
        if not is_pan_id(m.group(1)) or (m.group(1) in ids and m.group(3) is None and _plain(m.group(2)) in plain):
            return m.group(0)
        warnings.append(f"pan token unwrapped: {m.group(0)}")
        return m.group(2)

    out = [d.model_copy(update={"text": TOKEN_RE.sub(check, d.text)}) for d in directions]
    return out, warnings


def check_bake(
    bake_type: Optional[str],
    bake_step: Optional[int],
    directions: Sequence[TokenizedDirection],
    heading_steps: Set[int],
) -> Tuple[Optional[str], Optional[int]]:
    # R-E: a step with no known type is meaningless, so both go.
    if bake_type not in BAKE_TYPES:
        return None, None
    kind = bake_type
    step = (
        bake_step
        if bake_step is not None and 0 <= bake_step < len(directions) and bake_step not in heading_steps
        else None
    )
    return kind, step


def _core_in_band(main: Tuple[str, float]) -> bool:
    scale, n = main
    c = n if scale == "C" else (n - 32) * 5 / 9
    return _CORE_MIN_C <= c <= _CORE_MAX_C


def _without_core(value: str) -> str:
    return ",".join(part for part in value.split(",") if part.strip() != "core")


def check_core_flags(directions: Sequence[TokenizedDirection]) -> Tuple[List[TokenizedDirection], List[str]]:
    """
    R5, R-B: ``,core`` stays only on a degree reading between 30 and 110 °C; otherwise it is removed
    and the token kept. A value that does not parse but would parse without ``,core`` (``180C,oven,core``,
    ``gas4,core``) loses the flag. A value that fails either way is left untouched for
    _check_temperatures. Pre-pass: refine calls this before _check_temperatures.
    """
    warnings: List[str] = []

    def check(m: "re.Match[str]") -> str:
        token_id = m.group(1)
        if not is_temp_id(token_id):
            return m.group(0)
        value = token_id[len(TEMP_PREFIX) :]
        if "core" not in [p.strip() for p in value.split(",")]:
            return m.group(0)
        parsed = parse_temp_value(value)
        if parsed is not None and parsed.core and parsed.main is not None and _core_in_band(parsed.main):
            return m.group(0)
        if parsed is None and parse_temp_value(_without_core(value)) is None:
            return m.group(0)
        warnings.append(f"core flag removed: {m.group(0)}")
        return m.group(0).replace(token_id, TEMP_PREFIX + _without_core(value), 1)

    return [d.model_copy(update={"text": TOKEN_RE.sub(check, d.text)}) for d in directions], warnings


def heading_steps(directions: Sequence[TokenizedDirection]) -> Set[int]:
    """0-based indexes of steps that are a phase heading: one bold span, optional colon (REFINE rule 3)."""
    return {i for i, d in enumerate(directions) if _HEADING.fullmatch(d.text.strip())}


def apply_pan_checks(refinement: CayenneRefinement, raw_directions: Sequence[str]) -> List[str]:
    """
    Every check above, in place on ``refinement``. Returns the warnings. Order matters: check_core_flags
    is a pre-pass that refine must also run before _check_temperatures (Task 4 wires that).
    """
    pans, warnings = normalise_pans(refinement.pans)
    directions, w = check_pan_tokens(refinement.tokenized_directions, {p.id for p in pans}, "\n".join(raw_directions))
    warnings += w
    directions, w = check_core_flags(directions)
    warnings += w
    if refinement.bake_type is not None and refinement.bake_type not in BAKE_TYPES:
        warnings.append(f"bake type {refinement.bake_type!r} dropped")
    refinement.pans = pans
    refinement.tokenized_directions = directions
    refinement.bake_type, refinement.bake_step = check_bake(
        refinement.bake_type, refinement.bake_step, directions, heading_steps(directions)
    )
    return warnings


def _on_word_boundary(text: str, start: int, end: int) -> bool:
    """Whether ``text[start:end]`` is not cut out of a longer word: "tin" in "tinning" is not."""
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    first, last = text[start], text[end - 1]
    return not (first.isalnum() and before.isalnum()) and not (last.isalnum() and after.isalnum())


def place_pan_mentions(text: str, mentions: Sequence[Tuple[str, str, str]]) -> Tuple[str, List[str]]:
    """Splice the model's ``(quote, context, pan_id)`` mentions into ``text`` as tin tokens where each
    quote is found verbatim, outside every token and earlier placement. A bad id or quote, a quote
    not found or found twice without a context to tell them apart, is dropped and reported. Only the
    braces and id are added: no word of ``text`` changes."""
    dropped: List[str] = []
    placed: List[Tuple[int, int, str]] = []
    for quote, context, pan_id in mentions:
        if not _ID.fullmatch(pan_id) or not quote or any(c in quote for c in "{}|"):
            dropped.append(f"{quote!r}: unusable id {pan_id!r} or quote")
            continue
        spans = token_spans(text) + [(s, e) for s, e, _ in placed]
        starts = [
            s for s in locate(text, quote, context)
            if not overlaps(s, s + len(quote), spans) and _on_word_boundary(text, s, s + len(quote))
        ]
        if len(starts) != 1:
            dropped.append(f"{quote!r} ({context!r}): {len(starts)} free placement(s)")
            continue
        placed.append((starts[0], starts[0] + len(quote), pan_id))
    pieces: List[str] = []
    cursor = 0
    for start, end, pan_id in sorted(placed):
        pieces.append(text[cursor:start])
        pieces.append(pan_token(pan_id, text[start:end]))
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces), dropped


def mark_core(text: str, quotes: Sequence[str]) -> Tuple[str, List[str]]:
    """Add ``,core`` to the plain degree token whose words contain a quote (internal temperature,
    D24). Only the token's value changes. An oven, fan, gas, tin or ingredient token, a value outside
    30 to 110 C, or a quote matching no token is left alone and reported; a token already ``,core``
    is left alone silently; a quote that fits several eligible tokens marks none."""
    notes: List[str] = []
    wanted = {fold(q): q for q in quotes if q}
    matches = [m for m in TOKEN_RE.finditer(text) if is_temp_id(m.group(1))]
    mark: Set[int] = set()
    hit: Set[str] = set()
    for q, original in wanted.items():
        eligible: List["re.Match[str]"] = []
        for m in matches:
            if q not in fold(m.group(2)):
                continue
            hit.add(q)
            value = m.group(1)[len(TEMP_PREFIX) :]
            parsed = parse_temp_value(value)
            flagged = parse_temp_value(value + ",core")
            if parsed is not None and parsed.core:
                continue
            if (
                parsed is None or parsed.oven or parsed.fan is not None
                or flagged is None or not flagged.core or flagged.main is None
                or flagged.main[0] == "gas" or not _core_in_band(flagged.main)
            ):
                notes.append(f"core not marked: {m.group(0)}")
            else:
                eligible.append(m)
        if len(eligible) > 1:
            notes.append(f"core quote {original!r} is ambiguous: {len(eligible)} temperature tokens fit")
        else:
            mark.update(m.start() for m in eligible)
    notes.extend(f"core quote {wanted[q]!r} matches no temperature token" for q in wanted if q not in hit)

    def apply(m: "re.Match[str]") -> str:
        return m.group(0).replace(m.group(1), m.group(1) + ",core", 1) if m.start() in mark else m.group(0)

    return TOKEN_RE.sub(apply, text), notes
