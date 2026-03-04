"""Pydantic models for structured Gemini output."""
from typing import List, Optional
from pydantic import BaseModel, Field, field_validator


class RecipeExtraction(BaseModel):
    name: str = Field(description="The name or title of the recipe.")
    photo_filename: Optional[str] = Field(
        default=None,
        description=(
            "The filename of the hero/finished-dish photo for this recipe. "
            "If a [HERO IMAGE: filename] marker is present, always use that filename. "
            "Otherwise look for an [IMAGE: filename] marker that appears just before the "
            "recipe title/ingredients, or just after the last ingredient and before the "
            "first method step. "
            "Ignore images embedded within numbered method steps — those are process shots. "
            "If no suitable hero image is present, leave blank."
        ),
    )
    servings: Optional[str] = Field(
        default=None, description="Number of servings (e.g., '4 servings', '2-4')."
    )
    prep_time: Optional[str] = Field(
        default=None, description="Preparation time (e.g., '15 mins')."
    )
    cook_time: Optional[str] = Field(
        default=None, description="Cook time (e.g., '30 mins')."
    )
    ingredients: List[str] = Field(
        description="List of ingredients. Convert unicode fractions to text fractions (e.g. ½ -> 1/2)."
    )
    directions: List[str] = Field(
        description="List of step-by-step cooking instructions."
    )

    @field_validator("ingredients", "directions", mode="before")
    @classmethod
    def _coerce_str_to_list(cls, v):
        """
        Guard against Gemini returning a plain string instead of a JSON array
        for list fields (observed with very large normalised chunks).  Split on
        newlines so each line becomes one list item; filter blank lines.
        """
        if isinstance(v, str):
            return [line for line in v.splitlines() if line.strip()]
        return v

    notes: Optional[str] = Field(
        default=None,
        description="Any additional notes or headnotes from the author.",
    )

    # Populated by the pipeline after extraction — not part of the LLM schema.
    # exclude=True keeps it out of Gemini's response_schema so the model never
    # tries to fill it in.
    categories: List[str] = Field(
        default_factory=lambda: ["EPUB Imports"],
        exclude=True,
        description="Paprika taxonomy categories assigned by the categorisation pass.",
    )


class RecipeList(BaseModel):
    recipes: List[RecipeExtraction] = Field(
        description="A list of all distinct recipes found in the text chunk."
    )


class TocEntry(BaseModel):
    """Single TOC entry: recipe title and optional page/section reference."""

    title: str = Field(description="The recipe or section title.")
    page: Optional[int] = Field(
        default=None,
        description="Page number (1-based) if known; null otherwise.",
    )


class TocList(BaseModel):
    """Parsed table of contents from a cookbook."""

    entries: List[TocEntry] = Field(
        description="Ordered list of TOC entries (recipe titles and optional page numbers)."
    )


class TocRecipeClassification(BaseModel):
    """Result of classifying which TOC entries are recipe titles vs section headers."""

    recipe_indices: List[int] = Field(
        description="0-based indices of TOC entries that are specific recipe/dish names, not section headers."
    )
