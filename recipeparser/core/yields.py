"""
recipeparser/core/yields.py - the yield kinds a recipe has and the container of a pie or pastry
(Cayenne yield kinds 11.3). Checks what REFINE and the yields backfill say; a model slip costs the
field, never the recipe. The vocabularies are Cayenne's domain/yieldKinds.ts; keep them alike
(tests/fixtures/yield_kinds.json).

Pure: no I/O, no imports from recipeparser.io or recipeparser.adapters.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from recipeparser.models import StructuredIngredient, YieldAnswer, YieldContainer

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
_PIECE_DIAMETER_MM = (10, 1000)
# A whole number beyond this is not one a float holds exactly, and no count or position is near it.
_WHOLE_LIMIT = 2**53


def _in_range(v: Optional[float], bounds: Tuple[float, float]) -> bool:
    return v is not None and bounds[0] <= v <= bounds[1]


def _whole(v: object) -> Optional[int]:
    """A whole number from an int or a float with no fraction; anything else (bool, str, None, NaN, an
    infinity, or a number too big for a float to hold exactly) is None. It never raises."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        f = float(v)
    except OverflowError:
        return None
    if not math.isfinite(f) or abs(f) > _WHOLE_LIMIT or not f.is_integer():
        return None
    return int(f)


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
    pastry = _word(c.pastry, PASTRY_TYPES, DEFAULT_PASTRY, "pastry", warnings)
    base = c.base if isinstance(c.base, bool) else True
    top = _word(c.top, TOPS, DEFAULT_TOP, "top", warnings)
    if pastry == "crumb" and not base and top != "crumble":
        # D96: crumbs with no base are a crumble scattered on top, never a crumb crust (a crust is pressed as a base).
        warnings.append("yield crumb container with no base read as a crumble top (D96)")
        top = "crumble"
    if not base and top in ("crumble", "none") and component:
        # No rolled piece: what the model listed as component lines is the topping.
        topping = topping + [i for i in component if i not in topping]
        component = []
        warnings.append("yield container with no rolled piece: component_lines moved to topping_lines")
    return {
        "pastry": pastry,
        "base": base,
        "sides": _word(c.sides, SIDES, DEFAULT_SIDES, "sides", warnings),
        "top": top,
        "thickness": _word(c.thickness, THICKNESSES, DEFAULT_THICKNESS, "thickness", warnings),
        "component_lines": component,
        "topping_lines": topping,
    }


def _area_dough(ingredients: Sequence[StructuredIngredient]) -> Dict[str, object]:
    """The container Cayenne assumes for a component by area that has none: a flat pizza-dough base, every
    ingredient line (headings excluded) the rolled component."""
    return {
        "pastry": "pizza-dough",
        "base": True,
        "sides": "none",
        "top": DEFAULT_TOP,
        "thickness": DEFAULT_THICKNESS,
        "component_lines": [i.id for i in ingredients if i.kind != "heading"],
        "topping_lines": [],
    }


def _unsplit(container: Optional[Dict[str, object]]) -> Optional[Dict[str, object]]:
    """A recipe demoted to servings has no part split: both line lists empty, so every line takes the number."""
    return None if container is None else {**container, "component_lines": [], "topping_lines": []}


def yield_text(servings_min: object, servings_max: object, servings_note: Optional[str]) -> Optional[str]:
    """A stored row's yield words as the cook sees them (Cayenne's formatServingsSpan): "4", "2 to 4", then the
    note ("16 Enough for double crust for a standard pie plate"). A one-sided span shows its one number.
    None when there is nothing. Pure."""
    low, high = _whole(servings_min), _whole(servings_max)
    parts: List[str] = []
    if low is not None and high is not None:
        parts.append(str(low) if low == high else f"{low} to {high}")
    elif low is not None or high is not None:
        parts.append(str(low if low is not None else high))
    note = (servings_note or "").strip()
    if note:
        parts.append(note)
    return " ".join(parts) or None


def check_yield(
    answer: Optional[YieldAnswer],
    ingredients: Sequence[StructuredIngredient],
    pans: Sequence[Any],
) -> Tuple[Optional[dict], List[str]]:
    """The yield_spec to store, or None, and the warnings. Pure. It never raises and never costs the recipe.

    - an unknown kind -> None;
    - a container is kept on any kind (an assembly is a servings or pieces recipe with one), unless it
      has no piece at all (no base and no top), which drops it with a warning; the check never
      guesses from the tins;
    - a field outside its vocabulary, or a null pastry, takes the default, with a warning;
    - `area` is read only on a component, with a whole count of 1 or more and diameter_mm within 50..600;
    - `pieces` is kept only on a pieces kind, with a whole count of 1 or more; weight_g within 10..5000 or
      None; diameter_mm within 10..1000 or None; a weight is never asked of a component;
    - component_lines / topping_lines are 1-based positions into `ingredients` (headings included)
      and are mapped to ids; one out of range or naming a heading is dropped with a warning; a line
      in both lists is component;
    - a component stays a component whatever its tins and whether or not it has an area (D97, Cayenne yield
      kinds 7.8: the client fits one with neither); a crumb crust is a component like any other (D95);
    - a `crumb` container with no base is a crumble scattered on top (D96): its top reads 'crumble' and its
      component lines move to topping_lines, so it is never dropped as having no base and no top;
    - `pans` is not read since D97 (kept for the four callers);
    - a pieces kind with no kept `pieces` group (no whole count of 1 or more) is stored as 'servings' too, container kept
      with empty line lists, since a pieces recipe is scaled by that count and cannot be scaled without it;
    - a component that keeps its `area` but has no container (none given, or one dropped) gets the pizza-dough
      container Cayenne already assumes for it, every ingredient line its component, with a warning (F-559);
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
            diameter = p.diameter_mm if _in_range(p.diameter_mm, _PIECE_DIAMETER_MM) else None
            if p.diameter_mm is not None and diameter is None:
                warnings.append(f"yield piece diameter {p.diameter_mm} mm is outside 10..1000; dropped")
            pieces = {"count": count, "noun": p.noun or "", "weight_g": weight, "diameter_mm": diameter}

    if kind == "pieces" and pieces is None:
        warnings.append("yield pieces with no count stored as servings")
        kind = "servings"
        container = _unsplit(container)
    # F-559: the model sometimes drops a dough's container between runs. Cayenne reads a component by area
    # with no container as pizza dough (yieldScale, ScalingSheetBody), so store that container explicitly.
    if kind == "component" and area is not None and container is None:
        warnings.append("yield component by area with no container stored as pizza dough")
        container = _area_dough(ingredients)

    return {"kind": kind, "container": container, "area": area, "pieces": pieces, "edited": False}, warnings
