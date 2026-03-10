from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, List
import os
from dotenv import load_dotenv

from recipeparser.models import CayenneRecipe, IngestResponse

load_dotenv()

app = FastAPI(title='Cayenne Ingestion API')


class IngestRequest(BaseModel):
    url: Optional[str] = None
    text: Optional[str] = None
    uom_system: Optional[str] = 'US'
    measure_preference: Optional[str] = 'Volume'


def _get_client():
    """Lazily initialise the Gemini client so tests can import the module
    without a real API key present at import time."""
    from google import genai
    api_key = os.getenv('GOOGLE_API_KEY')
    if not api_key:
        raise RuntimeError('GOOGLE_API_KEY not found in environment.')
    return genai.Client(api_key=api_key)


@app.post('/ingest', response_model=IngestResponse)
async def ingest_recipe(request: IngestRequest):
    from recipeparser.gemini import extract_recipes, refine_recipe_for_cayenne, get_embeddings

    source_text = request.text
    if not source_text or not source_text.strip():
        # TODO: Add Jina scraper here for URL ingestion
        raise HTTPException(
            status_code=400,
            detail='Only text ingestion is supported currently. URL ingestion coming soon.'
        )

    try:
        client = _get_client()
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    try:
        # Step 1: Raw Extraction
        recipe_list = extract_recipes(source_text, client)
        if not recipe_list or not recipe_list.recipes:
            raise HTTPException(status_code=422, detail='No recipes found in source.')

        raw_recipe = recipe_list.recipes[0]

        # Step 2: Cayenne Refinement (Fat Tokens + unit conversions)
        refined = refine_recipe_for_cayenne(
            raw_recipe,
            client,
            uom_system=request.uom_system or 'US',
            measure_preference=request.measure_preference or 'Volume',
        )

        if not refined:
            raise HTTPException(status_code=500, detail='Refinement pass failed.')

        # Step 3: Vectorisation
        ing_names = ', '.join([i.name for i in refined.structured_ingredients])
        embedding_input = f'{refined.title}\n\n{ing_names}'
        embedding = get_embeddings(embedding_input, client)

        # Reassemble into canonical CayenneRecipe
        cayenne_recipe = CayenneRecipe(
            title=refined.title,
            prep_time=raw_recipe.prep_time,
            cook_time=raw_recipe.cook_time,
            base_servings=refined.base_servings or 4,
            source_url=request.url,
            categories=['Uncategorized'],
            structured_ingredients=refined.structured_ingredients,
            tokenized_directions=refined.tokenized_directions,
        )

        return IngestResponse(
            **cayenne_recipe.model_dump(),
            embedding=embedding,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=8000)
