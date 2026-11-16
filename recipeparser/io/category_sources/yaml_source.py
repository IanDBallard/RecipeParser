
"""
YAML-backed category source for CLI and GUI adapters.

Loads a user's taxonomy from a YAML file on disk.
Intended for local/offline use — does not require Supabase connectivity.

Two formats are read.

``axes:`` — named axes, each a list of tags::

    axes:
      Cuisine:
        - Italian
        - Mexican
      Meal Type:
        - Breakfast
        - Dessert

``categories:`` — the Paprika-style tree the app itself writes (the bundled
default, ``--sync-categories``, the GUI editor; see ``recipeparser.categories``)::

    categories:
      - Breakfast
      - Dessert:
          - Cake
          - Pie

Every name in the tree is offered on one axis, ``CATEGORIES_AXIS``, so TAG
makes one call per batch rather than one per top-level name; a child keeps
its parent through ``load_parents``. Before issue #149 only ``axes:`` was
read, so every CLI and GUI run tagged nothing.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import yaml  # type: ignore[import-untyped]

from recipeparser.io.category_sources.base import CategorySource

log = logging.getLogger(__name__)

#: The axis every name of a ``categories:`` tree is offered on.
CATEGORIES_AXIS = "Category"

_Taxonomy = Tuple[Dict[str, List[str]], Dict[str, str]]


class YamlCategorySource(CategorySource):
    """
    Loads taxonomy axes from a YAML file.

    Args:
        yaml_path: Path to the YAML taxonomy file. If None or the file does
                   not exist, load_axes() returns {} (no categorization).
    """

    def __init__(self, yaml_path: Union[str, Path, None] = None) -> None:
        self._path = Path(yaml_path) if yaml_path else None

    def load_axes(self, user_id: str = "") -> Dict[str, List[str]]:
        """
        Load axes from the YAML file. ``user_id`` is ignored (file-based source).

        Returns {} if no path was provided, the file doesn't exist, or the
        file is malformed.
        """
        axes, _ = self._read()
        log.info("YamlCategorySource: loaded %d axes from %s.", len(axes), self._path)
        return axes

    def load_parents(self, user_id: str = "") -> Dict[str, str]:
        """``{child: parent}`` for a ``categories:`` tree; ``{}`` for ``axes:``."""
        return self._read()[1]

    def _read(self) -> _Taxonomy:
        if self._path is None:
            log.debug("YamlCategorySource: no path configured — returning empty axes.")
            return {}, {}

        if not self._path.exists():
            log.warning(
                "YamlCategorySource: taxonomy file not found at %s — returning empty axes.",
                self._path,
            )
            return {}, {}

        try:
            with self._path.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except Exception as exc:
            log.error("YamlCategorySource: failed to parse %s: %s", self._path, exc)
            return {}, {}

        if isinstance(data, dict) and "axes" in data:
            return _axes_format(data["axes"]), {}
        if isinstance(data, dict) and "categories" in data:
            return _tree_format(data["categories"])

        log.warning(
            "YamlCategorySource: %s has neither an 'axes' nor a 'categories' key — "
            "returning empty axes.",
            self._path,
        )
        return {}, {}

    def load_category_ids(self, user_id: str = "") -> Dict[str, str]:
        """
        YAML source does not write to Supabase — returns empty dict.
        Junction table writes are not supported for file-based sources.
        """
        return {}


def _axes_format(raw_axes: Any) -> Dict[str, List[str]]:
    """Each value must be a list of strings; anything else is skipped."""
    if not isinstance(raw_axes, dict):
        log.warning("YamlCategorySource: 'axes' must be a dict — returning empty axes.")
        return {}
    axes: Dict[str, List[str]] = {}
    for axis_name, tags in raw_axes.items():
        if not isinstance(tags, list):
            log.warning(
                "YamlCategorySource: axis '%s' value is not a list — skipping.", axis_name
            )
            continue
        clean_tags = [str(t) for t in tags if t is not None]
        if clean_tags:
            axes[str(axis_name)] = clean_tags
    return axes


def _tree_format(entries: Any) -> _Taxonomy:
    """Flatten a ``categories:`` tree onto ``CATEGORIES_AXIS``, in file order.

    Names are stripped (the bundled file has ``'Grilled '``) and offered once:
    the first occurrence decides whether a name has a parent.
    """
    names: List[str] = []
    parents: Dict[str, str] = {}

    def add(entry: Any, parent: Optional[str]) -> None:
        if isinstance(entry, dict):
            for key, children in entry.items():
                name = add_name(key, parent)
                for child in children if isinstance(children, list) else []:
                    add(child, name)
        elif entry is not None:
            add_name(entry, parent)

    def add_name(raw: Any, parent: Optional[str]) -> Optional[str]:
        name = str(raw).strip()
        if not name:
            return parent
        if name not in names:
            names.append(name)
            if parent is not None:
                parents[name] = parent
        return name

    if not isinstance(entries, list):
        log.warning("YamlCategorySource: 'categories' must be a list — returning empty axes.")
        return {}, {}
    for entry in entries:
        add(entry, None)
    return ({CATEGORIES_AXIS: names} if names else {}), parents
