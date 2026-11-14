"""Builds small Mela exports for tests. Written from the Mela import spec §4, not from any other importer."""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Dict, Union

from PIL import Image  # Pillow is already a RecipeParser dependency (RP#61)


def _tiny(fmt: str) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (1, 1), "white").save(buf, fmt)
    return buf.getvalue()


# 1x1 white pictures: enough bytes for a picture that decodes and sniffs by its magic bytes.
TINY_JPEG = _tiny("JPEG")
TINY_PNG = _tiny("PNG")


def entry(**fields) -> dict:
    """A minimal Mela recipe JSON object; keyword arguments override or add fields."""
    base = {
        "id": "test", "title": "Pie", "ingredients": "1 egg", "instructions": "Bake.", "categories": [], "images": [],
    }
    base.update(fields)
    return base


def _bytes(value: Union[bytes, dict]) -> bytes:
    return value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")


def make_archive(tmp_path: Path, entries: Dict[str, Union[bytes, dict]], name: str = "Recipes.melarecipes") -> str:
    """A .melarecipes zip whose members are the given names, each a dict (JSON-encoded) or raw bytes."""
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as zf:
        for member, value in entries.items():
            zf.writestr(member, _bytes(value))
    return str(path)


def make_single(tmp_path: Path, value: Union[bytes, dict], name: str = "Pie.melarecipe") -> str:
    """A single .melarecipe file."""
    path = tmp_path / name
    path.write_bytes(_bytes(value))
    return str(path)
