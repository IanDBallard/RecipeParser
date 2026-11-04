"""
recipeparser/core/yields.py - the yield kinds a recipe has and the container of a pie or pastry
(Cayenne yield kinds 11.3). Checks what REFINE and the yields backfill say; a model slip costs the
field, never the recipe. The vocabularies are Cayenne's domain/yieldKinds.ts; keep them alike
(tests/fixtures/yield_kinds.json).

Pure: no I/O, no imports from recipeparser.io or recipeparser.adapters.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from recipeparser.models import RecipePan, StructuredIngredient, YieldAnswer, YieldContainer

YIELD_KINDS = ("servings", "pieces", "component")
PASTRY_TYPES = ("shortcrust", "sweet-shortcrust", "puff", "hot-water", "suet", "pizza-dough", "crumb", "filo")
SIDES = ("full", "short", "none")
TOPS = ("none", "full", "lattice", "crumble")
THICKNESSES = ("thin", "standard", "thick")

DEFAULT_PASTRY = "shortcrust"
DEFAULT_SIDES = "full"
DEFAULT_TOP = "none"
DEFAULT_THICKNESS = "standard"

_AREA_DIAMETER_MM = (50, 600)
_PIECE_WEIGHT_G = (10, 5000)


def _in_range(v: Optional[float], bounds: Tuple[float, float]) -> bool:
    return v is not None and bounds[0] <= v <= bounds[1]


def _whole(v: object) -> Optional[int]:
    """A whole number from an int or a float with no fraction; anything else (bool, str, None) is None."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v in (float("inf"), float("-inf")):
        return None
    return int(v) if float(v).is_integer() else None


def _word(value: Optional[str], vocab: Sequence[str], default: str, field: str, warnings: List[str]) -> str:
    if value in vocab:
        return value  # type: ignore[return-value]
    warnings.append(f"yield container {field} {value!r} is not in the vocabulary; using {default!r}")
    return default


def _ids(
    positions: Sequence[object],
    ingredients: Sequence[StructuredIngredient],
    label: str,
    warnings: List[str],
) -> List[str]:
    """1-based positions in the list the prompt shows (headings included) -> ingredient ids."""
    out: List[str] = []
    for raw in positions:
        pos = _whole(raw)
        if pos is None or not 1 <= pos <= len(ingredients):
            pos = raw
            warnings.append(f"yield {label} position {pos} is out of range; dropped")
        elif ingredients[pos - 1].kind == "heading":
            warnings.append(f"yield {label} position {pos} names a heading; dropped")
        elif ingredients[pos - 1].id not in out:
            out.append(ingredients[pos - 1].id)
    return out


def _container(
    c: YieldContainer, ingredients: Sequence[StructuredIngredient], warnings: List[str]
) -> Dict[str, object]:
    component = _ids(c.component_lines, ingredients, "component_lines", warnings)
    topping = [i for i in _ids(c.topping_lines, ingredients, "topping_lines", warnings) if i not in component]
    return {
        "pastry": _word(c.pastry, PASTRY_TYPES, DEFAULT_PASTRY, "pastry", warnings),
        "base": c.base if isinstance(c.base, bool) else True,
        "sides": _word(c.sides, SIDES, DEFAULT_SIDES, "sides", warnings),
        "top": _word(c.top, TOPS, DEFAULT_TOP, "top", warnings),
        "thickness": _word(c.thickness, THICKNESSES, DEFAULT_THICKNESS, "thickness", warnings),
        "component_lines": component,
        "topping_lines": topping,
    }


def check_yield(
    answer: Optional[YieldAnswer],
    ingredients: Sequence[StructuredIngredient],
    pans: Sequence[RecipePan],
) -> Tuple[Optional[dict], List[str]]:
    """The yield_spec to store, or None, and the warnings. Pure. It never raises and never costs the recipe.

    - an unknown kind -> None;
    - a container is kept on any kind (an assembly is a servings or pieces recipe with one), unless it
      has no piece at all (no base and no top), which drops it with a warning; the check never
      guesses from the tins;
    - a field outside its vocabulary, or a null pastry, takes the default, with a warning;
    - `area` is kept only on a component, with a whole count of 1 or more and diameter_mm within 50..600;
    - `pieces` is kept only on a pieces kind, with a whole count of 1 or more; weight_g within 10..5000 or
      None; a weight is never asked of a component;
    - component_lines / topping_lines are 1-based positions into `ingredients` (headings included)
      and are mapped to ids; one out of range or naming a heading is dropped with a warning; a line
      in both lists is component;
    - `edited` is always False from the parser.
    """
    if answer is None:
        return None, []
    if answer.kind not in YIELD_KINDS:
        return None, [f"yield kind {answer.kind!r} is not in the vocabulary; no yield_spec"]
    warnings: List[str] = []
    kind = answer.kind

    container: Optional[Dict[str, object]] = None
    if answer.container is not None:
        container = _container(answer.container, ingredients, warnings)
        if container["base"] is False and container["top"] == "none":
            warnings.append("yield container has no base and no top; dropped")
            container = None

    area: Optional[Dict[str, object]] = None
    if answer.area is not None:
        a = answer.area
        count, diameter = _whole(a.count), a.diameter_mm
        if kind != "component":
            warnings.append(f"yield area on a {kind} recipe; dropped")
        elif count is None or count < 1 or not _in_range(diameter, _AREA_DIAMETER_MM):
            warnings.append("yield area needs a whole count of 1 or more and a diameter of 50..600 mm; dropped")
        else:
            area = {"count": count, "noun": a.noun or "", "diameter_mm": diameter}

    pieces: Optional[Dict[str, object]] = None
    if answer.pieces is not None:
        p = answer.pieces
        count = _whole(p.count)
        if kind != "pieces":
            warnings.append(f"yield pieces on a {kind} recipe; dropped")
        elif count is None or count < 1:
            warnings.append("yield pieces needs a whole count of 1 or more; dropped")
        else:
            weight = p.weight_g if _in_range(p.weight_g, _PIECE_WEIGHT_G) else None
            if p.weight_g is not None and weight is None:
                warnings.append(f"yield piece weight {p.weight_g} g is outside 10..5000; dropped")
            diameter = p.diameter_mm if p.diameter_mm is not None and p.diameter_mm > 0 else None
            pieces = {"count": count, "noun": p.noun or "", "weight_g": weight, "diameter_mm": diameter}

    return {"kind": kind, "container": container, "area": area, "pieces": pieces, "edited": False}, warnings
