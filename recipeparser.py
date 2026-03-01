import os
import re
import json
import gzip
import zipfile
import base64
import uuid
import shutil
import logging
import argparse
from typing import List, Optional

import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field
from google import genai
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

load_dotenv()
client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# Gemini token limit guard: keep chunks under this character count.
# gemini-2.5-flash has a large context window, but very long single chapters
# can still cause issues or inflate latency. ~30k chars ≈ ~7-8k tokens.
MAX_CHUNK_CHARS = 30_000


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class RecipeExtraction(BaseModel):
    name: str = Field(description="The name or title of the recipe.")
    photo_filename: Optional[str] = Field(
        default=None,
        description="The filename from the [IMAGE: filename.jpg] marker nearest to this recipe. Leave blank if none.",
    )
    servings: Optional[str] = Field(default=None, description="Number of servings (e.g., '4 servings', '2-4').")
    prep_time: Optional[str] = Field(default=None, description="Preparation time (e.g., '15 mins').")
    cook_time: Optional[str] = Field(default=None, description="Cook time (e.g., '30 mins').")
    ingredients: List[str] = Field(description="List of ingredients. Convert unicode fractions to text fractions (e.g. ½ -> 1/2).")
    directions: List[str] = Field(description="List of step-by-step cooking instructions.")
    notes: Optional[str] = Field(default=None, description="Any additional notes or headnotes from the author.")


class RecipeList(BaseModel):
    recipes: List[RecipeExtraction] = Field(description="A list of all distinct recipes found in the text chunk.")


# ---------------------------------------------------------------------------
# Paprika category taxonomy
# ---------------------------------------------------------------------------

# Flat list mirroring the user's existing Paprika category tree.
# Sub-categories are included as standalone strings — Paprika matches them by
# name regardless of nesting, so "Cake" and "Dessert" both work independently.
PAPRIKA_CATEGORIES: List[str] = [
    "Appetizers",
    "Baking Basics",
    "Barbeque",
    "Beans",
    "Bread And Buns",
    "Breakfast",
    "camping",
    "Deep Fried",
    "Dessert",
    "Dessert/Almond",
    "Dessert/Bars",
    "Dessert/Cake",
    "Dessert/Chocolate",
    "Dessert/Cookies",
    "Dessert/Fruit",
    "Dessert/Gluten free",
    "Dessert/Pie",
    "Dessert/Pistachio",
    "Dessert/Pudding",
    "Dessert/Quick Bread",
    "Dessert/Summer Fruit",
    "Dessert/Sweets for Wedding",
    "Dips",
    "Jam",
    "Mains",
    "Mains/Beef Dishes",
    "Mains/Braises",
    "Mains/Chicken Dishes",
    "Mains/Egg Dishes",
    "Mains/Fish and Seafood",
    "Mains/Grilled",
    "Mains/Pasta and Noodles",
    "Mains/Pork Dishes",
    "Mains/Savoury Pies",
    "Mains/Seafood Dishes",
    "Mains/Turkey Dishes",
    "Pantry Items",
    "Pastries",
    "Pizza",
    "Preserving",
    "Pressure Cooker",
    "Regional Cuisine",
    "Regional Cuisine/African",
    "Regional Cuisine/Central European",
    "Regional Cuisine/Chinese",
    "Regional Cuisine/French",
    "Regional Cuisine/Greek",
    "Regional Cuisine/Indian",
    "Regional Cuisine/Italian",
    "Regional Cuisine/Japanese",
    "Regional Cuisine/Korean",
    "Regional Cuisine/Middle Eastern",
    "Regional Cuisine/South And Central American",
    "Regional Cuisine/Spain",
    "Regional Cuisine/Thai",
    "Regional Cuisine/Vietnamese",
    "Salads",
    "Sandwiches and Filled Buns",
    "Simple Dinner",
    "Slow Cooker",
    "Soup",
    "Sous Vide",
    "Vegetables",
]


def categorise_recipe(recipe: "RecipeExtraction") -> List[str]:
    """
    Ask Gemini to assign 1–3 categories from PAPRIKA_CATEGORIES that best fit
    this recipe.  Returns a list of matching category strings.  Falls back to
    ["EPUB Imports"] if the API call fails or returns nothing useful.
    """
    category_list = "\n".join(f"- {c}" for c in PAPRIKA_CATEGORIES)
    ingredient_sample = "\n".join(recipe.ingredients[:10])

    prompt = f"""You are a recipe categorisation assistant.

Given the recipe details below, select the 1 to 3 most appropriate categories
from the provided list.  Prefer specific sub-categories (e.g. "Dessert/Cake")
over their parent ("Dessert") when the recipe clearly fits.  Only choose
categories from the list — do not invent new ones.

Return ONLY a JSON array of strings, e.g. ["Pizza", "Baking Basics"]

Available categories:
{category_list}

Recipe name: {recipe.name}
First ingredients: {ingredient_sample}
Notes: {recipe.notes or ""}
"""
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config={"temperature": 0},
        )
        text = response.text.strip()
        # Strip any markdown code fences Gemini might wrap around the JSON
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
        categories = json.loads(text)
        if isinstance(categories, list) and categories:
            # Validate every returned category is actually in our taxonomy
            valid = [c for c in categories if c in PAPRIKA_CATEGORIES]
            if valid:
                return valid
    except Exception as e:
        log.warning("  -> Category assignment failed for '%s': %s", recipe.name, e)

    return ["EPUB Imports"]


# ---------------------------------------------------------------------------
# EPUB extraction
# ---------------------------------------------------------------------------

def extract_all_images(book: epub.EpubBook, output_dir: str) -> str:
    """Write every image item in the EPUB to <output_dir>/images/ and return the path."""
    image_dir = os.path.join(output_dir, "images")
    os.makedirs(image_dir, exist_ok=True)

    for item in book.get_items():
        if item.get_type() == ebooklib.ITEM_IMAGE:
            file_name = os.path.basename(item.file_name)
            file_path = os.path.join(image_dir, file_name)
            with open(file_path, "wb") as f:
                f.write(item.get_content())

    return image_dir


def extract_chapters_with_image_markers(book: epub.EpubBook) -> List[str]:
    """
    Return one text string per EPUB document item, with <img> tags replaced
    by [IMAGE: filename] breadcrumb markers so the LLM can associate images
    with recipes without needing vision input.
    """
    chunks = []

    for item in book.get_items():
        if item.get_type() == ebooklib.ITEM_DOCUMENT:
            soup = BeautifulSoup(item.get_body_content(), "html.parser")

            for img in soup.find_all("img"):
                src = img.get("src", "")
                if src:
                    filename = os.path.basename(src)
                    img.replace_with(f"\n[IMAGE: {filename}]\n")

            text = soup.get_text(separator="\n", strip=True)
            if text.strip():
                chunks.append(text)

    return chunks


def split_large_chunk(text: str, max_chars: int = MAX_CHUNK_CHARS) -> List[str]:
    """
    Split a text chunk that exceeds max_chars at paragraph boundaries so that
    we never send a single oversized request to the LLM.
    """
    if len(text) <= max_chars:
        return [text]

    parts = []
    paragraphs = text.split("\n\n")
    current = []
    current_len = 0

    for para in paragraphs:
        para_len = len(para) + 2  # account for the "\n\n" separator
        if current_len + para_len > max_chars and current:
            parts.append("\n\n".join(current))
            current = [para]
            current_len = para_len
        else:
            current.append(para)
            current_len += para_len

    if current:
        parts.append("\n\n".join(current))

    return parts


def is_recipe_candidate(text: str) -> bool:
    """
    Lightweight heuristic to skip obviously non-recipe content (TOC, copyright
    pages, author bios, etc.) before spending an API call.

    Requires both:
      - at least 2 distinct unit/cooking keywords (quantity signals)
      - at least 1 structural keyword (ingredients/directions heading or method verb)
    """
    text_lower = text.lower()

    quantity_keywords = ["tbsp", "tablespoon", "tsp", "teaspoon", "cup", "ounce", "oz", "gram", "lb", "pound", "ml", "litre", "liter"]
    structure_keywords = ["ingredients", "directions", "instructions", "method", "preheat", "bake", "simmer", "sauté", "saute", "stir", "whisk", "fold", "roast", "boil"]

    quantity_hits = sum(1 for w in quantity_keywords if w in text_lower)
    structure_hits = sum(1 for w in structure_keywords if w in text_lower)

    return quantity_hits >= 2 and structure_hits >= 1


# ---------------------------------------------------------------------------
# Gemini integration
# ---------------------------------------------------------------------------

def verify_gemini_connectivity() -> bool:
    """
    Send a minimal single-token request to confirm the API key is valid and
    the Generative Language API is enabled before processing any real content.
    Returns True if the API is reachable, False otherwise.
    """
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents="Reply with the single word OK.",
            config={"max_output_tokens": 5, "temperature": 0},
        )
        log.info("Gemini connectivity check passed (response: %s).", response.text.strip())
        return True
    except Exception as e:
        log.error("Gemini connectivity check FAILED: %s", e)
        return False


def needs_table_normalisation(text: str) -> bool:
    """
    Detect multi-column baker's percentage ingredient tables that confuse the
    LLM extractor.  These are exclusive to professional baking books and are
    reliably identified by the presence of a baker's percentage column header.

    The apostrophe in "Baker's" is sometimes mangled into a Unicode replacement
    character (U+FFFD) during EPUB HTML stripping, so we match it loosely with
    a regex that accepts any character in that position.
    """
    upper = text.upper()
    return bool(
        re.search(r"BAKER.S %", upper)
        or re.search(r"BAKER.S PERCENTAGE", upper)
    )


def normalise_baker_table(text_chunk: str) -> str:
    """
    Pre-process a chunk that contains multi-column baker's percentage ingredient
    tables by asking Gemini to reformat them into readable ingredient lines
    before the main extraction pass runs.

    Input example (as stripped HTML):
        Water
        350g
        1½ cups
        70%
        Fine sea salt
        15g
        2¾ tsp
        3.0%

    Expected output:
        Water: 350g (1½ cups) — 70%
        Fine sea salt: 15g (2¾ tsp) — 3.0%

    Returns the reformatted text, or the original text unchanged if the call fails.
    """
    prompt = f"""The following text is from a recipe book and contains one or more ingredient tables
where each ingredient name, its weight, its volume measure, and its baker's percentage
appear on separate lines rather than in columns.

Reformat ONLY the ingredient table sections so that each ingredient appears on a single
line in the format: "IngredientName: weight (volume) — baker's%"

Do NOT change any recipe titles, headings, method steps, notes, or any other text.
Do NOT add or remove any ingredients or values.
Preserve all [IMAGE: ...] markers exactly as they appear.

Text:
{text_chunk}"""

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config={"temperature": 0},
        )
        normalised = response.text.strip()
        if normalised:
            log.info("  -> Table normalisation applied (%d -> %d chars).",
                     len(text_chunk), len(normalised))
            return normalised
        log.warning("  -> Table normalisation returned empty response; using original text.")
        return text_chunk
    except Exception as e:
        log.warning("  -> Table normalisation failed (%s); using original text.", e)
        return text_chunk


def extract_recipes_with_gemini(text_chunk: str) -> Optional[RecipeList]:
    prompt = f"""
You are a culinary data extractor. Review the following text from an EPUB recipe book.
Extract ALL distinct recipes found in the text.

Rules:
- If you see an [IMAGE: filename.jpg] marker, assign that filename to the photo_filename
  field of the recipe it most directly precedes or follows. Only assign one image per recipe.
- Convert all unicode fractions (½, ¼, ¾, etc.) to plain text (1/2, 1/4, 3/4, etc.).
- If a field is entirely absent from the text, leave it null.
- Do not invent or infer values that are not present in the text.

Text chunk:
{text_chunk}
"""

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config={
                "response_mime_type": "application/json",
                "response_schema": RecipeList,
                "temperature": 0.1,
            },
        )
        return response.parsed
    except Exception as e:
        log.error("Gemini extraction failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def deduplicate_recipes(recipes: List[RecipeExtraction]) -> List[RecipeExtraction]:
    """
    Remove duplicate recipes based on a normalised version of the name.
    Keeps the first occurrence encountered (preserves chapter order).
    """
    seen: set[str] = set()
    unique: List[RecipeExtraction] = []

    for recipe in recipes:
        key = recipe.name.lower().strip()
        if key not in seen:
            seen.add(key)
            unique.append(recipe)
        else:
            log.info("Duplicate recipe skipped: '%s'", recipe.name)

    return unique


# ---------------------------------------------------------------------------
# Paprika bundler
# ---------------------------------------------------------------------------

def create_paprika_export(
    recipes: List[RecipeExtraction],
    output_dir: str,
    image_dir: str,
    export_filename: str,
) -> bool:
    """
    Bundle recipes into a .paprikarecipes archive (ZIP of gzipped JSON files).
    Returns True on success, False if nothing was written.
    """
    if not recipes:
        log.warning("No recipes to export — skipping bundle creation.")
        return False

    export_path = os.path.join(output_dir, export_filename)
    log.info("Bundling %d recipe(s) into %s ...", len(recipes), export_filename)

    with zipfile.ZipFile(export_path, "w", zipfile.ZIP_DEFLATED) as zip_archive:
        for recipe in recipes:
            recipe_uid = str(uuid.uuid4()).upper()
            photo_data = ""
            photo_name = ""

            if recipe.photo_filename:
                img_path = os.path.join(image_dir, recipe.photo_filename)
                if os.path.exists(img_path):
                    with open(img_path, "rb") as img_file:
                        photo_data = base64.b64encode(img_file.read()).decode("utf-8")
                    photo_name = recipe.photo_filename
                else:
                    log.warning(
                        "Image '%s' referenced by recipe '%s' not found — skipping photo.",
                        recipe.photo_filename,
                        recipe.name,
                    )

            paprika_dict = {
                "uid": recipe_uid,
                "name": recipe.name,
                "directions": "\n".join(recipe.directions),
                "ingredients": "\n".join(recipe.ingredients),
                "prep_time": recipe.prep_time or "",
                "cook_time": recipe.cook_time or "",
                "servings": recipe.servings or "",
                "notes": recipe.notes or "",
                "description": "",
                "nutritional_info": "",
                "difficulty": "",
                "rating": 0,
                "source": "EPUB Auto-Import",
                "categories": getattr(recipe, "_categories", ["EPUB Imports"]),
            }

            if photo_name and photo_data:
                paprika_dict["photo"] = photo_name
                paprika_dict["photo_data"] = photo_data

            json_str = json.dumps(paprika_dict, ensure_ascii=False)
            gzipped_content = gzip.compress(json_str.encode("utf-8"))

            safe_title = "".join(c for c in recipe.name if c.isalnum() or c in " -_").strip()
            if not safe_title:
                safe_title = "Untitled_Recipe"
            internal_filename = f"{safe_title}.paprikarecipe"

            zip_archive.writestr(internal_filename, gzipped_content)

    log.info("Export created: %s", export_path)
    return True


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def process_epub(epub_path: str, output_dir: str):
    os.makedirs(output_dir, exist_ok=True)

    log.info("Verifying Gemini API connectivity...")
    if not verify_gemini_connectivity():
        log.error("Aborting — fix the API key or enable the Generative Language API and retry.")
        return

    log.info("Opening EPUB: %s", epub_path)

    try:
        book = epub.read_epub(epub_path)
    except Exception as e:
        log.error("Failed to open EPUB: %s", e)
        return

    log.info("Extracting images to disk...")
    image_dir = extract_all_images(book, output_dir)

    log.info("Extracting text with image breadcrumbs...")
    raw_chunks = extract_chapters_with_image_markers(book)

    # Split any oversized chapters before sending to the LLM
    chunks: List[str] = []
    for raw in raw_chunks:
        chunks.extend(split_large_chunk(raw))

    log.info("Total text segments to evaluate: %d", len(chunks))

    all_recipes: List[RecipeExtraction] = []

    for i, chunk in enumerate(chunks):
        if not is_recipe_candidate(chunk):
            log.debug("Segment %d skipped (no recipe signals).", i)
            continue

        log.info("Analysing segment %d / %d ...", i + 1, len(chunks))

        if needs_table_normalisation(chunk):
            log.info("  -> Baker's percentage table detected — normalising before extraction...")
            chunk = normalise_baker_table(chunk)

        result = extract_recipes_with_gemini(chunk)

        if result and result.recipes:
            log.info("  -> %d recipe(s) found.", len(result.recipes))
            all_recipes.extend(result.recipes)
        else:
            log.info("  -> No recipes extracted.")

    log.info("Total recipes before deduplication: %d", len(all_recipes))
    all_recipes = deduplicate_recipes(all_recipes)
    log.info("Total recipes after deduplication:  %d", len(all_recipes))

    # Categorise each recipe against the Paprika taxonomy
    log.info("Categorising %d recipe(s)...", len(all_recipes))
    for recipe in all_recipes:
        cats = categorise_recipe(recipe)
        recipe._categories = cats
        log.info("  %-40s -> %s", recipe.name[:40], cats)

    # Derive export filename from the EPUB filename
    epub_stem = os.path.splitext(os.path.basename(epub_path))[0]
    export_filename = f"{epub_stem}.paprikarecipes"

    success = create_paprika_export(all_recipes, output_dir, image_dir, export_filename)

    # Only clean up temporary images if the export succeeded
    if success and os.path.exists(image_dir):
        log.info("Cleaning up temporary image directory...")
        shutil.rmtree(image_dir)
        log.info("Cleanup complete.")
    elif not success:
        log.warning("Export failed or empty — keeping image directory for inspection: %s", image_dir)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Extract recipes from an EPUB cookbook and export to Paprika 3."
    )
    parser.add_argument("epub", help="Path to the input .epub file.")
    parser.add_argument(
        "--output",
        default="./output",
        help="Directory to write the .paprikarecipes file (default: ./output).",
    )
    args = parser.parse_args()

    process_epub(args.epub, args.output)
