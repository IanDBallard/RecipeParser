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

from recipeparser.core.numbers import written_measures
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


def _writes_unit(text: str, unit: Optional[str]) -> bool:
    """
    True when ``text`` names ``unit`` as a word: in any case, singular or plural, a full stop after
    each word or none ("oz." is "oz", "cup" is "cups", "fl oz" is "fl. oz."). Letters may not adjoin
    it, digits may: "250g" writes "g", "grated" does not.
    """
    word = re.sub(r"\.$", "", re.sub(r"\s+", " ", (unit or "").strip().lower()))
    if not word:
        return False
    if len(word) > 2 and word.endswith("s"):
        word = word[:-1]
    body = r"\.?\s*".join(re.escape(w) for w in word.split(" "))
    return re.search(r"(?<![^\W\d_])" + body + r"s?(?![^\W\d_])", text.lower()) is not None


def _check_conversions(refinement: CayenneRefinement) -> None:
    """
    D3, in place. A second measure marked as the writer's must be a number its own line writes, in
    the unit the model gave for it (Fix Roadmap F-011: a number the line writes under another unit,
    "cut into 2 cm cubes", is not the writer's 2 cups); when none is it is re-marked as the AI's.
    Then an AI conversion in anything but grams or millilitres is dropped, re-marked ones included:
    a model error costs a missing "≈", never a false claim that the writer gave a figure, and never
    an AI cup the source system would resize. An AI conversion into the same kind as the line's own
    unit ("1 lb" -> 454 g) is dropped too: it is not the other measure, and it would leave the Weight
    and Volume pills with nothing to show.
    """
    for ing in refinement.structured_ingredients:
        if ing.converted_amount is None:
            continue
        if not ing.is_ai_converted:
            tol = _tolerance(ing.converted_amount)
            if not any(abs(v - ing.converted_amount) <= tol and _writes_unit(after, ing.converted_unit)
                       for v, after in written_measures(ing.fallback_string)):
                log.warning("refine(): %s's second measure %s %s is not in its line — treating it as the AI's.",
                            ing.id, ing.converted_amount, ing.converted_unit)
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


# Fix Roadmap F-010: words that say nothing about the system on their own. Every system writes a cup,
# a spoon, an ounce, a pint and a gram, so "cup" or "1 ounce" is not evidence; neither are grams beside
# cups (the prompt's own rule). "stone", "gill", "teacup" and "breakfast" are absent: they are the
# pre-metric British measures the prompt names as evidence. "dessertspoon" is present: the prompt says
# it alone is not evidence.
_BARE_WORDS = {
    "t", "tsp", "teaspoon", "teaspoons", "tbsp", "tbs", "tbl", "tablespoon", "tablespoons",
    "c", "cup", "cups", "dessertspoon", "dessertspoons", "dsp", "fl", "fluid", "oz", "ounce", "ounces",
    "pint", "pints", "pt", "quart", "quarts", "qt", "gallon", "gallons", "gal",
    "ml", "millilitre", "millilitres", "milliliter", "milliliters", "cl", "dl", "l", "litre", "litres",
    "liter", "liters", "g", "gram", "grams", "kg", "kilogram", "kilograms", "lb", "lbs", "pound", "pounds",
    "a", "an", "of", "the", "and", "or", "about", "x",
}
# A measure-only quote is evidence when it states a size in millilitres or fluid ounces: "1 cup (250 ml)",
# "(237 ml)", "1 tbsp (20 ml)", "a 20 fl oz pint" (the prompt's first kind of evidence).
_STATED_SIZE = re.compile(r"\d\s*(?:ml|millilit(?:re|er)s?|fl\.?\s*oz|fluid\s+ounces?)(?![^\W\d_])")


def _quoted_from(quote: str, text: str) -> bool:
    """
    True when ``quote`` (already plain) is evidence the plain ``text`` writes: whole words of it, not a
    piece of one ("ted pecorino" is not in "grated pecorino"), and with some substance: a word beyond
    the measures every system writes, or a stated millilitre or fluid-ounce size.
    """
    words = re.findall(r"[^\W\d_]+", quote)
    if not any(w not in _BARE_WORDS for w in words) and not _STATED_SIZE.search(quote):
        return False
    # A word boundary only where the quote's own edge is a word character: "(237ml)" still counts in
    # "1 cup(237ml) cream".
    head = r"(?<!\w)" if re.match(r"\w", quote) else ""
    tail = r"(?!\w)" if re.search(r"\w$", quote) else ""
    return re.search(head + re.escape(quote) + tail, text) is not None


# Fix Roadmap F-108: a quote of substance still verified any of the five systems, "2 cups flour" as UK
# or Imperial alike. The quote must now carry, for the system it is offered for, one of the refine
# prompt's kinds of evidence (gemini.py, SOURCE SYSTEM): a unit size the prompt names, a statement
# naming the measures, a pre-metric British measure, or a regional name. The names are a closed list,
# the prompt's own and a few as unambiguous; a genuine one outside it costs the detection, never a
# wrong one. "US" and "EU" count only in capitals ("let us bake"); "AU" not at all ("au gratin").
_SIZE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(ml|millilit(?:re|er)s?|fl\.?\s*oz|fluid\s+ounces?)(?![^\W\d_])")
# A stated size and the systems it names: the US cup (and half), the metric cup (and half), the
# Australian 20 ml tablespoon, the 568 ml / 20 fl oz Imperial pint (and half).
_ML_SIZES = {
    236: ("US",), 237: ("US",), 240: ("US",), 118: ("US",), 120: ("US",),
    250: ("UK", "EU", "AU"), 125: ("UK", "EU", "AU"), 20: ("AU",), 568: ("Imperial",), 284: ("Imperial",),
}
_FL_OZ_SIZES = {8: ("US",), 20: ("Imperial",), 10: ("Imperial",)}
_FRACTION = "¼-¾⅐-⅞"
_SYSTEM_WORDS = {
    "US": r"\bamerican?\b|all[- ]purpose flour|heavy (?:whipping )?cream|half[- ]and[- ]half"
          r"|confectioners'? sugar|sticks? of butter",
    "UK": r"\bbritish\b|\bbritain\b|\buk\b|\bnew zealand\b|\bnz\b|\bmetric\b|cast[eo]r sugar"
          r"|plain flour|self[- ]raising flour|double cream|single cream",
    "EU": r"\beurope(?:an)?\b|\bmetric\b",
    "AU": r"\baustralian?\b|\bmetric\b",
    "Imperial": r"\bimperial\b|\bbritish\b|\bpre-metric\b|\bgills?\b|\bteacups?(?:fuls?)?\b|\bbreakfast ?cups?\b"
                r"|(?:\d|[" + _FRACTION + r"]|\bone|\bhalf a)\s*stones?\b",
}
_SYSTEM_CAPITALS = {"US": r"(?<![A-Za-z])U\.?S\.?(?:A\.?)?(?![A-Za-z])", "EU": r"(?<![A-Za-z])EU(?![A-Za-z])"}


def _indicates(system: str, quote: str, written: str) -> bool:
    """True when the plain ``quote`` (``written``: as the model gave it) is evidence of ``system``."""
    for figure, unit in _SIZE.findall(quote):
        sizes = _ML_SIZES if unit.startswith(("ml", "millilit")) else _FL_OZ_SIZES
        if float(figure).is_integer() and system in sizes.get(int(float(figure)), ()):
            return True
    if re.search(_SYSTEM_WORDS[system], quote):
        return True
    capitals = _SYSTEM_CAPITALS.get(system)
    return capitals is not None and re.search(capitals, written) is not None


def _check_detection(refinement: CayenneRefinement, raw: RecipeExtraction, source_host: Optional[str]) -> None:
    """
    D5, in place. The detected system must be one of the five (in any case; written canonically)
    and its evidence must be a quote from the recipe text (whole words, with some substance:
    ``_quoted_from``) that is evidence of that system (``_indicates``), or name the source host as
    given (the host or a dot-boundary suffix of it); otherwise both are written null.
    """
    detected = refinement.source_uom_system_detected
    system = _CANONICAL_SYSTEMS.get((detected or "").strip().lower())
    quote = _plain(refinement.source_uom_system_evidence or "")
    named_host = source_host is not None and _host_names(quote, source_host)
    supported = system is not None and quote != "" and (
        named_host
        or (_quoted_from(quote, _plain(_recipe_text(raw)))
            and _indicates(system, quote, refinement.source_uom_system_evidence or ""))
    )
    if supported:
        refinement.source_uom_system_detected = system
    else:
        if detected is not None:
            log.warning("refine(): dropped detected system %r — its evidence %r is not"
                        " a quote from the recipe that names that system, nor its host.",
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
