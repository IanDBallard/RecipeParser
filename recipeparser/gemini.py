"""Gemini API calls.

Every call that goes through ``_call_with_retry`` carries the HTTP timeout,
rate-limit back-off, and transient-server-error retry. ``get_embeddings`` and
``verify_connectivity`` call the client directly and carry the timeout only —
an embedding has no parse step worth retrying, and the connectivity probe is
meant to return a fast verdict rather than retry a dead key five times.
"""
import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Type, TypeVar

from google.genai import errors as genai_errors
from pydantic import BaseModel, Field, ValidationError, create_model

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
from recipeparser.models import CayenneRefinement, DirectionMention, DirectionMentions, RecipeList, StructuredIngredient

if TYPE_CHECKING:
    from recipeparser.core.rate_limiter import GlobalRateLimiter

log = logging.getLogger(__name__)

T = TypeVar("T")


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
        # Merge into any http_options the caller already set rather than
        # replacing it: an api_version pin, custom headers or a base_url
        # override would otherwise be dropped silently on the way to the SDK.
        # The timeout is a CEILING: a caller may tighten it (the shopping
        # classify call's 60 s), never lengthen it — min() applied last keeps
        # merging from becoming a way to opt out of the bound.
        "http_options": {
            **config.get("http_options", {}),
            # A falsy caller timeout (0, or an explicit None) must still become
            # a real bound: `.get(..., default)` only supplies the default when
            # the key is absent, so a caller-set 0 would reach min() as 0 (an
            # unbounded request to the SDK) and a caller-set None would reach
            # min() as None and raise TypeError against _HTTP_TIMEOUT_MS. `or`
            # catches both before min() ever sees them.
            "timeout": min(
                config.get("http_options", {}).get("timeout") or _HTTP_TIMEOUT_MS,
                _HTTP_TIMEOUT_MS,
            ),
        },
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
    client,
    model: str,
    contents: str,
    config: dict,
    *,
    what: str = "Gemini call",
    limiter: Optional["GlobalRateLimiter"] = None,
    max_retries: int = MAX_RETRIES,
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

    ``limiter``: every request after the first takes a slot from it (Fix Roadmap
    F-109: a back-off retry is a request like any other). The first attempt's slot
    is the caller's to take, or it would count twice. The limiter holds no slot
    across a wait, so taking one here cannot deadlock. None takes no slot.

    ``max_retries`` lets a caller with a person waiting (POST /embed,
    the shopping classify call) shorten the ladder, as ``get_embeddings``
    already does.
    """
    def _once() -> object:
        response = client.models.generate_content(
            model=model,
            contents=contents,
            config=_finalize_config(config),
        )
        _log_usage_metadata(response, what)
        return response

    return _with_backoff(_once, limiter=limiter, max_retries=max_retries)


def _with_backoff(
    call: Callable[[], T],
    *,
    limiter: Optional["GlobalRateLimiter"] = None,
    max_retries: int = MAX_RETRIES,
) -> T:
    """Run ``call``, retrying a rate-limit or transient server error with exponential back-off.

    The one back-off ladder, for ``generate_content`` (``_call_with_retry``) and
    ``embed_content`` (``get_embeddings``, F-132) alike. Anything else raises at once.
    ``limiter`` is as for ``_call_with_retry``: every attempt after the first takes a slot.
    """
    delay = BACKOFF_BASE_SECS
    for attempt in range(1, max_retries + 2):
        if attempt > 1 and limiter is not None:
            limiter.wait_then_record_start()
        try:
            return call()
        except Exception as exc:
            retryable = _is_rate_limit_error(exc) or _is_transient_server_error(exc)
            if retryable and attempt <= max_retries:
                log.warning(
                    "Transient error hit (attempt %d/%d) — waiting %ds before retry: %s",
                    attempt,
                    max_retries,
                    delay,
                    exc,
                )
                time.sleep(delay)
                delay = min(delay * 2, BACKOFF_MAX_SECS)
            else:
                raise
    raise AssertionError("unreachable: the last attempt returns or raises")


def _finish_reason(response: object) -> str:
    """Best-effort finish reason from a genai response; empty when absent."""
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return ""
    return str(getattr(candidates[0], "finish_reason", "") or "")


def _generate_and_parse(
    client,
    model: str,
    contents: str,
    config: dict,
    *,
    what: str,
    limiter: Optional["GlobalRateLimiter"] = None,
) -> RecipeList:
    """Call Gemini and parse the reply, retrying a reply that will not parse.

    ``_call_with_retry`` covers transport and quota failures. It cannot cover a
    200 response whose body is truncated JSON, because nothing raised — which is
    why such a reply was previously swallowed and its recipes lost. The parse
    therefore happens inside this loop, not after it.

    ``limiter`` is as for ``_call_with_retry``: a parse retry takes a slot too
    (F-109), and the first attempt's is the caller's.

    Raises:
        ExtractionParseError: every attempt returned something unparseable.
    """
    last_error = "no attempt made"
    for attempt in range(1, MAX_PARSE_RETRIES + 2):
        if attempt > 1 and limiter is not None:
            limiter.wait_then_record_start()
        response = _call_with_retry(client, model=model, contents=contents, config=config, what=what,
                                    limiter=limiter)
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
        # Timeout but deliberately NOT _call_with_retry: this is a preflight
        # probe whose value is a fast verdict before real work starts, so a
        # dead key should fail in one attempt rather than burn the five-retry
        # back-off ladder.
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


def get_embeddings(
    text: str,
    client,
    *,
    limiter: Optional["GlobalRateLimiter"] = None,
    max_retries: int = MAX_RETRIES,
) -> List[float]:
    """Generates a 1536-dimension embedding for the given text.

    A rate-limit or transient server error is retried with the same back-off as
    every ``generate_content`` call (Fix Roadmap F-132): until then one 503
    failed an ingest's EMBED step, or a regeneration, outright. ``max_retries``
    lets a caller with a person waiting (``POST /embed``) shorten the ladder.
    """
    from google.genai import types as genai_types

    def _once() -> object:
        response = client.models.embed_content(
            model=GEMINI_EMBEDDING_MODEL,
            contents=text,
            # Bounded like every generate_content call: EmbedContentConfig
            # carries its own http_options, so the timeout goes on the typed
            # config rather than through _finalize_config's dict merge.
            # This is the EMBED stage of every run — an unbounded call here
            # hangs an ordinary import.
            config=genai_types.EmbedContentConfig(
                output_dimensionality=1536,
                http_options={"timeout": _HTTP_TIMEOUT_MS},
            ),
        )
        _log_usage_metadata(response, "Embedding")
        return response

    try:
        response = _with_backoff(_once, limiter=limiter, max_retries=max_retries)
        return response.embeddings[0].values  # type: ignore[attr-defined]
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


def normalise_baker_table(text_chunk: str, client, *, limiter: Optional["GlobalRateLimiter"] = None) -> str:
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
            limiter=limiter,
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


def build_plain_text_prompt(text: str) -> str:
    """The prompt for a single recipe in plain text (Paprika import, pasted recipe)."""
    return f"""
You are a culinary data extractor. The following text is a recipe. Extract it.

Rules:
- Extract the recipe title, servings, prep time, cook time, ingredients, and directions.
- Ingredients: one item per list entry, copied exactly as the text writes it: the same numbers,
  the same units, every measure the line gives, in the same order. Never convert, round, drop or
  add a measure. The one change allowed: write unicode fractions (½, ¼, ¾) as plain text (1/2, 1/4, 3/4).
- Directions: one step per list entry.
- If a field is absent from the text, leave it null.
- stated_source is the publication or book as the text names itself; byline is the author's
  name if one is printed. Never the name of a model or a tool. Leave both null if the text
  does not say.
- total_time: the recipe's stated total or overall time, only when the text states one.
  Never add prep and cook together.
- description: the headnote or introduction printed with the recipe, in its own words, at
  most one paragraph. null when there is none.
- nutritional_info: the recipe's nutrition statement, verbatim, as one line. null when the
  text carries none.
- Do not invent or infer values not present in the text.
- photo_filename: always null (no images in plain text).

Text:
{text}
"""


def extract_recipe_from_text(
    text: str,
    client,
    *,
    limiter: Optional["GlobalRateLimiter"] = None,
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
        limiter=limiter,
    )


def build_extract_prompt(text_chunk: str) -> str:
    """The prompt for a book chunk that may hold several recipes."""
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
- Copy every ingredient line exactly as the text writes it: the same numbers, the same units,
  every measure the line gives, in the same order. Never convert, round, drop or add a measure.
  The one change allowed: write unicode fractions (½, ¼, ¾, etc.) as plain text (1/2, 1/4, 3/4, etc.).
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
- stated_source is the publication or book as the text names itself; byline is the author's
  name if one is printed. Never the name of a model or a tool. Leave both null if the text
  does not say.
- total_time: the recipe's stated total or overall time, only when the text states one.
  Never add prep and cook together.
- description: the headnote or introduction printed with the recipe, in its own words, at
  most one paragraph. null when there is none.
- nutritional_info: the recipe's nutrition statement, verbatim, as one line. null when the
  text carries none.
- Do not invent or infer values that are not present in the text.

Text chunk:
{text_chunk}
"""


def extract_recipes(
    text_chunk: str,
    client,
    *,
    limiter: Optional["GlobalRateLimiter"] = None,
) -> RecipeList:
    """
    Call Gemini with the extraction prompt and return a parsed RecipeList.
    Applies retry/back-off for rate-limit errors and for a reply that will
    not parse.

    Every ingredient line is copied verbatim (verbatim-ingestion D1); the stage holds the model to it.

    Raises:
        ExtractionParseError: every attempt's reply could not be parsed.
    """
    prompt = build_extract_prompt(text_chunk)

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
        limiter=limiter,
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
            "Gemini Vision returned no text for any page of the document. "
            "It may contain non-recipe imagery or be unreadable."
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


# How much of an ingredient one mention in the directions uses (Cayenne's direction amounts
# design). One text, shared by REFINE's rule 2 and the re-tagging pass, so the two cannot drift.
_MENTION_USES = """   - Say how much of the ingredient each mention uses, as "use":
       * all  - the whole amount the ingredient line gives goes in here, at once. Only the FIRST
         time it does: later mentions of the same food ("drain the rice", "until the rice is
         done", "the butter mixture") are none.
       * an amount - the direction itself writes a quantity for this mention. The words are ONLY
         that quantity as written ("1/2 cup (60 g)", "2", "two tablespoons"), never the
         ingredient's name ("Add 1 cup peas": the words are "1 cup", not "1 cup peas"), and the
         use is the quantity as a decimal number and a unit: "0.5 cup", "60 g", "2" for a count.
         If the direction names the ingredient without writing a quantity beside it, the use is
         all, rest or none - never an amount, even when you know the line's amount.
       * rest - what is left after amounts the directions stated earlier: "the remaining flour",
         "the rest of the sugar". The words are the ingredient's name.
       * none - the ingredient is named but no amount follows from the text: "add more flour
         until sticky", "a bit more", "season with salt", "salt to taste", "Salt it", a share
         ("half the cider", "a third of the flour"), a rate ("1 teaspoon at a time", "2 per
         ball"), or a later mention of food already added.
   - When unsure, say "none". A missing number is safe; a wrong one is not.
   - Wrap only the ingredient's words, never "the", "your" or "of": "the flour" -> the words are "flour"."""


def build_refine_prompt(
    raw_recipe: object,
    source_host: Optional[str],
    user_axes: Optional[Dict[str, List[str]]] = None,
) -> str:
    """The Pass-2 prompt: structured ingredients, fat tokens, the source system and categorisation.

    ``SOURCE HOST`` sits before ``RAW RECIPE:`` because the golden replay keys a refine call by the
    text after that marker (tests/goldens/golden_client.py).
    """
    categorization_section = _format_axes_for_prompt(user_axes or {})
    return f"""
You are a culinary data refiner. Transform this raw recipe into the structured Cayenne format.

RULES:
1. STRUCTURED INGREDIENTS:
   - Assign each ingredient a unique ID (ing_01, ing_02, etc.).
   - Extract numeric "amount", "unit" (null if unitless), and "name".
   - "fallback_string" is the original full line, unchanged.
   - "line_index" is the 0-based position of the source line in the RAW RECIPE
     ingredients list. Every ingredient line gets exactly one entry with its
     index. A section header line (e.g. "For the sauce:") gets no entry.
   - STATE: for every ingredient with an amount and a volume or weight unit, say whether it is
     "liquid" - pourable as the recipe uses it: water, milk, cream, stock, oil, wine, juice,
     vinegar, honey, syrup, melted butter - or "solid" - everything else, including flour,
     sugar, salt, cold butter, chopped vegetables and meat. Put it in "state". Leave "state"
     null when the line has no amount or its unit is neither a volume nor a weight.
     Use the state when you compute the conversion below.
   - DUAL MEASURES: when a line states two measures ("1 cup (240 g) flour", "250g/2 cups flour",
     "1 ounce (about 1/3 packed cup) grated pecorino"), the FIRST measure the writer gives is
     "amount" and "unit". When the second is the other kind (a weight beside a volume, or a volume
     beside a weight), put it in "converted_amount" and "converted_unit" exactly as the line writes
     it and set "is_ai_converted" to false. When both are the same kind ("1 pound (450g)"), the
     second is not structured: apply the CONVERSION rule below.
   - CONVERSION: for a line with one measure, give the equivalent in the OTHER measure, in grams or
     millilitres only: a weight for a volume ("1 cup" flour -> 120, "g"), a volume for a weight
     ("150 g" flour -> 300, "ml"). Never answer in cups, spoons, ounces or pints. Put it in
     "converted_amount" and "converted_unit" and set "is_ai_converted" to true. Leave
     "converted_amount" and "converted_unit" null and "is_ai_converted" false when the line
     states no amount ("salt to taste") or its unit is neither a volume nor a weight ("3 eggs",
     "a pinch").
     Sizes for older British measures: gill = 142 ml (a US gill = 118 ml); teacup = 142 ml; breakfast cup = 227 ml; dessertspoon = 10 ml; stone = 14 lb; dram = 1/16 oz (a weight).
   - Never change a number or a unit the line writes: "amount" and "unit" are the line's own.
   - amount: the numeric quantity. Use null - never 0 - when the source states no
     amount ("salt to taste", "a pinch of nutmeg"). A zero would be read as a real
     measurement of nothing.

2. TOKENIZED DIRECTIONS:
   - Rewrite each direction with Fat Tokens: {{{{ingredient_id|words|use}}}}. Copy the direction
     exactly; replacing every token with its words must give back the direction as written.
{_MENTION_USES}
   - Examples:
       "Whisk together the flour and salt" -> "Whisk together the {{{{ing_01|flour|all}}}} and {{{{ing_03|salt|all}}}}"
       "Add about 1/2 cup (60 g) flour" -> "Add about {{{{ing_02|1/2 cup (60 g)|0.5 cup}}}} flour"
       "Add the remaining flour" -> "Add the remaining {{{{ing_02|flour|rest}}}}"
       "Add more flour until sticky" -> "Add more {{{{ing_02|flour|none}}}} until sticky"

3. PHASES:
   - If the raw recipe groups its ingredients or directions into phases,
     stages, or days (a bold heading entry such as "**Phase 1**", "**Day 1**",
     "**Soaker**"), keep every one of those headings as its own entry, in
     place, in both lists.
   - A heading is not an ingredient: give it no amount, no unit, and no fat
     tokens. Carry the heading text through as the entry's fallback_string
     (ingredients) or text (directions).
   - Do NOT flatten, merge, renumber, or drop a phase. The reader must still
     be able to tell where one session ends and the next begins.

4. SOURCE SYSTEM:
   - "source_uom_system_detected": the measuring system the writer used, exactly one of "US",
     "UK", "EU", "AU", "Imperial", judged only from evidence in the RAW RECIPE or the SOURCE HOST:
       * a size the recipe states for a unit: "1 cup (240 ml)" or "(237 ml)" is US; "1 cup (250 ml)"
         is a metric cup (UK, EU or AU); "1 tbsp (20 ml)" is AU; "1 pint (568 ml)" or a 20 fl oz
         pint is Imperial;
       * the pre-metric British measures: "breakfast cup", "teacup" or "stone" as a measure, or "gill" when nothing points to the US, is Imperial; "dessertspoon" alone is not evidence (modern UK and Australian recipes still use it);
       * a statement about the measures ("Australian standard measures", "US cup measures");
       * the SOURCE HOST: .co.uk is UK, .com.au is AU, .co.nz is UK (New Zealand's measures are UK
         Metric's);
       * spelling and ingredient names only one country uses ("caster sugar", "plain flour" and
         "double cream" are UK; "all-purpose flour" and "heavy cream" are US).
     Grams printed beside cups are not evidence: American writers print them too.
   - "source_uom_system_evidence": the words you relied on, quoted exactly from the RAW RECIPE, or
     the SOURCE HOST exactly as given. One short quote.
   - Leave both null when nothing above applies.
{categorization_section}
SOURCE HOST: {source_host or "none"}

RAW RECIPE:
{raw_recipe}
"""


def refine_recipe_for_cayenne(
    raw_recipe: object,
    client,
    source_host: Optional[str] = None,
    user_axes: Optional[Dict[str, List[str]]] = None,
    *,
    limiter: Optional["GlobalRateLimiter"] = None,
) -> Optional[CayenneRefinement]:
    """
    Post-processing pass to convert raw text recipe into high-fidelity Cayenne data.

    Combines Fat Token generation, UOM conversion, and multipolar categorization
    into a single LLM call (Pass 2).

    Args:
        raw_recipe:        The raw RecipeExtraction object from Pass 1.
        client:            Initialised Gemini client.
        source_host:       The recipe's source host (core.citation.host_of), or None — the prompt's
                           only evidence outside the recipe text.
        user_axes:         Optional dict of axis_name → [tag, ...] for categorization.
                           When None or empty, grid_categories will be {} in the result.
        limiter:           Takes a slot for each retry (F-109; see _call_with_retry). The
                           caller takes the first request's.
    """
    axes = user_axes or {}
    schema = _build_dynamic_grid_schema(axes)

    prompt = build_refine_prompt(raw_recipe, source_host, axes)
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
            limiter=limiter,
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
                source_uom_system_detected=result.source_uom_system_detected,
                source_uom_system_evidence=result.source_uom_system_evidence,
            )

        return result
    except Exception as e:
        log.error("Cayenne refinement failed: %s", e)
        return None


def build_mentions_prompt(ingredients: List[StructuredIngredient], steps: List[str]) -> str:
    """The re-tagging prompt (Cayenne's direction amounts design): a recipe already refined, its
    ingredient lines with their ids, and its raw steps. The answer is quotes, not rewritten text, so
    ``core.fat_tokens.splice_mentions`` can place each token where its words are and the steps keep
    every word they had."""
    lines = "\n".join(f"  {i.id}: {i.fallback_string}" for i in ingredients)
    numbered = "\n".join(f"  {n}. {text}" for n, text in enumerate(steps, start=1))
    return f"""
You are a culinary data refiner. For the recipe below, find every mention of an ingredient in the
directions and say how much of that ingredient each mention uses.

RULES:
   - Report the mentions of each step in the order they appear in it.
   - "quote" is the mention's words, copied character for character from that step.
   - "context" is the quote with about five words around it, copied character for character from
     the step, so two mentions with the same words can be told apart.
   - "ingredient_id" is the id of the ingredient line the words refer to.
   - A heading step ("**Phase 1**") has no mentions.
{_MENTION_USES}
   - Examples, as step text -> quote, context, use:
       "Whisk together the flour and salt" -> "flour", "Whisk together the flour and", all;
                                              "salt", "the flour and salt", all
       "Place the butter and sugar in a bowl" -> "butter", "Place the butter and sugar", all;
                                                 "sugar", "butter and sugar in a bowl", all
       "Heat 1 tablespoon of the oil" -> "1 tablespoon", "Heat 1 tablespoon of the oil", "1 tablespoon"
       "Add about 1/2 cup (60 g) flour; add more flour until sticky"
           -> "1/2 cup (60 g)", "Add about 1/2 cup (60 g) flour", "0.5 cup";
              "flour", "add more flour until sticky", none
       "Add the remaining flour" -> "flour", "Add the remaining flour", rest

INGREDIENTS:
{lines}

DIRECTIONS:
{numbered}
"""


def tag_direction_mentions(
    ingredients: List[StructuredIngredient],
    steps: List[str],
    client,
    *,
    limiter: Optional["GlobalRateLimiter"] = None,
) -> List[DirectionMention]:
    """Every ingredient mention in ``steps`` with its use, from one Gemini call. Raises on failure:
    the caller (a backfill) decides whether a recipe it could not tag is skipped or retried."""
    response = _call_with_retry(
        client,
        model=GEMINI_MODEL,
        contents=build_mentions_prompt(ingredients, steps),
        config={
            "response_mime_type": "application/json",
            "response_json_schema": _schema_for_gemini(DirectionMentions),
            "temperature": 0.1,
        },
        what="Direction mentions",
        limiter=limiter,
    )
    if not response.text or not response.text.strip():
        raise RuntimeError("tag_direction_mentions(): Gemini returned an empty response")
    return DirectionMentions.model_validate(json.loads(response.text)).mentions


class _RecipeTags(BaseModel):
    recipe_id: str
    tags: List[str] = Field(default_factory=list)


class _BatchCategorization(BaseModel):
    results: List[_RecipeTags] = Field(default_factory=list)


def build_categorize_batch_prompt(
    recipes: List[Dict[str, Any]],
    new_axes: Dict[str, List[str]],
) -> str:
    """The bulk-recategorise prompt: several recipes against newly added tags only.

    The body columns are read through ``raw_body_column``, which raises on a
    double-encoded jsonb column rather than letting it through. Iterating a
    ``str`` yields characters, so a double-encoded ``ingredient_lines`` would
    put forty single-character "ingredients" in this prompt and the model would
    classify the result without complaint. That encoding is not hypothetical:
    all 786 rows in the live library once carried it on
    ``structured_ingredients``. The raise is the point -- a batch that cannot be
    described honestly is a batch for ``RecatWorker`` to record in ``skipped``.
    """
    from recipeparser.core.regen import raw_body_column  # noqa: PLC0415

    axes_text = "\n".join(f"- {axis}: {', '.join(tags)}" for axis, tags in new_axes.items())
    recipes_text = "\n\n".join(
        f"RECIPE ID: {r['id']}\nTITLE: {r.get('title', '')}\n"
        "INGREDIENTS:\n" + "\n".join(f"  - {line}" for line in raw_body_column(r, "ingredient_lines")) + "\n"
        "DIRECTIONS:\n" + "\n".join(f"  {i + 1}. {s}" for i, s in enumerate(raw_body_column(r, "direction_steps")))
        for r in recipes
    )
    return f"""
You are a culinary classifier. The user has just added these tags to their taxonomy:
{axes_text}

For EACH recipe below, list which of the tags above apply. Rules:
- Use ONLY tags from the list above, spelled exactly. Never invent a tag.
- Return an empty list when none apply. Most recipes will match nothing.
- Return one result per recipe id, in any order.

{recipes_text}
"""


def categorize_batch(
    recipes: List[Dict[str, Any]],
    new_axes: Dict[str, List[str]],
    client,
) -> Dict[str, List[str]]:
    """
    Categorise several existing recipes against ONLY the newly added tags
    (spec 6.2).  Returns recipe_id -> tags.  Never used at ingest; REFINE does
    that.

    A well-formed reply with no matches is a normal, successful result: the
    model is told most recipes will match nothing, so ``{}`` and empty tag lists
    are expected.

    Raises:
        Exception: whatever the Gemini call raises, and ValueError when the
            reply is empty or unparseable.  Failures MUST propagate.
            ``RecatWorker`` isolates every batch in its own try/except, counts
            the failure, and errors the job when more than 10% of batches fail
            (spec 6.4).  Swallowing them here and returning {} made that
            threshold dead code: a completely broken Gemini produced a job that
            finished status='done', progress_pct=100, recipe_count=0, telling
            the user their library had been recategorised when nothing was
            examined.
    """
    response = _call_with_retry(
        client,
        model=GEMINI_MODEL,
        contents=build_categorize_batch_prompt(recipes, new_axes),
        config={
            "response_mime_type": "application/json",
            "response_json_schema": _schema_for_gemini(_BatchCategorization),
            "temperature": 0.0,
        },
        what="categorize_batch",
    )
    if not response.text or not response.text.strip():
        raise ValueError("categorize_batch: Gemini returned an empty response.")
    parsed = _BatchCategorization.model_validate(json.loads(response.text))
    return {r.recipe_id: list(r.tags) for r in parsed.results}
