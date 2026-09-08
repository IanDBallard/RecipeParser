"""All Gemini API calls with retry, timeout, and rate-limit back-off."""
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Type

from google.genai import errors as genai_errors
from pydantic import BaseModel, create_model, Field, ValidationError

from recipeparser.config import (
    BACKOFF_BASE_SECS,
    BACKOFF_MAX_SECS,
    GEMINI_EMBEDDING_MODEL,
    GEMINI_MODEL,
    HTTP_TIMEOUT_SECS,
    MAX_PARSE_RETRIES,
    MAX_RETRIES,
    PARSE_RETRY_DELAY_SECS,
    THINKING_BUDGET,
)
from recipeparser.exceptions import ExtractionParseError
from recipeparser.models import RecipeList, CayenneRefinement

log = logging.getLogger(__name__)


def _strip_additional_properties(obj: Any) -> Any:
    """
    Recursively remove all 'additionalProperties' keys from a JSON schema dict.
    Gemini API rejects schemas containing additionalProperties (see googleapis/python-genai#70).
    """
    if isinstance(obj, dict):
        return {
            k: _strip_additional_properties(v)
            for k, v in obj.items()
            if k != "additionalProperties"
        }
    if isinstance(obj, list):
        return [_strip_additional_properties(item) for item in obj]
    return obj


def _schema_for_gemini(schema_class: Type[BaseModel]) -> dict:
    """
    Return a JSON schema suitable for Gemini by stripping additionalProperties.
    Use with response_json_schema; the API will constrain output but we parse manually.
    """
    raw = schema_class.model_json_schema()
    return _strip_additional_properties(raw)


def _is_rate_limit_error(exc: Exception) -> bool:
    """Return True if the exception looks like a 429 / quota error."""
    msg = str(exc).lower()
    return "429" in msg or "quota" in msg or "resource_exhausted" in msg


# gRPC-style status names for a transient server-side failure, matched only
# against google.genai.errors.APIError.status — never against free-form
# exception text (see _is_transient_server_error).
_TRANSIENT_STATUS_NAMES = frozenset({"UNAVAILABLE", "DEADLINE_EXCEEDED", "INTERNAL"})


def _is_transient_server_error(exc: Exception) -> bool:
    """Return True if *exc* looks like a transient server-side failure worth
    retrying through the existing back-off ladder: HTTP 500/502/503/504, or
    the gRPC-style UNAVAILABLE/DEADLINE_EXCEEDED/INTERNAL.

    Detection is exclusively typed/structured — it never inspects the
    exception message. google-genai's ``APIError.raise_error`` (google.genai
    .errors) raises ``ClientError`` for any 4xx status and ``ServerError``
    for any 5xx status, and every ``APIError`` carries a numeric ``.code``
    and a string ``.status`` (e.g. "UNAVAILABLE"); that is what every real
    call in this module actually raises on an HTTP-level failure, so typed
    detection fully covers the defect this function exists to fix (a 504
    aborting a recording run on attempt 1).

    A message-substring fallback was deliberately rejected, not merely
    avoided: this repo's own tests/test_gemini.py has two pre-existing,
    unrelated tests that raise a plain ``Exception("503 Service
    Unavailable")`` expecting it to propagate on attempt 1 with no retry
    and no sleep. Any text-based match for "503" or "UNAVAILABLE" reclassifies
    that plain exception as retryable and burns the real (unpatched, in
    those tests) 5x back-off ladder — turning a ~4s suite into a ~130s one.
    ``tests/goldens/golden_client.py``'s ``MissingRecordingError`` (whose
    message embeds a hex sha8 path segment such as
    ``.../gemini/f.epub/8fb1500a/extract-00.json``, containing "500") is the
    same trap from the other direction. Typed-only detection rules out both
    by construction: neither is a ``genai_errors.APIError``.
    """
    if not isinstance(exc, genai_errors.APIError):
        return False
    if isinstance(exc, genai_errors.ClientError):
        return False
    if isinstance(exc, genai_errors.ServerError):
        return True
    code = getattr(exc, "code", None)
    if isinstance(code, int) and 500 <= code < 600:
        return True
    return (getattr(exc, "status", None) or "").upper() in _TRANSIENT_STATUS_NAMES


# google-genai's GenerateContentConfig.http_options.timeout is in MILLISECONDS
# (confirmed against installed google-genai==1.68.0: HttpOptions.timeout's
# docstring says "Timeout for the request in milliseconds", and
# google.genai._api_client.get_timeout_in_seconds divides it by 1000.0 before
# handing it to httpx). HTTP_TIMEOUT_SECS is expressed in seconds, so it must
# be multiplied by 1000 here — passing it unconverted would give every real
# call an unusable ~0.18s timeout.
_HTTP_TIMEOUT_MS = HTTP_TIMEOUT_SECS * 1000


def _finalize_config(config: dict) -> dict:
    """Return a copy of ``config`` with the per-call HTTP timeout and the
    thinking budget applied.

    Passed per-call (not at client construction) because ``_call_with_retry``
    only ever receives an already-constructed ``client`` — this is the one
    place in the call path that can attach them, and ``generate_content``
    validates a plain ``config`` dict into ``GenerateContentConfig``, whose
    ``http_options.timeout`` bounds the underlying HTTP request and whose
    ``thinking_config.thinking_budget`` bounds reasoning-token spend.

    Every call this module makes is a bounded extraction, refinement, or
    classification task with one correct answer, not open-ended reasoning, so
    THINKING_BUDGET defaults to 0 — thinking tokens bill at the output rate
    and buy nothing here.
    """
    return {
        **config,
        "http_options": {"timeout": _HTTP_TIMEOUT_MS},
        "thinking_config": {"thinking_budget": THINKING_BUDGET},
    }


def _log_usage_metadata(response: object, what: str) -> None:
    """Best-effort log of the token accounting a Gemini reply carries.

    Nothing in this module previously looked at ``usage_metadata``, so every
    cost estimate for the ingestion pipeline was a guess from prompt length
    alone. This puts the real per-call numbers — including any thinking
    tokens, when THINKING_BUDGET allows them — into the log instead.
    """
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return
    log.info(
        "%s usage: prompt=%s candidates=%s thoughts=%s total=%s",
        what,
        getattr(usage, "prompt_token_count", None),
        getattr(usage, "candidates_token_count", None),
        getattr(usage, "thoughts_token_count", None),
        getattr(usage, "total_token_count", None),
    )


def _call_with_retry(
    client, model: str, contents: str, config: dict, *, what: str = "Gemini call"
) -> object:
    """
    Wrapper around client.models.generate_content that retries on rate-limit
    errors and transient server errors with exponential back-off, and raises
    for all other errors.

    A transport-level timeout is deliberately NOT retried: it is neither a
    rate-limit signal nor a transient server error (a server-returned 504 is
    a different thing from a local timeout), so it raises immediately on the
    first attempt instead of burning the 5x exponential back-off ladder on a
    call that already waited the full HTTP_TIMEOUT_SECS.
    """
    delay = BACKOFF_BASE_SECS
    for attempt in range(1, MAX_RETRIES + 2):
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=_finalize_config(config),
            )
            _log_usage_metadata(response, what)
            return response
        except Exception as exc:
            retryable = _is_rate_limit_error(exc) or _is_transient_server_error(exc)
            if retryable and attempt <= MAX_RETRIES:
                log.warning(
                    "Transient error hit (attempt %d/%d) — waiting %ds before retry: %s",
                    attempt,
                    MAX_RETRIES,
                    delay,
                    exc,
                )
                time.sleep(delay)
                delay = min(delay * 2, BACKOFF_MAX_SECS)
            else:
                raise


def _finish_reason(response: object) -> str:
    """Best-effort finish reason from a genai response; empty when absent."""
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return ""
    return str(getattr(candidates[0], "finish_reason", "") or "")


def _generate_and_parse(client, model: str, contents: str, config: dict, *, what: str) -> RecipeList:
    """Call Gemini and parse the reply, retrying a reply that will not parse.

    ``_call_with_retry`` covers transport and quota failures. It cannot cover a
    200 response whose body is truncated JSON, because nothing raised — which is
    why such a reply was previously swallowed and its recipes lost. The parse
    therefore happens inside this loop, not after it.

    Raises:
        ExtractionParseError: every attempt returned something unparseable.
    """
    last_error = "no attempt made"
    for attempt in range(1, MAX_PARSE_RETRIES + 2):
        response = _call_with_retry(client, model=model, contents=contents, config=config, what=what)
        text = (getattr(response, "text", "") or "").strip()
        if text:
            try:
                return RecipeList.model_validate(json.loads(text))
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
        else:
            last_error = "empty response"
        log.warning(
            "%s: unparseable reply (attempt %d/%d) — finish_reason=%s, first line=%r",
            what, attempt, MAX_PARSE_RETRIES + 1, _finish_reason(response),
            text.splitlines()[0][:200] if text else "",
        )
        if attempt <= MAX_PARSE_RETRIES:
            time.sleep(PARSE_RETRY_DELAY_SECS)
    raise ExtractionParseError(
        f"{what}: {MAX_PARSE_RETRIES + 1} attempts all unparseable "
        f"(finish_reason={_finish_reason(response)}); last error: {last_error}"
    )


def verify_connectivity(client) -> bool:
    """
    Send a minimal single-token request to confirm the API key is valid and
    the Generative Language API is enabled before processing any real content.
    Returns True if the API is reachable, False otherwise.
    """
    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents="Reply with the single word OK.",
            config=_finalize_config({"max_output_tokens": 5, "temperature": 0}),
        )
        _log_usage_metadata(response, "Connectivity check")
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
            model=GEMINI_EMBEDDING_MODEL,
            contents=text,
            config=genai_types.EmbedContentConfig(output_dimensionality=1536),
        )
        _log_usage_metadata(response, "Embedding")
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


def build_table_prompt(text_chunk: str) -> str:
    """The prompt that reformats a multi-column baker's percentage table."""
    return f"""The following text is from a recipe book and contains one or more ingredient tables
where each ingredient name, its weight, its volume measure, and its baker's percentage
appear on separate lines rather than in columns.

Reformat ONLY the ingredient table sections so that each ingredient appears on a single
line in the format: "IngredientName: weight (volume) — baker's%"

Do NOT change any recipe titles, headings, method steps, notes, or any other text.
Do NOT add or remove any ingredients or values.
Preserve all [IMAGE: ...] markers exactly as they appear.

Text:
{text_chunk}"""


def normalise_baker_table(text_chunk: str, client) -> str:
    """
    Pre-process a chunk containing multi-column baker's percentage tables by
    asking Gemini to reformat them into readable per-ingredient lines.
    Returns the reformatted text, or the original text unchanged if the call fails.
    """
    prompt = build_table_prompt(text_chunk)

    try:
        response = _call_with_retry(
            client,
            model=GEMINI_MODEL,
            contents=prompt,
            config={"temperature": 0},
            what="Table normalisation",
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


def build_plain_text_prompt(text: str) -> str:
    """The prompt for a single recipe in plain text (Paprika import, pasted recipe)."""
    return f"""
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


def extract_recipe_from_text(
    text: str,
    client,
) -> RecipeList:
    """
    Extract a single recipe from plain text (e.g. from a Paprika import or
    pasted recipe).  Uses a simpler, more direct prompt than extract_recipes
    which is tuned for EPUB/PDF book chunks.

    Raises:
        ExtractionParseError: every attempt's reply could not be parsed.
    """
    prompt = build_plain_text_prompt(text)
    return _generate_and_parse(
        client,
        model=GEMINI_MODEL,
        contents=prompt,
        config={
            "response_mime_type": "application/json",
            "response_json_schema": _schema_for_gemini(RecipeList),
            "temperature": 0.1,
        },
        what="Gemini plain-text extraction",
    )


def build_extract_prompt(text_chunk: str, units: str = "book") -> str:
    """The prompt for a book chunk that may hold several recipes."""
    units_rule = _UNITS_RULES.get(units.lower(), "")
    units_section = f"\n{units_rule}" if units_rule else ""
    return f"""
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


def extract_recipes(
    text_chunk: str,
    client,
    units: str = "book",
) -> RecipeList:
    """
    Call Gemini with the extraction prompt and return a parsed RecipeList.
    Applies retry/back-off for rate-limit errors and for a reply that will
    not parse.

    ``units`` controls how dual-measurement ingredient lines are handled:
      "metric"   — keep only gram/ml values  (e.g. "250g flour")
      "us"       — keep only US cup/tbsp values
      "imperial" — keep only oz/lb values (falls back to metric for dual lines)
      "book"     — preserve whatever the book uses (default)

    Raises:
        ExtractionParseError: every attempt's reply could not be parsed.
    """
    prompt = build_extract_prompt(text_chunk, units)

    return _generate_and_parse(
        client,
        model=GEMINI_MODEL,
        contents=prompt,
        config={
            "response_mime_type": "application/json",
            "response_json_schema": _schema_for_gemini(RecipeList),
            "temperature": 0.1,
        },
        what="Gemini extraction",
    )


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
                model=GEMINI_MODEL,
                contents=[
                    genai_types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
                    VISION_PROMPT,
                ],
                config={"temperature": 0},
                what=f"Vision OCR page {page_num + 1}/{doc.page_count}",
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


def _build_dynamic_grid_schema(user_axes: Dict[str, List[str]]) -> type:
    """
    Build a dynamic Pydantic model at runtime that extends CayenneRefinement
    with a ``grid_categories`` field whose per-axis sub-fields are constrained
    to the exact tags the user has defined.

    Each axis becomes a field typed ``List[str]`` with a description that lists
    the valid tags.  The LLM is instructed (via field description) to return []
    for axes that don't apply — enforcing the Zero-Tag Mandate.

    Args:
        user_axes: Dict mapping axis name → list of valid tag strings.
                   e.g. {"Cuisine": ["Italian", "Mexican"], "Protein": ["Chicken"]}

    Returns:
        A dynamically-created Pydantic model class that Gemini can use as a
        response_schema.  When user_axes is empty, returns CayenneRefinement
        unchanged (no categorization fields added).
    """
    if not user_axes:
        return CayenneRefinement

    # Build per-axis sub-model fields: each axis → List[str] with valid tags in description
    axis_fields: Dict[str, tuple] = {}
    for axis_name, tags in user_axes.items():
        tags_str = ", ".join(f'"{t}"' for t in tags)
        axis_fields[axis_name] = (
            List[str],
            Field(
                default_factory=list,
                description=(
                    f"Tags for the '{axis_name}' axis. "
                    f"Choose 0-2 tags from this exact list: [{tags_str}]. "
                    f"Return [] if none apply — do NOT invent tags outside this list."
                ),
            ),
        )

    # Create the per-axis grid sub-model
    GridModel = create_model("GridCategories", **axis_fields)

    # Extend CayenneRefinement with the typed grid_categories field
    RefinementWithGrid = create_model(
        "CayenneRefinementWithGrid",
        __base__=CayenneRefinement,
        grid_categories=(
            GridModel,
            Field(
                default_factory=GridModel,
                description=(
                    "Multipolar categorization. For each axis, pick 0-2 matching tags "
                    "from the provided list. Return [] for axes that don't apply."
                ),
            ),
        ),
    )

    return RefinementWithGrid


def _format_axes_for_prompt(user_axes: Dict[str, List[str]]) -> str:
    """Format user_axes into a human-readable prompt section."""
    if not user_axes:
        return ""
    lines = ["", "3. CATEGORIZATION (grid_categories):"]
    lines.append(
        "   Classify this recipe using the user's taxonomy axes below. "
        "For each axis, select 0-2 tags that best describe the recipe. "
        "Return [] for any axis that does not apply. "
        "NEVER invent tags outside the provided lists."
    )
    for axis_name, tags in user_axes.items():
        tags_str = ", ".join(f'"{t}"' for t in tags)
        lines.append(f"   - {axis_name}: [{tags_str}]")
    return "\n".join(lines)


def build_refine_prompt(
    raw_recipe: object,
    uom_system: str,
    measure_preference: str,
    user_axes: Optional[Dict[str, List[str]]] = None,
) -> str:
    """The Pass-2 prompt: structured ingredients, fat tokens, and categorisation."""
    categorization_section = _format_axes_for_prompt(user_axes or {})
    return f"""
You are a culinary data refiner. Transform this raw recipe into the structured Cayenne format.

RULES:
1. STRUCTURED INGREDIENTS:
   - Assign each ingredient a unique ID (ing_01, ing_02, etc.).
   - Extract numeric "amount", "unit" (null if unitless), and "name".
   - "fallback_string" is the original full line.
   - CONVERSION: If preference is "Weight" and source is "Volume", provide "converted_amount" and "converted_unit" (e.g. 1 cup -> 120g). Set "is_ai_converted" to true.
   - amount: the numeric quantity. Use null - never 0 - when the source states no
     amount ("salt to taste", "a pinch of nutmeg"). A zero would be read as a real
     measurement of nothing.

2. TOKENIZED DIRECTIONS:
   - Rewrite directions using Fat Tokens: {{{{ingredient_id|original_text}}}}
   - Example: "Mix the flour" -> "Mix the {{{{ing_01|flour}}}}"
{categorization_section}
CONTEXT:
UOM System: {uom_system}
Measure Preference: {measure_preference}

RAW RECIPE:
{raw_recipe}
"""


def refine_recipe_for_cayenne(
    raw_recipe: object,
    client,
    uom_system: str = "US",
    measure_preference: str = "Volume",
    user_axes: Optional[Dict[str, List[str]]] = None,
) -> Optional[CayenneRefinement]:
    """
    Post-processing pass to convert raw text recipe into high-fidelity Cayenne data.

    Combines Fat Token generation, UOM conversion, and multipolar categorization
    into a single LLM call (Pass 2).

    Args:
        raw_recipe:        The raw RecipeExtraction object from Pass 1.
        client:            Initialised Gemini client.
        uom_system:        "US", "Metric", or "Imperial".
        measure_preference: "Volume" or "Weight".
        user_axes:         Optional dict of axis_name → [tag, ...] for categorization.
                           When None or empty, grid_categories will be {} in the result.
    """
    axes = user_axes or {}
    schema = _build_dynamic_grid_schema(axes)

    prompt = build_refine_prompt(raw_recipe, uom_system, measure_preference, axes)
    try:
        # Use response_json_schema with additionalProperties stripped — Gemini API
        # rejects response_schema when Pydantic emits additionalProperties (Dict types).
        json_schema = _schema_for_gemini(schema)
        response = _call_with_retry(
            client,
            model=GEMINI_MODEL,
            contents=prompt,
            config={
                "response_mime_type": "application/json",
                "response_json_schema": json_schema,
                "temperature": 0.1,
            },
            what="Cayenne refinement",
        )
        # response_json_schema does not auto-parse; we get raw JSON text.
        if not response.text or not response.text.strip():
            log.error("Cayenne refinement failed: Gemini returned empty response")
            return None
        raw_data = json.loads(response.text)
        result = schema.model_validate(raw_data)

        # When a dynamic schema was used, grid_categories is a nested sub-model.
        # Normalize it back to a plain Dict[str, List[str]] on the CayenneRefinement.
        if axes and result is not None:
            raw_grid = result.grid_categories
            if hasattr(raw_grid, "model_dump"):
                # It's a Pydantic sub-model — convert to plain dict
                normalized: Dict[str, List[str]] = {
                    k: v for k, v in raw_grid.model_dump().items()
                    if isinstance(v, list)
                }
            elif isinstance(raw_grid, dict):
                normalized = raw_grid
            else:
                normalized = {}

            # Re-validate: strip any tags not in the user's defined lists
            clean_grid: Dict[str, List[str]] = {}
            for axis_name, selected_tags in normalized.items():
                valid_tags = set(axes.get(axis_name, []))
                clean_grid[axis_name] = [t for t in selected_tags if t in valid_tags]

            # Return a proper CayenneRefinement with the cleaned grid
            return CayenneRefinement(
                title=result.title,
                base_servings=result.base_servings,
                structured_ingredients=result.structured_ingredients,
                tokenized_directions=result.tokenized_directions,
                grid_categories=clean_grid,
            )

        return result
    except Exception as e:
        log.error("Cayenne refinement failed: %s", e)
        return None
