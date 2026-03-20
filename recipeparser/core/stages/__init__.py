"""
recipeparser/core/stages — Pure stage functions for the Cayenne pipeline.

Each module wraps one logical step of the pipeline.  All functions are pure
with respect to I/O: they accept a Gemini client object but make no filesystem
or network calls beyond the Gemini API itself.

No imports from recipeparser.io or recipeparser.adapters are permitted here
(enforced by ruff TID rules).
"""
from recipeparser.core.stages.assemble import assemble
from recipeparser.core.stages.categorize import categorize
from recipeparser.core.stages.embed import embed
from recipeparser.core.stages.extract import extract
from recipeparser.core.stages.refine import refine

__all__ = ["extract", "refine", "categorize", "embed", "assemble"]
