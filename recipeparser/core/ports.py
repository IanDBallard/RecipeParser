"""
recipeparser/core/ports.py — Port (interface) definitions for the core layer.

In Hexagonal (Ports & Adapters) architecture, Ports are the interfaces that
the core layer depends on.  Concrete Adapters (in ``io/``) implement these
interfaces and are injected by the adapter layer (CLI, GUI, API) at startup.

This module lives in ``core/`` so that ``core/pipeline.py`` can depend on the
``CategorySource`` port without importing from ``io/`` — preserving the
hexagonal boundary enforced by the TID ruff rule.

TID rule: this module MUST NOT import from ``recipeparser.io`` or
``recipeparser.adapters``.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List


class CategorySource(ABC):
    """
    Port for loading a user's multipolar taxonomy axes.

    Concrete implementations live in ``recipeparser/io/category_sources/``:
      - ``YamlCategorySource``     — loads from a local YAML file
      - ``PaprikaCategorySource``  — loads from a local Paprika SQLite DB
      - ``SupabaseCategorySource`` — loads from the Supabase ``categories`` table

    Returns a dict mapping axis name → list of valid tag strings.
    An empty dict means "no categories defined" — the engine will skip
    categorization entirely (Zero-Tag Mandate: no Uncategorized fallback).

    Example return value::

        {
            "Cuisine": ["Italian", "Mexican", "Japanese", "French"],
            "Protein": ["Chicken", "Beef", "Pork", "Vegetarian", "Seafood"],
            "Meal Type": ["Breakfast", "Lunch", "Dinner", "Snack", "Dessert"],
        }
    """

    @abstractmethod
    def load_axes(self, user_id: str = "") -> Dict[str, List[str]]:
        """
        Load the user's taxonomy axes.

        Args:
            user_id: The authenticated user's UUID.  Used by the Supabase
                     source to scope the query.  May be ignored by file-based
                     sources.

        Returns:
            Dict mapping axis name → list of valid tag strings.
            Returns ``{}`` if no axes are defined for this user.
        """
        ...

    @abstractmethod
    def load_category_ids(self, user_id: str = "") -> Dict[str, str]:
        """
        Load a mapping of category name → category UUID for junction table writes.

        The Supabase writer needs the UUID of each category row to insert into
        the ``recipe_categories`` junction table.  File-based sources return
        ``{}`` since they don't write to Supabase.

        Args:
            user_id: The authenticated user's UUID.

        Returns:
            Dict mapping category name (tag) → UUID string.
            Returns ``{}`` for sources that don't support Supabase writes.
        """
        ...
