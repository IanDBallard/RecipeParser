"""Carry a book's photographs from its temporary image directory onto its chunks.

EpubReader and PdfReader extract images to a directory that is deleted when
read() returns, and mark where each stood with ``[IMAGE: filename]``.  The
model names the hero photo by that filename (``photo_filename``); these
helpers make sure the bytes behind the name are still there when it does.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Dict, List

from recipeparser.config import HERO_INJECT_MAX_STUB_CHARS
from recipeparser.io.readers.epub import is_recipe_candidate

log = logging.getLogger(__name__)

#: Every image a chunk's text names, hero or not.
_MARKER_RE = re.compile(r"\[(?:HERO )?IMAGE:\s*([^\]]+?)\s*\]")
#: A plain marker on a line of its own — the shape a photo-only page reduces to.
_IMAGE_ONLY_RE = re.compile(r"^\s*\[IMAGE:\s*([^\]]+)\]\s*", re.MULTILINE)


def inject_hero_markers(chunks: List[str]) -> List[str]:
    """Prepend a photo-only page's image to the chunk after it as ``[HERO IMAGE: …]``.

    Some books put the finished-dish photo on its own page just before the
    recipe. That page fails ``is_recipe_candidate`` and its photo would never
    reach the model; the extract prompt treats a HERO marker as definitive.
    """
    enriched = list(chunks)
    for i, chunk in enumerate(chunks):
        if i + 1 >= len(enriched) or is_recipe_candidate(chunk):
            continue
        markers = _IMAGE_ONLY_RE.findall(chunk)
        stub_text = _IMAGE_ONLY_RE.sub("", chunk).strip()
        if markers and len(stub_text) < HERO_INJECT_MAX_STUB_CHARS:
            name = markers[-1].strip()
            enriched[i + 1] = f"[HERO IMAGE: {name}]\n" + enriched[i + 1]
            log.debug("Injected hero image '%s' from chunk %d into chunk %d.", name, i, i + 1)
    return enriched


def images_named_in(text: str, image_dir: str) -> Dict[str, bytes]:
    """The bytes of every image *text* marks that exists in *image_dir*, keyed by filename."""
    images: Dict[str, bytes] = {}
    for name in _MARKER_RE.findall(text):
        name = os.path.basename(name)
        if name in images:
            continue
        path = os.path.join(image_dir, name)
        if os.path.isfile(path):
            with open(path, "rb") as f:
                images[name] = f.read()
    return images
