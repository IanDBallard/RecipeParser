# recipeparser/epub.py  ← SHIM — will be deleted in Phase 7
# Backward-compat re-export. Import from recipeparser.io.readers.epub directly.
from recipeparser.io.readers.epub import (  # noqa: F401
    extract_all_images,
    extract_chapters_with_image_markers,
    extract_text_from_epub,
    get_book_source,
    is_recipe_candidate,
    load_epub,
    split_large_chunk,
)
