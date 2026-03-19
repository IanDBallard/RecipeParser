"""
core/models.py — Shared data models for the hexagonal pipeline.

This module defines the Chunk dataclass and InputType enum that form the
contract between I/O readers and the RecipePipeline orchestrator.

Design rule: this module imports ONLY from stdlib and recipeparser.models.
It must never import from recipeparser.io or recipeparser.adapters.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, List, Optional, Union

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

    PAPRIKA_LEGACY = "PAPRIKA_LEGACY"
    """A Paprika recipe entry with no _cayenne_meta key — requires full pipeline."""

    PAPRIKA_CAYENNE = "PAPRIKA_CAYENNE"
    """A Paprika recipe entry with a valid _cayenne_meta key — fast-path restore."""


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
    pre_parsed:
        Fully-assembled IngestResponse deserialized from ``_cayenne_meta``.
        Set only for PAPRIKA_CAYENNE chunks.  When present, the pipeline
        skips EXTRACT, REFINE, and CATEGORIZE entirely.
    pre_parsed_embedding:
        The 1536-dim embedding stored in ``_cayenne_meta``.  When present
        alongside ``pre_parsed``, the pipeline also skips EMBED — achieving
        $0 cost for Cayenne-native restores.
    """

    text: str
    input_type: InputType
    source_url: Optional[str] = None
    image_url: Optional[str] = None
    image_bytes: Optional[bytes] = None
    pre_parsed: Optional[Union["CayenneRecipe", "IngestResponse"]] = None
    pre_parsed_embedding: Optional[List[float]] = field(default=None)
