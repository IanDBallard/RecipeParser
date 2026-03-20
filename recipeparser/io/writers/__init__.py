"""
recipeparser/io/writers/__init__.py — RecipeWriter port (interface).

All concrete writers implement this ABC. The adapter layer (CLI, GUI, API)
instantiates the appropriate writer and passes it to the pipeline.

TID rule: this module MUST NOT import from ``recipeparser.adapters``.
Importing from ``recipeparser.core`` or ``recipeparser.models`` is allowed.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from recipeparser.models import IngestResponse


class RecipeWriter(ABC):
    """
    Port for writing completed recipes to a destination.

    Concrete implementations live in ``recipeparser/io/writers/``:
      - ``SupabaseWriter``   — writes to Supabase ``recipes`` + ``recipe_categories``
      - ``PaprikaWriter``    — writes to a ``.paprikarecipes`` ZIP archive
      - ``CayenneZipWriter`` — writes to a ``.cayenne`` ZIP with ``_cayenne_meta``

    The ``write()`` method is the sole public API. All configuration
    (output path, user_id, category_ids, etc.) is passed to ``__init__``.
    """

    @abstractmethod
    def write(self, recipes: List[IngestResponse], **kwargs: object) -> None:
        """
        Write a list of completed recipes to the destination.

        Args:
            recipes: All successfully processed ``IngestResponse`` objects
                     from ``RecipePipeline.run()``.
            **kwargs: Writer-specific keyword arguments (e.g. ``image_dir``
                      for ``PaprikaWriter``).

        Raises:
            RuntimeError: If the write operation fails unrecoverably.
        """
        ...
