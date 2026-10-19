"""A photograph of a recipe, read through the vision OCR the scanned-PDF path owns."""
from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, List, Optional

import fitz  # type: ignore[import-untyped]  # PyMuPDF

from recipeparser.config import VISION_JPEG_QUALITY, VISION_MAX_EDGE_PX
from recipeparser.core.models import Chunk, InputType
from recipeparser.exceptions import ImageExtractionError, JobCancelledError
from recipeparser.io.readers import RecipeReader

if TYPE_CHECKING:
    from recipeparser.gemini import VisionImage

log = logging.getLogger(__name__)


def photo_for_vision(source: str) -> Optional["VisionImage"]:
    """The photo at ``source`` as the vision OCR should see it, or None if Pillow cannot read it.

    Upright (its EXIF orientation applied, which PyMuPDF ignores), RGB, its long
    edge at most VISION_MAX_EDGE_PX (never enlarged), and a JPEG at
    VISION_JPEG_QUALITY. Until 2026-10-18 a photo went through the scanned-PDF
    render instead: doubled, then sent as a lossless PNG. The tester's 4.1 MB,
    3000x4000 photo became 6000x8000 (144 MB decoded); Gemini answered 504
    DEADLINE_EXCEEDED four times and the API was OOM-killed on the fifth
    attempt (Cayenne F-426).
    """
    from PIL import Image, ImageOps  # noqa: PLC0415

    from recipeparser.gemini import VisionImage  # noqa: PLC0415

    try:
        with Image.open(source) as opened:
            upright = ImageOps.exif_transpose(opened)
            rgb = upright.convert("RGB")
    except Exception as exc:  # noqa: BLE001
        log.info("Pillow could not read the photo (%s); rendering it through PyMuPDF.", exc)
        return None
    rgb.thumbnail((VISION_MAX_EDGE_PX, VISION_MAX_EDGE_PX), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    rgb.save(buffer, format="JPEG", quality=VISION_JPEG_QUALITY, optimize=True)
    return VisionImage(buffer.getvalue(), "image/jpeg", rgb.width, rgb.height)


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
                The message is a predicate about the file and never names
                ``source``, the server's temp file: the API prefixes the
                user's own filename.
            RuntimeError: the model returned no text for it (from
                ``extract_text_via_vision``; the job fails with that message).
        """
        # Imported here, as pdf.py does, so this module stays importable without
        # the Gemini SDK on the path.
        from recipeparser.gemini import extract_text_from_images, extract_text_via_vision  # noqa: PLC0415

        try:
            doc = fitz.open(source)
        except Exception as exc:
            # The library's own text can carry the server path; it goes to the
            # log, and the user sees the predicate.
            log.warning("Image could not be opened: %s", exc)
            raise ImageExtractionError("could not be opened as an image.") from exc
        try:
            if doc.page_count == 0:
                raise ImageExtractionError("could not be opened as an image: no pages.")
            if doc.page_count != 1:
                # A PDF renamed .jpg opens fine here — PyMuPDF sniffs content, not
                # the extension — and would otherwise become one vision call per
                # page instead of the single photo this reader promises.
                raise ImageExtractionError(f"is not a single image ({doc.page_count} pages).")
            try:
                photo = photo_for_vision(source)
                if photo is not None:
                    text = extract_text_from_images([photo], self._client, count=1, what="photo")
                else:
                    # Something PyMuPDF opens and Pillow does not (a one-page PDF
                    # named .jpg): the page render, capped like any other.
                    text = extract_text_via_vision(doc, self._client, what="photo")
            except (RuntimeError, JobCancelledError):
                # The model read the page fine and found nothing on it, or kept
                # timing out (AIServiceTimeoutError is a RuntimeError), or the
                # cook cancelled — none of them is a bad file.
                raise
            except Exception as exc:
                # PyMuPDF defers format validation past fitz.open(): a file
                # that isn't really an image only fails once a page is
                # decoded, which happens inside extract_text_via_vision.
                log.warning("Image could not be decoded: %s", exc)
                raise ImageExtractionError("could not be opened as an image.") from exc
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
