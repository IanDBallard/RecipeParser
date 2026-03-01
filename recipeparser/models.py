"""Pydantic models for structured Gemini output."""
from typing import List, Optional
from pydantic import BaseModel, Field


class RecipeExtraction(BaseModel):
    name: str = Field(description="The name or title of the recipe.")
    photo_filename: Optional[str] = Field(
        default=None,
        description=(
            "The filename from the single [IMAGE: filename.jpg] marker that is most likely "
            "the hero/finished-dish photo for this recipe. "
            "Prefer the image that appears just before the ingredient list or recipe title — "
            "this is usually the plated dish. "
            "Ignore images embedded inside the method steps — those are instructional process shots. "
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
