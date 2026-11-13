"""
mela.py — Reader for Mela exports.

A ``.melarecipes`` file is a ZIP archive whose members are ``.melarecipe``
files; a ``.melarecipe`` is one UTF-8 JSON object, uncompressed. Mela publishes
no specification: the format is recorded in the Mela import design (Cayenne
docs/superpowers/specs/2026-11-13-mela-import-design.md §4). Every entry goes
through the full pipeline as InputType.MELA, like a legacy Paprika entry.

Written from that spec alone. No code from Tandoor, Mealie or RecipeSage (AGPL).
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import zipfile
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

from recipeparser.core.models import Chunk, InputType, RestoreFields, SourceMeta
from recipeparser.exceptions import MelaExtractionError
from recipeparser.io.readers import RecipeReader
from recipeparser.io.readers._recipe_text import recipe_text, source_citation

log = logging.getLogger(__name__)

# The most one entry may be: its JSON with its base64 pictures. A library has no
# ceiling (it is a whole collection), so each entry is bounded instead.
_MAX_ENTRY_BYTES = 50_000_000

# A picture is a nicety, so it is bounded twice: each decoded picture, and all the
# pictures one read keeps (a hostile archive deflates ~1000:1, so a small file can
# otherwise claim gigabytes). A recipe is never dropped for its picture.
_MAX_PICTURE_BYTES = 15_000_000
_MAX_KEPT_PICTURE_BYTES = 512_000_000

_ENTRY_SUFFIX = ".melarecipe"

_HEADING = re.compile(r"^[ \t]*#+[ \t]+(.+?)[ \t]*$", re.MULTILINE)
# [label](url), where the url may hold one level of balanced parentheses.
_LINK = re.compile(r"\[([^\]]*)\]\(((?:[^()]|\([^()]*\))*)\)")

_TOO_LARGE = "larger than 50 MB"
_UNREADABLE = "could not be read"


def clean_text(value: object) -> str:
    """Mela's two text conventions made plain (spec §5.1); anything not a string is empty.

    ``# Heading`` lines become the heading alone. ``[label](mela://…)`` becomes the
    label, and ``[label](url)`` becomes ``label (url)``. Nothing else changes.
    """
    return _HEADING.sub(r"\1", _clean_links(value))


def _clean_links(value: object) -> str:
    """Links only (spec §5.1): ``mela://`` gives the label, a web link ``label (url)``; all else is verbatim."""
    if not isinstance(value, str):
        return ""

    def _link(match: "re.Match[str]") -> str:
        label, url = match.group(1), match.group(2).strip()
        if url.lower().startswith("mela://"):
            return label
        if url.lower().startswith(("http://", "https://")):
            return f"{label} ({url})"
        return match.group(0)

    return _LINK.sub(_link, value)


def _text(value: object) -> str:
    """A string field trimmed; a number (not a bool) as its text, e.g. ``"yield": 8``; else empty."""
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return str(value).strip()
    return value.strip() if isinstance(value, str) else ""


def _picture_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


class _PictureBudget:
    """What one read may still keep in pictures; once spent, every later picture is dropped."""

    def __init__(self) -> None:
        self.kept = 0
        self.spent = False

    def admit(self, size: int, label: str) -> bool:
        if self.spent or self.kept + size > _MAX_KEPT_PICTURE_BYTES:
            if not self.spent:
                log.warning(
                    "MelaReader: pictures kept reached %d bytes at %r — later recipes import without pictures.",
                    _MAX_KEPT_PICTURE_BYTES, label,
                )
            self.spent = True
            return False
        self.kept += size
        return True


def _picture(images: object, label: str, budget: _PictureBudget) -> Tuple[Optional[bytes], str]:
    """The first image, decoded; one too big, over budget or undecodable is dropped, never the recipe."""
    if not isinstance(images, list) or not images or not isinstance(images[0], str):
        return None, "image/jpeg"
    encoded = "".join(images[0].split())
    if len(encoded) * 3 // 4 > _MAX_PICTURE_BYTES + 3:  # judged before decoding, so it is never decoded
        log.warning("MelaReader: the picture of %r is over %d bytes — continuing without it.", label, _MAX_PICTURE_BYTES)
        return None, "image/jpeg"
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        log.warning("MelaReader: could not decode the picture of %r — continuing without it.", label)
        return None, "image/jpeg"
    if not data:
        return None, "image/jpeg"
    if len(data) > _MAX_PICTURE_BYTES:
        log.warning("MelaReader: the picture of %r is over %d bytes — continuing without it.", label, _MAX_PICTURE_BYTES)
        return None, "image/jpeg"
    if not budget.admit(len(data), label):
        return None, "image/jpeg"
    return data, _picture_type(data)


def _skipped(name: str, reason: str) -> Chunk:
    """A line under "What was missed": no stage runs for it (D6)."""
    return Chunk(text="", input_type=InputType.MELA, skip_reason=f"{name}: {reason}", label=name or None)


def _entry_chunk(name: str, entry: dict, budget: _PictureBudget) -> Chunk:
    """One Mela recipe as a full-pipeline chunk (spec §5.1 mapping table)."""
    title = _text(entry.get("title"))
    ingredients = clean_text(entry.get("ingredients")).strip()
    instructions = clean_text(entry.get("instructions")).strip()
    notes = clean_text(entry.get("notes")).strip()
    if not (ingredients or instructions or notes):
        return _skipped(name, "has no recipe text")  # a title alone is not a recipe
    # Notes-only entries (the whole recipe typed into Notes) go to the AI as the directions.
    directions = instructions if (ingredients or instructions) else notes

    label = title or "Untitled recipe"
    link = _text(entry.get("link"))
    is_web = link.lower().startswith(("http://", "https://"))
    source_url = link if is_web else None
    source = None if is_web or link.lower().startswith("mela://") else (link or None)
    picture, picture_type = _picture(entry.get("images"), label, budget)

    return Chunk(
        text=recipe_text(
            title,
            ingredients,
            directions,
            facts=(("Servings", _text(entry.get("yield"))), ("Total time", _text(entry.get("totalTime")))),
        ),
        input_type=InputType.MELA,
        source_url=source_url,
        citation=source_citation(source, source_url),
        image_bytes=picture,
        image_content_type=picture_type,
        label=label,
        meta=SourceMeta(
            prep_time=_text(entry.get("prepTime")),
            cook_time=_text(entry.get("cookTime")),
            source=source,
            notes=clean_text(entry.get("notes")),
            nutritional_info=_text(entry.get("nutrition")),
            description=_clean_links(entry.get("text")),
        ),
        restore=RestoreFields(is_favourite=True) if entry.get("favorite") is True else None,
    )


class MelaReader(RecipeReader):
    """Reads a ``.melarecipes`` archive or a single ``.melarecipe`` into one chunk per recipe."""

    def read(self, source: str) -> List[Chunk]:
        path = Path(source)
        single = path.suffix.lower() == _ENTRY_SUFFIX
        chunks: List[Chunk] = []

        # Entries are parsed one at a time, each entry's JSON released once its chunk is
        # built. The chunks themselves are all kept, so pictures are bounded per picture
        # and per read (_MAX_PICTURE_BYTES, _MAX_KEPT_PICTURE_BYTES).
        budget = _PictureBudget()
        for name, data in self._entries(path, single):
            entry = None
            if data is not None:
                try:
                    parsed = json.loads(data.decode("utf-8-sig"))
                    entry = parsed if isinstance(parsed, dict) else None
                except (json.JSONDecodeError, UnicodeDecodeError):
                    entry = None
            if entry is None:
                reason = _TOO_LARGE if data is None else _UNREADABLE
                if single:
                    detail = _TOO_LARGE if data is None else "not readable JSON"
                    raise MelaExtractionError(f"is not a Mela recipe: {detail}.")
                log.warning("MelaReader: skipping %r — %s.", name, reason)
                chunks.append(_skipped(name, reason))
                continue
            chunks.append(_entry_chunk(name, entry, budget))

        if not any(chunk.skip_reason is None for chunk in chunks):
            raise MelaExtractionError("contains no recipes.")
        log.info("MelaReader.read: produced %d chunks from %s", len(chunks), path.name)
        return chunks

    def _entries(self, path: Path, single: bool) -> Iterator[Tuple[str, Optional[bytes]]]:
        """(name, bytes) per entry, the name without its extension; bytes is None past the cap."""
        if single:
            with path.open("rb") as fh:
                data = fh.read(_MAX_ENTRY_BYTES + 1)
            yield path.stem, (None if len(data) > _MAX_ENTRY_BYTES else data)
            return

        if not zipfile.is_zipfile(path):
            raise MelaExtractionError("is not a Mela export: not a ZIP archive.")
        try:
            zf = zipfile.ZipFile(path, "r")
        except zipfile.BadZipFile as exc:
            raise MelaExtractionError("is not a Mela export: not a ZIP archive.") from exc
        with zf:
            for info in zf.infolist():
                member = info.filename
                leaf = member.rsplit("/", 1)[-1]
                if info.is_dir() or not leaf.lower().endswith(_ENTRY_SUFFIX):
                    continue
                if member.startswith("__MACOSX/") or leaf.startswith("._"):
                    continue  # macOS resource forks, not recipes (Review Focus 1)
                name = leaf[: -len(_ENTRY_SUFFIX)]
                try:
                    with zf.open(info) as fh:
                        data = fh.read(_MAX_ENTRY_BYTES + 1)
                except Exception as exc:  # corrupt (zlib.error), encrypted (RuntimeError), unsupported, truncated
                    log.warning("MelaReader: could not open member %r: %s", member, exc)
                    yield name, b""  # empty bytes fail to parse and become "could not be read"
                    continue
                yield name, (None if len(data) > _MAX_ENTRY_BYTES else data)
