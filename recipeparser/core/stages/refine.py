"""
recipeparser/core/stages/refine.py — REFINE stage.

Wraps gemini.refine_recipe_for_cayenne() to convert a raw RecipeExtraction
into a structured CayenneRefinement (Fat Tokens + UOM conversion +
categorization in one Gemini call).

No imports from recipeparser.io or recipeparser.adapters are permitted here.
"""
import logging
import re
from typing import Any, Dict, List, Optional

from recipeparser.core.numbers import written_values
from recipeparser.gemini import refine_recipe_for_cayenne
from recipeparser.models import SOURCE_SYSTEMS, CayenneRefinement, RecipeExtraction

log = logging.getLogger(__name__)

# Fat Token regex — must match {{ing_01|fallback text}}
_FAT_TOKEN_RE = re.compile(r"\{\{([^|]+)\|([^}]+)\}\}")


def _validate_fat_tokens(refinement: CayenneRefinement) -> None:
    """
    Verify that every Fat Token in tokenized_directions references a valid
    ingredient ID.  Raises ValueError on the first violation found.
    """
    valid_ids = {ing.id for ing in refinement.structured_ingredients}
    for step in refinement.tokenized_directions:
        for match in _FAT_TOKEN_RE.finditer(step.text):
            token_id = match.group(1)
            if token_id not in valid_ids:
                raise ValueError(
                    "refine(): Fat Token '{{" + token_id + "|...}}' in step "
                    + str(step.step)
                    + f" references unknown ingredient ID '{token_id}'. "
                    + f"Valid IDs: {sorted(valid_ids)}"
                )


def _normalise_line_index(refinement: CayenneRefinement, raw: RecipeExtraction) -> None:
    """
    Clear any ``line_index`` that does not point at a distinct line of
    ``raw.ingredients``, in place.  Valid indices are left untouched.

    ``line_index`` is optional by design: the model is ``Optional[int]`` and
    spec 4.3 defines the client-side fallback for a missing or wrong index (the
    client compares ``fallback_string`` against the line and bumps ``body_rev``
    instead of writing an override).  An out-of-range or duplicated index is a
    plausible model slip on a long sectioned ingredient list, and raising here
    propagates to the per-chunk error boundary and drops the whole recipe at
    ingest — destroying a recipe over a cosmetic error in a field whose absence
    the design already tolerates.  Degrade the offending entries to ``None`` and
    log which ones, so the fallback path handles them.
    """
    n_lines = len(raw.ingredients)
    seen: Dict[int, str] = {}
    dropped: List[str] = []
    for ing in refinement.structured_ingredients:
        idx = ing.line_index
        if idx is None:
            continue
        if idx < 0 or idx >= n_lines:
            dropped.append(f"'{ing.id}' (line_index {idx}, {n_lines} raw line(s))")
            ing.line_index = None
        elif idx in seen:
            dropped.append(f"'{ing.id}' (line_index {idx} already claimed by '{seen[idx]}')")
            ing.line_index = None
        else:
            seen[idx] = ing.id
    if dropped:
        log.warning(
            "refine(): '%s' — dropped %d unusable line_index value(s); the client falls "
            "back to fallback_string matching for these (spec 4.3): %s",
            getattr(raw, "name", "?"), len(dropped), ", ".join(dropped),
        )


# What an AI conversion may be written in (D3): grams and millilitres have one size everywhere.
_METRIC_UNITS = {"g", "gram", "grams", "ml", "millilitre", "millilitres", "milliliter", "milliliters"}
_QUOTES = "\"'“”‘’"


# Which kind each unit word measures, so an AI conversion into the SAME kind can be refused (D3:
# converted_* is the OTHER measure). The words are Cayenne's registry spellings; a unit not listed
# ("pinch", "st") has no kind and its conversion is left alone.
_VOLUME_WORDS = {
    "tsp", "teaspoon", "teaspoons", "tbsp", "tablespoon", "tablespoons", "tbs", "cup", "cups",
    "fl oz", "fluid ounce", "fluid ounces", "pint", "pints", "pt", "quart", "quarts", "qt",
    "gallon", "gallons", "gal", "ml", "millilitre", "millilitres", "milliliter", "milliliters",
    "cl", "dl", "l", "litre", "litres", "liter", "liters", "gill", "gills", "teacup", "teacups",
    "teacupful", "breakfast cup", "breakfast cups", "dessertspoon", "dessertspoons", "dsp",
    "fl dram", "fluid dram", "fluid drams",
}
_WEIGHT_WORDS = {
    "g", "gram", "grams", "kg", "kilogram", "kilograms", "oz", "ounce", "ounces", "lb", "lbs",
    "pound", "pounds", "stone", "stones", "dram", "drams", "drachm", "drachms",
}


def _kind(unit: Optional[str]) -> Optional[str]:
    word = re.sub(r"\.$", "", re.sub(r"\s+", " ", (unit or "").strip().lower()))
    if word in _VOLUME_WORDS:
        return "volume"
    if word in _WEIGHT_WORDS:
        return "weight"
    return None


def _tolerance(value: float) -> float:
    """Half the last decimal place ``value`` states: 0.333 matches 1/3, 0.3 matches 0.33."""
    text = repr(float(value))
    decimals = len(text.split(".", 1)[1]) if "." in text and "e" not in text else 0
    return 0.5 * 10 ** -decimals


def _check_conversions(refinement: CayenneRefinement) -> None:
    """
    D3, in place. A second measure marked as the writer's must equal a number its own line writes;
    when none does it is re-marked as the AI's. Then an AI conversion in anything but grams or
    millilitres is dropped, re-marked ones included: a model error costs a missing "≈", never a
    false claim that the writer gave a figure, and never an AI cup the source system would resize.
    An AI conversion into the same kind as the line's own unit ("1 lb" -> 454 g) is dropped too: it
    is not the other measure, and it would leave the Weight and Volume pills with nothing to show.
    """
    for ing in refinement.structured_ingredients:
        if ing.converted_amount is None:
            continue
        if not ing.is_ai_converted:
            tol = _tolerance(ing.converted_amount)
            if not any(abs(v - ing.converted_amount) <= tol for v in written_values(ing.fallback_string)):
                log.warning("refine(): %s's second measure %s is not in its line — treating it as the AI's.",
                            ing.id, ing.converted_amount)
                ing.is_ai_converted = True
        if ing.is_ai_converted and (ing.converted_unit or "").strip().lower() not in _METRIC_UNITS:
            log.warning("refine(): dropped %s's AI conversion in %r — only g or ml is admissible.",
                        ing.id, ing.converted_unit)
            ing.converted_amount = None
            ing.converted_unit = None
            ing.is_ai_converted = False
        elif ing.is_ai_converted and _kind(ing.unit) is not None and _kind(ing.unit) == _kind(ing.converted_unit):
            log.warning("refine(): dropped %s's AI conversion %s %s — the same kind as %r, not the other measure.",
                        ing.id, ing.converted_amount, ing.converted_unit, ing.unit)
            ing.converted_amount = None
            ing.converted_unit = None
            ing.is_ai_converted = False


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().strip(_QUOTES)).lower()


def _recipe_text(raw: RecipeExtraction) -> str:
    """The recipe text the prompt showed the model (the RecipeExtraction's repr fields)."""
    parts = [raw.name, raw.servings or "", raw.prep_time or "", raw.cook_time or "", raw.notes or ""]
    return "\n".join([*parts, *raw.ingredients, *raw.directions])


_CANONICAL_SYSTEMS = {s.lower(): s for s in SOURCE_SYSTEMS}


def _host_names(quote: str, source_host: str) -> bool:
    """
    True when ``quote`` names the given host: the host itself, or a non-empty suffix of it that starts
    at a dot boundary (".com.au", "com.au"). A leading "www." is ignored on both. Never an arbitrary
    substring: "aste.com" does not name "taste.com.au".
    """
    host = _plain(source_host).removeprefix("www.")
    quote = quote.removeprefix("www.")
    bare = quote.lstrip(".")
    if not bare:
        return False
    return host == bare or host.endswith("." + bare)


def _check_detection(refinement: CayenneRefinement, raw: RecipeExtraction, source_host: Optional[str]) -> None:
    """
    D5, in place. The detected system must be one of the five (in any case; written canonically)
    and its evidence must be a quote from the recipe text or name the source host as given (the host
    or a dot-boundary suffix of it); otherwise both are written null.
    """
    detected = refinement.source_uom_system_detected
    system = _CANONICAL_SYSTEMS.get((detected or "").strip().lower())
    quote = _plain(refinement.source_uom_system_evidence or "")
    supported = (
        system is not None
        and quote != ""
        and ((source_host is not None and _host_names(quote, source_host)) or quote in _plain(_recipe_text(raw)))
    )
    if supported:
        refinement.source_uom_system_detected = system
    else:
        if detected is not None:
            log.warning("refine(): dropped detected system %r — its evidence %r is not in the recipe or its host.",
                        detected, refinement.source_uom_system_evidence)
        refinement.source_uom_system_detected = None
        refinement.source_uom_system_evidence = None


def refine(
    raw: RecipeExtraction,
    client: Any,
    *,
    source_host: Optional[str] = None,
    user_axes: Optional[Dict[str, List[str]]] = None,
) -> CayenneRefinement:
    """
    Refine a raw RecipeExtraction into a structured CayenneRefinement.

    This is Pass 2 of the pipeline.  A single Gemini call handles:
      - Structured ingredient parsing (id, amount, unit, name, fallback_string)
      - Fat Token injection into direction text
      - The writer's second measure, else a g/ml conversion (flagged as is_ai_converted),
        and the detected source system with its evidence (D3, D5)
      - Multipolar categorization via grid_categories (when user_axes provided)

    Args:
        raw:               The RecipeExtraction from the EXTRACT stage.
        client:            An initialised ``google.genai.Client`` instance.
        source_host:       The recipe's source host (core.citation.host_of), or None
                           for pasted text and books. Shown to the model and accepted
                           as detection evidence only when given.
        user_axes:         Optional dict of axis_name → [tag, ...].
                           When None or empty, grid_categories will be {} in
                           the result.

    Returns:
        A validated ``CayenneRefinement`` object.

    Raises:
        ValueError: If Gemini returns None, or if Fat Token validation fails.

    Note:
        An unusable ``line_index`` (out of range, or claimed twice) is *not* an
        error: the offending entries are set to ``None`` with a warning and the
        recipe is kept.  The field is optional and spec 4.3 defines the
        client-side fallback for it.
    """
    result = refine_recipe_for_cayenne(
        raw_recipe=raw,
        client=client,
        source_host=source_host,
        user_axes=user_axes,
    )

    if result is None:
        raise ValueError(
            f"refine(): Gemini returned None for recipe '{getattr(raw, 'name', '?')}'. "
            "The refinement call failed — check logs for details."
        )

    _validate_fat_tokens(result)
    _normalise_line_index(result, raw)
    _check_conversions(result)
    _check_detection(result, raw, source_host)
    log.info(
        "refine(): '%s' → %d ingredients, %d steps.",
        result.title,
        len(result.structured_ingredients),
        len(result.tokenized_directions),
    )
    return result
