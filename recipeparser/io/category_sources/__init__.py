"""Category source implementations for multipolar taxonomy injection."""
from recipeparser.core.ports import CategorySource
from recipeparser.io.category_sources.paprika_db import PaprikaCategorySource
from recipeparser.io.category_sources.supabase_source import SupabaseCategorySource
from recipeparser.io.category_sources.yaml_source import YamlCategorySource

__all__ = [
    "CategorySource",
    "YamlCategorySource",
    "PaprikaCategorySource",
    "SupabaseCategorySource",
]
