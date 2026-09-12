"""A photograph of a recipe, read through the vision OCR the scanned-PDF path owns."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, List

import fitz  # type: ignore[import-untyped]  # PyMuPDF

from recipeparser.core.models import Chunk, InputType
from recipeparser.exceptions import ImageExtractionError
from recipeparser.io.readers import RecipeReader

log = logging.getLogger(__name__)


class ImageReader(RecipeReader):
    """
    Reads one photo and returns one Chunk holding its transcript.

    PyMuPDF opens a JPEG or PNG as a one-page document, which is exactly
    what ``extract_text_via_vision`` takes — the same call that reads a scanned
    PDF. The chunk carries no citation and no image: the transcript says where
    the recipe came from (``resolve_citation`` reads the model's stated source),
    and a photograph of a page is not a photograph of the dish.

    Args:
        client: An initialised ``google.genai.Client``; the OCR is a model call.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def read(self, source: str) -> List[Chunk]:
        """
        Transcribe the photo at ``source`` and return a single-element list.

        Raises:
            ImageExtractionError: PyMuPDF could not open the file as an image.
            RuntimeError: the model returned no text for it (from
                ``extract_text_via_vision``; the job fails with that message).
        """
        # Imported here, as pdf.py does, so this module stays importable without
        # the Gemini SDK on the path.
        from recipeparser.gemini import extract_text_via_vision  # noqa: PLC0415

        try:
            doc = fitz.open(source)
        except Exception as exc:
            raise ImageExtractionError(f"Could not open '{Path(source).name}' as an image: {exc}") from exc
        try:
            if doc.page_count == 0:
                raise ImageExtractionError(f"Could not open '{Path(source).name}' as an image: no pages.")
            if doc.page_count != 1:
                # A PDF renamed .jpg opens fine here — PyMuPDF sniffs content, not
                # the extension — and would otherwise become one vision call per
                # page instead of the single photo this reader promises.
                name = Path(source).name
                raise ImageExtractionError(f"'{name}' is not a single image ({doc.page_count} pages).")
            try:
                text = extract_text_via_vision(doc, self._client)
            except RuntimeError:
                # The model read the page fine and found nothing on it — that
                # is extract_text_via_vision's own error, not a bad file.
                raise
            except Exception as exc:
                # PyMuPDF defers format validation past fitz.open(): a file
                # that isn't really an image only fails once a page is
                # decoded, which happens inside extract_text_via_vision.
                raise ImageExtractionError(f"Could not open '{Path(source).name}' as an image: {exc}") from exc
        finally:
            doc.close()

        log.info("ImageReader: %s transcribed to %d chars.", Path(source).name, len(text))
        return [
            Chunk(
                text=text,
                input_type=InputType.IMAGE,
                source_url=None,
                citation=None,
                label=Path(source).name,
            )
        ]
