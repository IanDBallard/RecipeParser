"""Pydantic models for structured Gemini output."""
from typing import Dict, List, Optional
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


# --- Cayenne Specific Models ---

class StructuredIngredient(BaseModel):
    id: str = Field(description="Unique ID for cross-linking, e.g., \"ing_01\"")
    amount: float = Field(description="Numeric quantity, e.g., 1.5. 0 if none.")
    unit: Optional[str] = Field(default=None, description="Unit of measure, e.g., \"cups\".")
    name: str = Field(description="Core name, e.g., \"flour\".")
    fallback_string: str = Field(description="Full original string, e.g., \"1 1/2 cups flour\".")
    converted_amount: Optional[float] = Field(default=None, description="Converted amount (e.g. Volume -> Weight)")
    converted_unit: Optional[str] = Field(default=None, description="Converted unit, e.g., \"g\"")
    is_ai_converted: bool = Field(default=False, description="True if AI calculated the conversion.")


class TokenizedDirection(BaseModel):
    step: int = Field(description="1-based step number.")
    text: str = Field(description="Direction text with Fat Tokens {{id|fallback}}.")


class CayenneRefinement(BaseModel):
    """Internal refinement pass output."""
    title: str
    base_servings: Optional[int]
    structured_ingredients: List[StructuredIngredient]
    tokenized_directions: List[TokenizedDirection]
    grid_categories: Dict[str, List[str]] = Field(
        default_factory=dict,
        description=(
            "Multipolar categorization result. Keys are axis names (e.g. 'Cuisine'), "
            "values are lists of 0-2 matching tags from that axis. "
            "Return [] for any axis that does not apply — never hallucinate tags."
        ),
    )


class CayenneRecipe(BaseModel):
    """Canonical recipe shape for Project Cayenne."""
    title: str
    prep_time: Optional[str] = None
    cook_time: Optional[str] = None
    base_servings: Optional[int] = None
    source_url: Optional[str] = None
    image_url: Optional[str] = None  # Supabase Storage public URL; None when no photo available
    categories: List[str] = Field(
        default_factory=list,
        description=(
            "Flat list of category names for Paprika compatibility. "
            "Derived by flattening grid_categories values. Empty when no axes defined."
        ),
    )
    grid_categories: Dict[str, List[str]] = Field(
        default_factory=dict,
        description=(
            "Multipolar categorization result. Keys are axis names, "
            "values are lists of matching tags. Empty dict when no axes defined."
        ),
    )
    structured_ingredients: List[StructuredIngredient]
    tokenized_directions: List[TokenizedDirection]


class IngestResponse(CayenneRecipe):
    """Final envelope for the /ingest endpoint."""
    embedding: List[float]


class JobResponse(BaseModel):
    """
    Response returned by all /ingest* endpoints.

    ARCHITECTURAL INVARIANT:
      The API writes the recipe directly to Supabase and returns only this
      lightweight acknowledgment. The client app NEVER receives recipe JSON
      in an HTTP response. Recipes reach the client via PowerSync sync.

    recipe_id is present only for single-recipe endpoints (/ingest, /ingest/url).
    Multi-recipe endpoints (/ingest/pdf, /jobs/file) omit it because N recipes
    are written asynchronously — each gets its own UUID internally.
    """
    job_id: str                  # UUID — correlates with the ingestion_jobs row
    recipe_id: Optional[str] = None  # UUID — only set for single-recipe endpoints
