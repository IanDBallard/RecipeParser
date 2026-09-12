"""
core/models.py — Shared data models for the hexagonal pipeline.

This module defines the Chunk dataclass and InputType enum that form the
contract between I/O readers and the RecipePipeline orchestrator.

Design rule: this module imports ONLY from stdlib, recipeparser.models and
recipeparser.core.citation (core→core is allowed). It must never import from
recipeparser.io or recipeparser.adapters.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, List, Optional, Union

from recipeparser.core.citation import Citation

if TYPE_CHECKING:
    # Avoid circular imports at runtime; only used for type hints.
    from recipeparser.models import CayenneRecipe, IngestResponse


class InputType(Enum):
    """Identifies the origin of a Chunk so the pipeline can route it correctly."""

    URL = "URL"
    """A single web page fetched via r.jina.ai."""

    PDF = "PDF"
    """One page-group chunk from a PDF document."""

    EPUB = "EPUB"
    """One chapter chunk from an EPUB document."""

    IMAGE = "IMAGE"
    """One photograph of a recipe, transcribed by vision OCR; routed like a book chunk."""

    PAPRIKA_LEGACY = "PAPRIKA_LEGACY"
    """A Paprika recipe entry with no _cayenne_meta key — requires full pipeline."""

    PAPRIKA_CAYENNE = "PAPRIKA_CAYENNE"
    """A Paprika recipe entry with a valid _cayenne_meta key — fast-path restore."""


@dataclass
class SourceMeta:
    """
    Fields a source supplies directly, rather than the extractor inferring them.

    Only a Paprika entry fills these in today: its JSON carries the recipe's own
    prep and cook times, its source, notes, rating and the rest, all of which the
    text blob handed to the extractor throws away.  A value here is authoritative
    and beats the extracted one (see ``assemble()``).

    Normalisation happens once, here, so every construction site gets it.  Paprika
    writes ``""`` for a field the recipe never filled in and ``0`` for an unrated
    recipe; both mean absent, and both become ``None``.  A stored ``0`` would read
    as a real zero-star rating to every client downstream.
    """

    prep_time: Optional[str] = None
    cook_time: Optional[str] = None
    source: Optional[str] = None
    notes: Optional[str] = None
    rating: Optional[int] = None
    nutritional_info: Optional[str] = None
    description: Optional[str] = None
    difficulty: Optional[str] = None

    def __post_init__(self) -> None:
        for name in (
            "prep_time",
            "cook_time",
            "source",
            "notes",
            "nutritional_info",
            "description",
            "difficulty",
        ):
            value = getattr(self, name)
            if value is None:
                continue
            stripped = str(value).strip()
            setattr(self, name, stripped or None)

        try:
            rating = int(self.rating) if self.rating is not None else None
        except (TypeError, ValueError):
            rating = None
        self.rating = rating or None


@dataclass
class Chunk:
    """
    A single unit of work for the RecipePipeline.

    Readers produce List[Chunk]; the pipeline consumes them.  The ``input_type``
    field drives stage routing (see §4.2 of PIPELINE_REFACTOR.md).

    Fields
    ------
    text:
        Raw text for the EXTRACT stage.  May be empty for PAPRIKA_CAYENNE
        chunks where ``pre_parsed`` is set.
    input_type:
        Determines which pipeline stages are executed for this chunk.
    source_url:
        Provenance URL (used to populate CayenneRecipe.source_url).
    image_url:
        Pre-resolved public image URL, if any (e.g. Supabase Storage URL
        already uploaded by the reader).
    image_bytes:
        Raw image bytes for Paprika entries that carry an embedded photo.
        The pipeline uploads these to Supabase Storage before calling the
        ASSEMBLE stage.
    image_content_type:
        MIME type of ``image_bytes``, taken from the source's own filename
        where it gives one.
    pre_parsed:
        Fully-assembled IngestResponse deserialized from ``_cayenne_meta``.
        Set only for PAPRIKA_CAYENNE chunks.  When present, the pipeline
        skips EXTRACT, REFINE, and CATEGORIZE entirely.
    pre_parsed_embedding:
        The 1536-dim embedding stored in ``_cayenne_meta``.  When present
        alongside ``pre_parsed``, the pipeline also skips EMBED — achieving
        $0 cost for Cayenne-native restores.
    label:
        Human-readable identifier for this chunk, used to name it if it is
        dropped.  A Paprika entry's name, an EPUB chapter title, a PDF page
        range — whatever the source actually provides.  None when the source
        names nothing: what a book chunk contained is unknown until extraction
        succeeds, and a dropped chunk is one where it did not.  No stage reads
        this field; it exists for reporting.
    meta:
        Fields the source stated for itself rather than the extractor inferring
        them.  Set by ``PaprikaReader``; None for every other reader, whose
        sources carry no such metadata.
    citation:
        What the reader knows about where the recipe came from: a book's
        metadata, a URL's host, a Paprika entry's source. None when only the
        text can say (pasted text), in which case assemble() takes the model's
        stated source. Books no longer put "Title — Author" in source_url.
    """

    text: str
    input_type: InputType
    source_url: Optional[str] = None
    citation: Optional[Citation] = None
    image_url: Optional[str] = None
    image_bytes: Optional[bytes] = None
    image_content_type: str = "image/jpeg"
    pre_parsed: Optional[Union["CayenneRecipe", "IngestResponse"]] = None
    pre_parsed_embedding: Optional[List[float]] = field(default=None)
    label: Optional[str] = None
    meta: Optional[SourceMeta] = None
