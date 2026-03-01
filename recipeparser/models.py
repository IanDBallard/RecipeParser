"""Pydantic models for structured Gemini output."""
from typing import List, Optional
from pydantic import BaseModel, Field


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
    notes: Optional[str] = Field(
        default=None,
        description="Any additional notes or headnotes from the author.",
    )


class RecipeList(BaseModel):
    recipes: List[RecipeExtraction] = Field(
        description="A list of all distinct recipes found in the text chunk."
    )
