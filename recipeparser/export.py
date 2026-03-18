# recipeparser/export.py  ← SHIM — will be deleted in Phase 7
from recipeparser.io.writers.paprika_zip import (  # noqa: F401
    create_paprika_export,
    merge_exports,
    _normalise_recipe_name,
)
