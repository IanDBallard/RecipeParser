"""
recipeparser/io/category_sources/base.py — backward-compatibility re-export.

The ``CategorySource`` ABC has been moved to ``recipeparser.core.ports`` so
that ``core/pipeline.py`` can depend on it without violating the hexagonal
architecture boundary (TID rule: core/ must not import from io/).

This module re-exports ``CategorySource`` from its new canonical location so
that existing concrete implementations (yaml_source, paprika_db,
supabase_source) continue to work without modification.

Concrete implementations in this package should update their imports to:
    from recipeparser.core.ports import CategorySource
at their next scheduled edit.  This shim will be removed in Phase 8.
"""
from recipeparser.core.ports import CategorySource  # noqa: F401 — re-export

__all__ = ["CategorySource"]
