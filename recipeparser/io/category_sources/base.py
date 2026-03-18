"""
Abstract base class for category source implementations.

ARCHITECTURAL INVARIANT:
  CategorySource is a Port (in Ports & Adapters terminology). Concrete
  implementations are Adapters that load user taxonomy from different backends
  (YAML file, Paprika SQLite, Supabase REST API).

  The engine and gemini layers depend ONLY on this ABC — never on concrete
  implementations. Concrete classes are injected by the adapter layer (CLI,
  GUI, API) at startup.
"""
from abc import ABC, abstractmethod
from typing import Dict, List


class CategorySource(ABC):
    """
    Port for loading a user's multipolar taxonomy axes.

    Returns a dict mapping axis name → list of valid tag strings.
    An empty dict means "no categories defined" — the engine will skip
    categorization entirely (Zero-Tag Mandate: no Uncategorized fallback).

    Example return value:
        {
            "Cuisine": ["Italian", "Mexican", "Japanese", "French"],
            "Protein": ["Chicken", "Beef", "Pork", "Vegetarian", "Seafood"],
            "Meal Type": ["Breakfast", "Lunch", "Dinner", "Snack", "Dessert"],
        }
    """

    @abstractmethod
    def load_axes(self, user_id: str) -> Dict[str, List[str]]:
        """
        Load the user's taxonomy axes.

        Args:
            user_id: The authenticated user's UUID. Used by Supabase source
                     to scope the query. May be ignored by file-based sources.

        Returns:
            Dict mapping axis name → list of valid tag strings.
            Returns {} if no axes are defined for this user.
        """
        ...

    @abstractmethod
    def load_category_ids(self, user_id: str) -> Dict[str, str]:
        """
        Load a mapping of category name → category UUID for junction table writes.

        The Supabase writer needs the UUID of each category row to insert into
        the recipe_categories junction table. File-based sources return {} since
        they don't write to Supabase.

        Args:
            user_id: The authenticated user's UUID.

        Returns:
            Dict mapping category name (tag) → UUID string.
            Returns {} for sources that don't support Supabase writes.
        """
        ...
