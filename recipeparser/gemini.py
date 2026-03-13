"""All Gemini API calls with retry, timeout, and rate-limit back-off."""
import logging
import re
import time
from typing import Optional, List

from recipeparser.config import BACKOFF_BASE_SECS, BACKOFF_MAX_SECS, MAX_RETRIES
from recipeparser.models import RecipeList, CayenneRefinement

log = logging.getLogger(__name__)


def _is_rate_limit_error(exc: Exception) -> bool:
    """Return True if the exception looks like a 429 / quota error."""
    msg = str(exc).lower()
    return "429" in msg or "quota" in msg or "resource_exhausted" in msg


def _call_with_retry(client, model: str, contents: str, config: dict) -> object:
    """
    Wrapper around client.models.generate_content that retries on rate-limit
    errors with exponential back-off, and raises for all other errors.
    """
    delay = BACKOFF_BASE_SECS
    for attempt in range(1, MAX_RETRIES + 2):
        try:
            return client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
        except Exception as exc:
            if _is_rate_limit_error(exc) and attempt <= MAX_RETRIES:
                log.warning(
                    "Rate-limit hit (attempt %d/%d) — waiting %ds before retry.",
                    attempt,
                    MAX_RETRIES,
                    delay,
                )
                time.sleep(delay)
                delay = min(delay * 2, BACKOFF_MAX_SECS)
            else:
                raise


def verify_connectivity(client) -> bool:
    """
    Send a minimal single-token request to confirm the API key is valid and
    the Generative Language API is enabled before processing any real content.
    Returns True if the API is reachable, False otherwise.
    """
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents="Reply with the single word OK.",
            config={"max_output_tokens": 5, "temperature": 0},
        )
        log.info("Gemini connectivity check passed (response: %s).", response.text.strip())
        return True
    except Exception as e:
        log.error("Gemini connectivity check FAILED: %s", e)
        return False


def get_embeddings(text: str, client) -> List[float]:
    """Generates a 1536-dimension embedding for the given text."""
    from google.genai import types as genai_types
    try:
        response = client.models.embed_content(
            model="models/gemini-embedding-001",
            contents=text,
            config=genai_types.EmbedContentConfig(output_dimensionality=1536),
        )
        return response.embeddings[0].values
    except Exception as e:
        log.error("Embedding generation failed: %s", e)
        raise  # Don't silently return zeros — surface the real error


def needs_table_normalisation(text: str) -> bool:
    """
    Detect multi-column baker's percentage ingredient tables that confuse the
    LLM extractor.  The apostrophe in "Baker's" is sometimes mangled into a
    Unicode replacement character (U+FFFD), so we match it loosely.
    """
    upper = text.upper()
    return bool(
        re.search(r"BAKER.S %", upper)
        or re.search(r"BAKER.S PERCENTAGE", upper)
    )


def normalise_baker_table(text_chunk: str, client) -> str:
    """
    Pre-process a chunk containing multi-column baker's percentage tables by
    asking Gemini to reformat them into readable per-ingredient lines.
    Returns the reformatted text, or the original text unchanged if the call fails.
    """
    prompt = f"""The following text is from a recipe book and contains one or more ingredient tables
where each ingredient name, its weight, its volume measure, and its baker's percentage
appear on separate lines rather than in columns.

Reformat ONLY the ingredient table sections so that each ingredient appears on a single
line in the format: "IngredientName: weight (volume) — baker's%"

Do NOT change any recipe titles, headings, method steps, notes, or any other text.
Do NOT add or remove any ingredients or values.
Preserve all [IMAGE: ...] markers exactly as they appear.

Text:
{text_chunk}"""

    try:
        response = _call_with_retry(
            client,
            model="gemini-2.5-flash",
            contents=prompt,
            config={"temperature": 0},
        )
        normalised = response.text.strip()
        if normalised:
            log.info(
                "  -> Table normalisation applied (%d -> %d chars).",
                len(text_chunk),
                len(normalised),
            )
            return normalised
        log.warning("  -> Table normalisation returned empty response; using original text.")
        return text_chunk
    except Exception as e:
        log.warning("  -> Table normalisation failed (%s); using original text.", e)
        return text_chunk


_UNITS_RULES = {
    # Keep only the metric (gram/ml) measurement from dual-unit lines like
    # "2 cups/250g flour" → "250g flour"
    "metric": (
        "- Many ingredient lines contain dual measurements in the format "
        "\"US-measure/metric-weight ingredient\" (e.g. \"2 cups/250g flour\", "
        "\"14 tablespoons/200g butter\"). "
        "Keep ONLY the metric (gram or ml) part and discard the US volume part. "
        "Output just \"250g flour\", \"200g butter\", etc."
    ),
    # Keep only the US volume/weight measurement
    "us": (
        "- Many ingredient lines contain dual measurements in the format "
        "\"US-measure/metric-weight ingredient\" (e.g. \"2 cups/250g flour\", "
        "\"14 tablespoons/200g butter\"). "
        "Keep ONLY the US measure part and discard the metric part. "
        "Output just \"2 cups flour\", \"14 tablespoons butter\", etc."
    ),
    # Keep imperial (oz/lb) where present; for dual-unit lines prefer metric
    "imperial": (
        "- Where ingredients are given with dual measurements "
        "(e.g. \"2 cups/250g flour\"), keep the metric (gram/ml) part. "
        "Where ounces or pounds appear, keep those as-is."
    ),
    # Default: preserve whatever the book uses, no stripping
    "book": "",
}


def extract_recipe_from_text(
    text: str,
    client,
) -> Optional[RecipeList]:
    """
    Extract a single recipe from plain text (e.g. from a Paprika import or
    pasted recipe).  Uses a simpler, more direct prompt than extract_recipes
    which is tuned for EPUB/PDF book chunks.
    """
    prompt = f"""
You are a culinary data extractor. The following text is a recipe. Extract it.

Rules:
- Extract the recipe title, servings, prep time, cook time, ingredients, and directions.
- Ingredients: one item per list entry. Convert unicode fractions (½, ¼, ¾) to plain text (1/2, 1/4, 3/4).
- Directions: one step per list entry.
- If a field is absent from the text, leave it null.
- Do not invent or infer values not present in the text.
- photo_filename: always null (no images in plain text).

Text:
{text}
"""
    try:
        response = _call_with_retry(
            client,
            model="gemini-2.5-flash",
            contents=prompt,
            config={
                "response_mime_type": "application/json",
                "response_schema": RecipeList,
                "temperature": 0.1,
            },
        )
        return response.parsed
    except Exception as e:
        log.error("Gemini plain-text extraction failed: %s", e)
        return None


def extract_recipes(
    text_chunk: str,
    client,
    units: str = "book",
) -> Optional[RecipeList]:
    """
    Call Gemini with the extraction prompt and return a parsed RecipeList.
    Applies retry/back-off for rate-limit errors; returns None on failure.

    ``units`` controls how dual-measurement ingredient lines are handled:
      "metric"   — keep only gram/ml values  (e.g. "250g flour")
      "us"       — keep only US cup/tbsp values
      "imperial" — keep only oz/lb values (falls back to metric for dual lines)
      "book"     — preserve whatever the book uses (default)
    """
    units_rule = _UNITS_RULES.get(units.lower(), "")
    units_section = f"\n{units_rule}" if units_rule else ""

    prompt = f"""
You are a culinary data extractor. Review the following text from an EPUB recipe book.
Extract ALL distinct recipes found in the text.

Rules:
- If you see a [HERO IMAGE: filename.jpg] marker, ALWAYS use that filename as
  photo_filename — it is the confirmed finished-dish photo for this recipe.
- Otherwise, if you see [IMAGE: filename.jpg] markers, assign exactly ONE filename
  to photo_filename: the image most likely to be the hero/finished-dish photo.
  The hero image can appear in any of these positions:
    a) immediately BEFORE the recipe title or ingredient list, OR
    b) immediately AFTER the last ingredient and BEFORE the first method step.
  Both positions are common depending on the book's layout.
  IGNORE images that appear embedded WITHIN the numbered method steps — those are
  instructional process shots, not the finished dish.
  If there is only one [IMAGE:] marker in the recipe, use it unless it is clearly
  mid-method (e.g. appears after "Step 2" or "Step 3" text).
  If no hero image is identifiable, leave photo_filename null.
- Convert all unicode fractions (½, ¼, ¾, etc.) to plain text (1/2, 1/4, 3/4, etc.).
- If a recipe uses multiple phases, stages, or days (e.g. "PHASE 1 / PHASE 2",
  "Day 1 / Day 2", "Soaker / Final Dough"), preserve ALL phases in full.
  Insert the phase label as a bold heading entry using Markdown bold syntax, e.g.:
    ingredients: ["**Phase 1**", "28g whole wheat flour", "28g pineapple juice",
                  "**Phase 2**", "56g whole wheat flour", "56g water"]
    directions:  ["**Phase 1**", "Mix flour and juice.", "**Phase 2**", "Add remaining flour."]
  The bold label must be its own separate list item, followed by that phase's
  ingredients or steps as normal list items.
  Do NOT flatten, merge, or skip any phase — the reader must follow them in order.
- If a field is entirely absent from the text, leave it null.
- Do not invent or infer values that are not present in the text.{units_section}

Text chunk:
{text_chunk}
"""

    try:
        response = _call_with_retry(
            client,
            model="gemini-2.5-flash",
            contents=prompt,
            config={
                "response_mime_type": "application/json",
                "response_schema": RecipeList,
                "temperature": 0.1,
            },
        )
        return response.parsed
    except Exception as e:
        log.error("Gemini extraction failed: %s", e)
        return None


def extract_text_via_vision(doc, client) -> str:
    """
    OCR fallback for scanned PDFs that contain no extractable text.

    Renders each page to a PNG pixmap via PyMuPDF and sends the images to
    Gemini's vision input.  Returns the concatenated plain-text transcript
    of all pages, separated by double newlines.

    Args:
        doc:    An open ``fitz.Document`` (PyMuPDF).  Must NOT be closed
                before this function returns.
        client: An initialised ``google.genai.Client`` instance.

    Returns:
        A non-empty string of extracted text, or raises ``RuntimeError`` if
        Gemini returns nothing useful for every page.
    """
    import fitz  # PyMuPDF — already a dependency; imported here to keep gemini.py PDF-agnostic
    from google.genai import types as genai_types

    VISION_PROMPT = (
        "You are an OCR assistant. The image is a page from a recipe document. "
        "Transcribe ALL text exactly as it appears — including the recipe title, "
        "ingredient quantities and names, and every numbered direction step. "
        "Preserve line breaks between sections. "
        "Do NOT add commentary, summaries, or any text not present in the image."
    )

    page_texts: List[str] = []
    for page_num in range(doc.page_count):
        page = doc[page_num]
        # Render at 2× scale (144 DPI) for legibility — good balance of quality vs. token cost.
        pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2))
        image_bytes = pixmap.tobytes("png")

        try:
            response = _call_with_retry(
                client,
                model="gemini-2.5-flash",
                contents=[
                    genai_types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
                    VISION_PROMPT,
                ],
                config={"temperature": 0},
            )
            page_text = (response.text or "").strip()
            if page_text:
                page_texts.append(page_text)
                log.info(
                    "Vision OCR page %d/%d: extracted %d chars.",
                    page_num + 1,
                    doc.page_count,
                    len(page_text),
                )
            else:
                log.warning("Vision OCR page %d/%d: empty response.", page_num + 1, doc.page_count)
        except Exception as exc:
            log.warning("Vision OCR page %d/%d failed: %s", page_num + 1, doc.page_count, exc)

    if not page_texts:
        raise RuntimeError(
            "Gemini Vision returned no text for any page in the scanned PDF. "
            "The document may contain non-recipe imagery or be unreadable."
        )

    return "\n\n".join(page_texts)


def refine_recipe_for_cayenne(
    raw_recipe: object,
    client,
    uom_system: str = "US",
    measure_preference: str = "Volume"
) -> Optional[CayenneRefinement]:
    """Post-processing pass to convert raw text recipe into high-fidelity Cayenne data."""
    prompt = f"""
You are a culinary data refiner. Transform this raw recipe into the structured Cayenne format.

RULES:
1. STRUCTURED INGREDIENTS:
   - Assign each ingredient a unique ID (ing_01, ing_02, etc.).
   - Extract numeric "amount", "unit" (null if unitless), and "name".
   - "fallback_string" is the original full line.
   - CONVERSION: If preference is "Weight" and source is "Volume", provide "converted_amount" and "converted_unit" (e.g. 1 cup -> 120g). Set "is_ai_converted" to true.

2. TOKENIZED DIRECTIONS:
   - Rewrite directions using Fat Tokens: {{{{ingredient_id|original_text}}}}
   - Example: "Mix the flour" -> "Mix the {{{{ing_01|flour}}}}"

CONTEXT:
UOM System: {uom_system}
Measure Preference: {measure_preference}

RAW RECIPE:
{raw_recipe}
"""
    try:
        response = _call_with_retry(
            client,
            model="gemini-2.5-flash",
            contents=prompt,
            config={
                "response_mime_type": "application/json",
                "response_schema": CayenneRefinement,
                "temperature": 0.1,
            },
        )
        return response.parsed
    except Exception as e:
        log.error("Cayenne refinement failed: %s", e)
        return None
