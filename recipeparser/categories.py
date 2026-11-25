"""Category taxonomy loading and recipe-categorisation via Gemini."""
import json
import logging
import re
from pathlib import Path
from typing import List, TYPE_CHECKING

import yaml

from recipeparser.config import GEMINI_MODEL
from recipeparser.exceptions import ConfigurationError
from recipeparser.paths import get_categories_file

if TYPE_CHECKING:
    from recipeparser.models import RecipeExtraction

log = logging.getLogger(__name__)

# User-editable path (writable). Initialised with a minimal default on first run.
_CATEGORIES_FILE = get_categories_file()

_DEFAULT_CATEGORIES = {"categories": ["EPUB Imports"]}


def reload_categories(path: Path = _CATEGORIES_FILE):
    """Re-read categories.yaml and return a fresh (tree, paprika_categories) pair.

    Useful when the GUI has saved edits and the next pipeline run should reflect
    them without restarting the process.
    """
    tree = load_category_tree(path)
    return tree, build_paprika_categories(tree)


def _ensure_user_categories_exist(path: Path) -> None:
    """If the default user categories file is missing, create a minimal default."""
    if path != get_categories_file():
        return  # custom path (e.g. import, tests) — do not touch
    if path.exists():
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(
                _DEFAULT_CATEGORIES,
                f,
                allow_unicode=True,
                default_flow_style=False,
                sort_keys=False,
            )
        log.info("Initialized categories with default: %s", path)
    except OSError as e:
        log.warning("Could not create categories file at %s: %s", path, e)


def load_category_tree(path: Path = _CATEGORIES_FILE) -> List[tuple]:
    """
    Parse categories.yaml into a list of (leaf, parent_or_None) tuples.

    YAML format::

        categories:
          - TopLevel
          - Parent:
              - Child1
              - Child2

    On first run, creates a minimal default (EPUB Imports) at the user path if missing.
    A missing file returns an empty list and logs a warning, so the script still
    runs (categories will fall back to "EPUB Imports").

    Raises:
        ConfigurationError: the file exists but cannot be read or is not the
            shape above. An empty tree there would file every recipe under the
            fallback, which reads as a categorisation result, not a broken file.
    """
    _ensure_user_categories_exist(path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        log.warning(
            "categories.yaml not found at %s — no categories will be assigned.", path
        )
        return []
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        raise ConfigurationError(f"Could not read categories file {path}: {e}") from e

    if not isinstance(data, dict):
        raise ConfigurationError(
            f"Categories file {path} must be a mapping with a 'categories' list."
        )
    entries = data.get("categories") or []
    if not isinstance(entries, list):
        raise ConfigurationError(f"Categories file {path}: 'categories' must be a list.")

    tree: List[tuple] = []
    for entry in entries:
        if isinstance(entry, str):
            tree.append((entry, None))
        elif isinstance(entry, dict):
            for parent, children in entry.items():
                if children is not None and not isinstance(children, list):
                    raise ConfigurationError(
                        f"Categories file {path}: the children of {parent!r} must be a list."
                    )
                tree.append((parent, None))
                for child in children or []:
                    tree.append((str(child), parent))
        else:
            raise ConfigurationError(
                f"Categories file {path}: {entry!r} is neither a name nor a parent with children."
            )
    return tree


def build_paprika_categories(tree: List[tuple]) -> List[str]:
    """Flat list of unique leaf names derived from the category tree."""
    return list(dict.fromkeys(leaf for leaf, _ in tree))


def _build_prompt_hierarchy(tree: List[tuple]) -> str:
    lines = []
    for leaf, parent in tree:
        if parent is None:
            lines.append(f"- {leaf}")
        else:
            lines.append(f"    - {leaf}  (sub-category of {parent})")
    return "\n".join(lines)


def categorise_recipe(
    recipe: "RecipeExtraction",
    tree: List[tuple],
    paprika_categories: List[str],
    client,
) -> List[str]:
    """
    Ask Gemini to assign 1–3 categories from paprika_categories that best fit
    this recipe.  Returns a list of leaf category name strings exactly as they
    appear in Paprika.  Falls back to ["EPUB Imports"] when there is no
    taxonomy or the reply names no offered category (or is not JSON); errors
    from the Gemini call itself propagate.
    """
    if not paprika_categories:
        return ["EPUB Imports"]

    category_list = _build_prompt_hierarchy(tree)
    ingredient_sample = "\n".join(recipe.ingredients[:10])

    prompt = f"""You are a recipe categorisation assistant.

Given the recipe details below, select the 1 to 3 most appropriate categories
from the provided list.  Prefer specific sub-categories over their parent when
the recipe clearly fits (e.g. choose "Cake" rather than "Dessert" for a cake).
Only choose category names from the list — do not invent new ones.
Return the exact category name as shown (the leaf name, not "Parent/Child").

Return ONLY a JSON array of strings, e.g. ["Pizza", "Baking Basics"]

Available categories:
{category_list}

Recipe name: {recipe.name}
First ingredients: {ingredient_sample}
Notes: {recipe.notes or ""}
"""
    # Routed through gemini._call_with_retry rather than calling the client
    # directly: that is the one path carrying the HTTP timeout, the
    # thinking-budget cap, the back-off ladder, and usage-metadata
    # logging. A bulk `--recategorize` run should survive a transient 503
    # rather than silently dropping that recipe into the fallback bucket.
    # Imported here, not at module scope, to keep categories.py
    # importable without pulling in the Gemini module.
    from recipeparser.gemini import _call_with_retry

    # Not caught here: an API error that outlived the retries, a used-up daily
    # quota or a cancel is not an answer, and the fallback would read as one.
    # The caller decides (recategorize keeps the recipe's own categories and
    # counts it; quota and cancel stop the run).
    response = _call_with_retry(
        client,
        model=GEMINI_MODEL,
        contents=prompt,
        config={},
        what="Categorisation",
    )
    text = (response.text or "").strip()
    text = re.sub(r"^```[a-z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    try:
        categories = json.loads(text)
    except json.JSONDecodeError as e:
        log.warning("  -> Category reply for '%s' was not JSON: %s", recipe.name, e)
        return ["EPUB Imports"]
    if isinstance(categories, list) and categories:
        valid = [c for c in categories if isinstance(c, str) and c in paprika_categories]
        if valid:
            return valid
    log.warning("  -> No offered category in the reply for '%s'.", recipe.name)
    return ["EPUB Imports"]
