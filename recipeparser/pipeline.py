"""
Top-level orchestration: open EPUB → extract → categorise → export.

Segment extraction runs in parallel using a ThreadPoolExecutor capped by a
semaphore so we never exceed Gemini's concurrent-call limit.  Each future
is given an individual timeout; timed-out or failed segments are logged and
skipped rather than aborting the whole run.
"""
import logging
import os
import re
import shutil
import threading
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

from ebooklib import epub

from recipeparser import categories as cat_module
from recipeparser import gemini as gem
from recipeparser.epub import (
    extract_all_images,
    extract_chapters_with_image_markers,
    get_book_source,
    is_recipe_candidate,
    split_large_chunk,
)
from recipeparser.export import create_paprika_export
from recipeparser.models import RecipeExtraction

log = logging.getLogger(__name__)

# Maximum Gemini API calls in-flight at once.  Gemini free-tier allows ~5;
# increase for paid tiers.
MAX_CONCURRENT_API_CALLS = 5

# Seconds to wait for a single segment future before abandoning it.
SEGMENT_TIMEOUT_SECS = 300


def _process_segment(
    index: int,
    chunk: str,
    client,
    semaphore: threading.Semaphore,
) -> Tuple[int, List[RecipeExtraction]]:
    """
    Worker executed in a thread pool.  Acquires the semaphore before touching
    the Gemini API so concurrent in-flight calls stay within the cap.

    Returns (original_index, recipes) so the caller can restore chapter order.
    """
    with semaphore:
        if gem.needs_table_normalisation(chunk):
            log.info(
                "  Segment %d: Baker's %% table detected — normalising...", index
            )
            chunk = gem.normalise_baker_table(chunk, client)

        result = gem.extract_recipes(chunk, client)

    if result and result.recipes:
        log.info("  Segment %d: %d recipe(s) found.", index, len(result.recipes))
        return index, list(result.recipes)

    log.info("  Segment %d: no recipes extracted.", index)
    return index, []


def deduplicate_recipes(recipes: List[RecipeExtraction]) -> List[RecipeExtraction]:
    """
    Remove duplicate recipes based on a normalised version of the name.
    Keeps the first occurrence (preserves chapter order).
    """
    seen: set = set()
    unique: List[RecipeExtraction] = []

    for recipe in recipes:
        key = recipe.name.lower().strip()
        if key not in seen:
            seen.add(key)
            unique.append(recipe)
        else:
            log.info("Duplicate recipe skipped: '%s'", recipe.name)

    return unique


def process_epub(epub_path: str, output_dir: str, client) -> Optional[str]:
    """
    Full pipeline: open EPUB → extract images + text → parallel Gemini calls
    → deduplicate → categorise → export to .paprikarecipes.

    Returns the path to the exported archive on success, None on failure.
    """
    os.makedirs(output_dir, exist_ok=True)

    log.info("Verifying Gemini API connectivity...")
    if not gem.verify_connectivity(client):
        log.error(
            "Aborting — fix the API key or enable the Generative Language API and retry."
        )
        return None

    log.info("Opening EPUB: %s", epub_path)
    try:
        book = epub.read_epub(epub_path)
    except Exception as e:
        log.error("Failed to open EPUB: %s", e)
        return None

    book_source = get_book_source(book)
    log.info("Book source: %s", book_source)

    log.info("Extracting images to disk...")
    image_dir = extract_all_images(book, output_dir)

    log.info("Extracting text with image breadcrumbs...")
    raw_chunks = extract_chapters_with_image_markers(book)

    chunks: List[str] = []
    for raw in raw_chunks:
        chunks.extend(split_large_chunk(raw))

    # --- Hero-image look-ahead injection -------------------------------------
    # Some books (e.g. Paul Hollywood) place the hero photo on a standalone
    # page immediately BEFORE the recipe text.  That page is a tiny chunk
    # (just "[IMAGE: xxxxx.jpg]\nRecipe Name >") that fails is_recipe_candidate
    # and is therefore never sent to Gemini.  When Gemini processes the actual
    # recipe chunk it never sees the hero image marker.
    #
    # Fix: scan every non-recipe chunk for a lone [IMAGE:] marker and prepend
    # it to the very next chunk as a "[HERO IMAGE: ...]" breadcrumb.  Gemini's
    # prompt already says to prefer the image before the title/ingredients, so
    # the renamed breadcrumb acts as a strong signal.
    _IMAGE_ONLY_RE = re.compile(r"^\s*\[IMAGE:\s*([^\]]+)\]\s*", re.MULTILINE)

    enriched: List[str] = list(chunks)
    for i, chunk in enumerate(chunks):
        if not is_recipe_candidate(chunk):
            markers = _IMAGE_ONLY_RE.findall(chunk)
            # Only inject when the chunk is essentially just an image + a title stub
            non_img_text = _IMAGE_ONLY_RE.sub("", chunk).strip()
            if markers and len(non_img_text) < 120 and i + 1 < len(enriched):
                hero_marker = f"[HERO IMAGE: {markers[-1].strip()}]\n"
                enriched[i + 1] = hero_marker + enriched[i + 1]
                log.debug(
                    "Injected hero image marker '%s' from chunk %d into chunk %d.",
                    markers[-1].strip(), i, i + 1,
                )

    candidate_chunks = [
        (i, chunk) for i, chunk in enumerate(enriched) if is_recipe_candidate(chunk)
    ]
    log.info(
        "Total segments: %d  |  Recipe candidates: %d",
        len(enriched),
        len(candidate_chunks),
    )

    # Shared semaphore caps total concurrent Gemini calls across both phases.
    semaphore = threading.Semaphore(MAX_CONCURRENT_API_CALLS)

    # --- Parallel extraction ---------------------------------------------------
    results: Dict[int, List[RecipeExtraction]] = {}

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_API_CALLS) as executor:
        future_to_index: Dict[Future, int] = {
            executor.submit(_process_segment, idx, chunk, client, semaphore): idx
            for idx, chunk in candidate_chunks
        }

        log.info(
            "Submitted %d segment(s) to thread pool (concurrency cap: %d).",
            len(future_to_index),
            MAX_CONCURRENT_API_CALLS,
        )

        for future in as_completed(future_to_index):
            seg_idx = future_to_index[future]
            try:
                idx, recipes = future.result(timeout=SEGMENT_TIMEOUT_SECS)
                results[idx] = recipes
            except TimeoutError:
                log.warning(
                    "Segment %d timed out after %ds — skipping.",
                    seg_idx,
                    SEGMENT_TIMEOUT_SECS,
                )
                results[seg_idx] = []
            except Exception as exc:
                log.error("Segment %d raised an error: %s — skipping.", seg_idx, exc)
                results[seg_idx] = []

    # Reassemble in chapter order
    all_recipes: List[RecipeExtraction] = []
    for idx in sorted(results):
        all_recipes.extend(results[idx])

    log.info("Total recipes before deduplication: %d", len(all_recipes))
    all_recipes = deduplicate_recipes(all_recipes)
    log.info("Total recipes after deduplication:  %d", len(all_recipes))

    # --- Categorisation (parallel) --------------------------------------------
    category_tree = cat_module.load_category_tree()
    paprika_cats = cat_module.build_paprika_categories(category_tree)

    log.info("Categorising %d recipe(s) in parallel...", len(all_recipes))

    def _categorise_one(
        recipe: RecipeExtraction,
    ) -> Tuple[RecipeExtraction, List[str]]:
        with semaphore:
            cats = cat_module.categorise_recipe(
                recipe, category_tree, paprika_cats, client
            )
        return recipe, cats

    cat_futures: Dict[Future, RecipeExtraction] = {}
    # Reuse a fresh executor — the extraction one has already shut down.
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_API_CALLS) as cat_executor:
        for recipe in all_recipes:
            cat_futures[cat_executor.submit(_categorise_one, recipe)] = recipe

        for future in as_completed(cat_futures):
            try:
                recipe, cats = future.result(timeout=SEGMENT_TIMEOUT_SECS)
                recipe._categories = cats  # type: ignore[attr-defined]
                log.info("  %-40s -> %s", recipe.name[:40], cats)
            except TimeoutError:
                r = cat_futures[future]
                log.warning("  Categorisation timed out for '%s' — using fallback.", r.name)
                r._categories = ["EPUB Imports"]  # type: ignore[attr-defined]
            except Exception as exc:
                r = cat_futures[future]
                log.error("  Categorisation error for '%s': %s — using fallback.", r.name, exc)
                r._categories = ["EPUB Imports"]  # type: ignore[attr-defined]

    # --- Photo assignment summary --------------------------------------------
    with_photo = sum(1 for r in all_recipes if r.photo_filename)
    log.info(
        "Photo assignment: %d / %d recipes have a photo filename assigned.",
        with_photo,
        len(all_recipes),
    )
    if with_photo < len(all_recipes):
        missing = [r.name for r in all_recipes if not r.photo_filename]
        log.info("  No photo assigned for: %s", ", ".join(missing[:10]))
        if len(missing) > 10:
            log.info("  ... and %d more.", len(missing) - 10)

    # --- Export ---------------------------------------------------------------
    epub_stem = os.path.splitext(os.path.basename(epub_path))[0]
    export_filename = f"{epub_stem}.paprikarecipes"

    success = create_paprika_export(
        all_recipes, output_dir, image_dir, export_filename, book_source
    )

    if success and os.path.exists(image_dir):
        log.info("Cleaning up temporary image directory...")
        shutil.rmtree(image_dir)
        log.info("Cleanup complete.")
    elif not success:
        log.warning(
            "Export failed or empty — keeping image directory for inspection: %s",
            image_dir,
        )
        return None

    return os.path.join(output_dir, export_filename)
