# recipeparser/supabase_writer.py  ← SHIM — will be deleted in Phase 7
from recipeparser.io.writers.supabase import (  # noqa: F401
    write_recipe_to_supabase,
    delete_recipe_from_supabase,
    verify_recipe_in_supabase,
)
