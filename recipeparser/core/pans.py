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

from recipeparser.core.fat_tokens import TEMP_PREFIX, TOKEN_RE, is_pan_id, is_temp_id, parse_temp_value
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
)
PAN_SHAPES = ("round", "square", "rect", "loaf", "ring", "flat-round", "sheet")
PAN_FAMILIES = ("round-cake", "square", "traybake", "sheet", "loaf", "ring", "tart", "pizza")
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
        if not is_temp_id(token_id) or ",core" not in token_id:
            return m.group(0)
        value = token_id[len(TEMP_PREFIX) :]
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
