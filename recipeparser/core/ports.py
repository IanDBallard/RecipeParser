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
from typing import Dict, List, Optional


class CategorySource(ABC):
    """
    Port for loading a user's multipolar taxonomy axes.

    Concrete implementations live in ``recipeparser/io/category_sources/``:
      - ``YamlCategorySource``     — loads from a local YAML file
      - ``PaprikaCategorySource``  — loads from a local Paprika SQLite DB
      - ``SupabaseCategorySource`` — loads from the Supabase ``categories`` table

    Returns a dict mapping axis name → list of valid tag strings.
    An empty dict means "no categories defined" (or a source that is
    documented as not configured, such as no file path or no credentials in
    local/test mode) — the engine will skip categorization entirely
    (Zero-Tag Mandate: no Uncategorized fallback).

    A source that cannot load the categories — a failed request, an
    unreadable or malformed file, a database error — raises
    ``recipeparser.exceptions.CategoryLoadError``. It never returns empty for
    that: an import that runs untagged and finishes as a success hides the
    failure from the cook.

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

    def load_parents(self, user_id: str = "") -> Dict[str, str]:
        """
        Load ``{tag name: parent tag name}`` for tags nested below another tag.

        ``load_axes`` flattens each axis, so this is the only place the tree's
        shape survives: the prompts show a nested tag under its parent, and
        CATEGORIZE keeps only the most specific of a tag and its ancestors.
        A source with a flat taxonomy keeps this default.

        Args:
            user_id: The authenticated user's UUID.

        Returns:
            Dict mapping a nested tag's name to its parent's name; ``{}`` when
            no tag is nested below another.
        """
        return {}


class ImageStore(ABC):
    """Somewhere a recipe's hero image can be put, addressed by a public URL.

    A port so the pipeline can store the photograph a reader pulled out of an
    archive without importing recipeparser.io — the same reason CategorySource
    exists.  The Supabase implementation lives in recipeparser.io.writers.
    """

    @abstractmethod
    def put(self, image_bytes: bytes, recipe_id: str, content_type: str = "image/jpeg") -> Optional[str]:
        """Store *image_bytes* and return its public URL.

        Returns None only when there is nothing to store or no store is
        configured (empty bytes, no credentials in local or test mode).

        Raises ``recipeparser.exceptions.ImageStoreError`` when storing fails,
        so a storage outage is told apart from "no picture". A photograph that
        cannot be stored is still a recipe without a picture, never a recipe
        that fails to import: the caller catches the error, keeps the recipe
        and reports the lost picture (the pipeline through ``on_picture_lost``).
        """
