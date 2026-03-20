"""
io/readers/__init__.py — RecipeReader ABC.

All concrete readers implement this interface so the pipeline and adapters
can treat them interchangeably.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from recipeparser.core.models import Chunk


class RecipeReader(ABC):
    """Abstract base class for all recipe source readers."""

    @abstractmethod
    def read(self, source: str) -> List[Chunk]:
        """
        Read the source and return a list of Chunk objects ready for pipeline
        processing.

        Args:
            source: A file-system path, URL, or other reader-specific
                    identifier for the input.

        Returns:
            A non-empty list of Chunk objects.  Each chunk carries the text
            and metadata needed by the pipeline stage router.
        """
