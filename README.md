# RecipeParser

Extracts recipes from EPUB cookbooks and exports them as a `.paprikarecipes` archive ready to import into [Paprika 3](https://www.paprikaapp.com/).

## How it works

1. Opens the EPUB and extracts all embedded images to a temporary directory.
2. Parses each chapter, replacing `<img>` tags with `[IMAGE: filename]` breadcrumb markers so the AI can associate photos with recipes.
3. Filters out non-recipe content (table of contents, author bios, etc.) with a lightweight heuristic before sending anything to the API.
4. Sends each candidate chapter to **Gemini** (structured JSON output via a Pydantic schema) to extract recipe fields.
5. Deduplicates recipes by name across chapters.
6. Bundles everything into a `.paprikarecipes` file (ZIP of gzipped JSON, one file per recipe) with base64-encoded photos embedded.
7. Cleans up the temporary image directory — only after a successful export.

## Setup

```bash
pip install -r requirements.txt
```

Create a `.env` file in the project root:

```
GOOGLE_API_KEY=your_api_key_here
```

## Usage

```bash
python recipeparser.py path/to/cookbook.epub
```

The `.paprikarecipes` file is written to `./output/` by default. Use `--output` to change the destination:

```bash
python recipeparser.py path/to/cookbook.epub --output ./my_exports
```

Then in Paprika 3: **File → Import Recipes** and select the `.paprikarecipes` file.

## Notes

- Large chapters are automatically split at paragraph boundaries to stay within safe API token limits.
- If a referenced image file is missing from the EPUB, the recipe is still exported without a photo and a warning is logged.
- The temporary `images/` folder is preserved if the export fails, so you can inspect it without re-running the extraction.
