"""ImageReader — one photo, one chunk, through the vision OCR the PDF path already owns."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import fitz  # PyMuPDF
import pytest

from recipeparser.core.models import InputType
from recipeparser.exceptions import ImageExtractionError
from recipeparser.io.readers.image import ImageReader


def _png(tmp_path: Path, name: str = "IMG_4021.png") -> str:
    """A small real PNG, rendered by PyMuPDF so no binary fixture is committed."""
    doc = fitz.open()
    page = doc.new_page(width=200, height=100)
    page.insert_text((20, 50), "Cake: 1 cup flour. Mix.")
    path = tmp_path / name
    path.write_bytes(page.get_pixmap().tobytes("png"))
    doc.close()
    return str(path)


def _client(text: str) -> MagicMock:
    client = MagicMock()
    client.models.generate_content.return_value = MagicMock(text=text)
    return client


def test_a_photo_becomes_one_image_chunk_from_the_transcript(tmp_path):
    client = _client("Cake\n\n1 cup flour\n\nMix and bake.")
    chunks = ImageReader(client).read(_png(tmp_path))
    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.text == "Cake\n\n1 cup flour\n\nMix and bake."
    assert chunk.input_type is InputType.IMAGE
    assert chunk.source_url is None
    assert chunk.citation is None            # ruling 1: the transcript says where it came from
    assert chunk.image_url is None and chunk.image_bytes is None   # ruling 2
    assert chunk.label == "IMG_4021.png"


def test_the_vision_call_carries_the_photo_as_png_bytes(tmp_path):
    client = _client("something")
    ImageReader(client).read(_png(tmp_path))
    client.models.generate_content.assert_called_once()
    contents = client.models.generate_content.call_args.kwargs["contents"]
    assert len(contents) == 2                # the image part, then the OCR prompt
    assert getattr(contents[0], "inline_data", None) is not None
    assert contents[0].inline_data.mime_type == "image/png"


def test_a_file_pymupdf_cannot_open_is_an_image_extraction_error(tmp_path):
    bad = tmp_path / "not-a-photo.png"
    bad.write_bytes(b"this is not an image")
    with pytest.raises(ImageExtractionError, match="not-a-photo.png"):
        ImageReader(_client("x")).read(str(bad))


def test_a_multi_page_pdf_renamed_jpg_is_refused_before_any_vision_call(tmp_path):
    """PyMuPDF sniffs content, not the extension, so a PDF saved as .jpg opens
    fine — and without this check would become one vision call per page."""
    doc = fitz.open()
    doc.new_page()
    doc.new_page()
    path = tmp_path / "menu.jpg"
    doc.save(str(path))
    doc.close()

    client = _client("must not be read")
    with pytest.raises(ImageExtractionError, match=r"not a single image \(2 pages\)"):
        ImageReader(client).read(str(path))
    client.models.generate_content.assert_not_called()


def test_a_photo_the_model_cannot_read_raises_the_vision_error(tmp_path):
    with pytest.raises(RuntimeError, match="no text"):
        ImageReader(_client("")).read(_png(tmp_path))


def test_input_type_image_routes_through_the_full_pipeline():
    from recipeparser.core.models import Chunk
    from recipeparser.core.pipeline import RecipePipeline

    stages = RecipePipeline._get_stages(MagicMock(), Chunk(text="x", input_type=InputType.IMAGE))
    assert stages == ["EXTRACT", "REFINE", "CATEGORIZE", "EMBED", "ASSEMBLE"]
