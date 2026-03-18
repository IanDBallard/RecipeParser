"""
YAML-backed category source for CLI and GUI adapters.

Loads a user's multipolar taxonomy from a YAML file on disk.
Intended for local/offline use — does not require Supabase connectivity.

Expected YAML format:
    axes:
      Cuisine:
        - Italian
        - Mexican
        - Japanese
      Protein:
        - Chicken
        - Beef
        - Vegetarian
      Meal Type:
        - Breakfast
        - Dinner
        - Dessert

The top-level key must be ``axes``. Each key under ``axes`` is an axis name;
its value is a list of valid tag strings.
"""
import logging
from pathlib import Path
from typing import Dict, List

import yaml

from recipeparser.io.category_sources.base import CategorySource

log = logging.getLogger(__name__)


class YamlCategorySource(CategorySource):
    """
    Loads taxonomy axes from a YAML file.

    Args:
        yaml_path: Path to the YAML taxonomy file. If None or the file does
                   not exist, load_axes() returns {} (no categorization).
    """

    def __init__(self, yaml_path: str | Path | None = None) -> None:
        self._path = Path(yaml_path) if yaml_path else None

    def load_axes(self, user_id: str) -> Dict[str, List[str]]:
        """
        Load axes from the YAML file. ``user_id`` is ignored (file-based source).

        Returns {} if no path was provided, the file doesn't exist, or the
        file is malformed.
        """
        if self._path is None:
            log.debug("YamlCategorySource: no path configured — returning empty axes.")
            return {}

        if not self._path.exists():
            log.warning(
                "YamlCategorySource: taxonomy file not found at %s — returning empty axes.",
                self._path,
            )
            return {}

        try:
            with self._path.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except Exception as exc:
            log.error("YamlCategorySource: failed to parse %s: %s", self._path, exc)
            return {}

        if not isinstance(data, dict) or "axes" not in data:
            log.warning(
                "YamlCategorySource: %s missing top-level 'axes' key — returning empty axes.",
                self._path,
            )
            return {}

        raw_axes = data["axes"]
        if not isinstance(raw_axes, dict):
            log.warning("YamlCategorySource: 'axes' must be a dict — returning empty axes.")
            return {}

        # Validate and coerce: each value must be a list of strings
        axes: Dict[str, List[str]] = {}
        for axis_name, tags in raw_axes.items():
            if not isinstance(tags, list):
                log.warning(
                    "YamlCategorySource: axis '%s' value is not a list — skipping.",
                    axis_name,
                )
                continue
            clean_tags = [str(t) for t in tags if t is not None]
            if clean_tags:
                axes[str(axis_name)] = clean_tags

        log.info(
            "YamlCategorySource: loaded %d axes from %s.",
            len(axes),
            self._path,
        )
        return axes

    def load_category_ids(self, user_id: str) -> Dict[str, str]:
        """
        YAML source does not write to Supabase — returns empty dict.
        Junction table writes are not supported for file-based sources.
        """
        return {}
