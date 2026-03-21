"""
Regression tests for _select_reader() in recipeparser.adapters.api.

Bug fixed: the original condition was:
    if ext == ".paprikarecipes" or (
        not ext and content_type in (...) and filename.lower().endswith(".paprikarecipes")
    ):
The second branch was unreachable — if filename ends with ".paprikarecipes" then
os.path.splitext() will always produce ext == ".paprikarecipes", so `not ext` is
always False.  This broke fallback detection for paprika files sent with a generic
MIME type (application/zip or application/octet-stream).

Gate command: pytest tests/unit/test_select_reader.py -v
"""
from __future__ import annotations

import pytest

from recipeparser.adapters.api import _select_reader


# ---------------------------------------------------------------------------
# Normal extension-based routing
# ---------------------------------------------------------------------------

class TestSelectReaderByExtension:

    def test_pdf_extension_returns_pdf(self):
        assert _select_reader("recipe.pdf", "application/pdf") == "pdf"

    def test_epub_extension_returns_epub(self):
        assert _select_reader("cookbook.epub", "application/epub+zip") == "epub"

    def test_paprikarecipes_extension_returns_paprika(self):
        """Primary path: .paprikarecipes extension is present."""
        assert _select_reader("export.paprikarecipes", "application/zip") == "paprika"

    def test_paprikarecipes_extension_case_insensitive(self):
        assert _select_reader("EXPORT.PAPRIKARECIPES", "application/zip") == "paprika"

    def test_pdf_extension_overrides_content_type(self):
        """Extension takes priority over content-type."""
        assert _select_reader("recipe.pdf", "application/octet-stream") == "pdf"

    def test_epub_extension_overrides_content_type(self):
        assert _select_reader("book.epub", "application/octet-stream") == "epub"


# ---------------------------------------------------------------------------
# Fallback routing via content-type (Bug 1 regression)
# ---------------------------------------------------------------------------

class TestSelectReaderFallbackContentType:
    """
    These are the cases that were BROKEN before the fix.

    When a browser or HTTP client sends a .paprikarecipes file with a generic
    MIME type (application/zip or application/octet-stream), the old code's
    second branch was unreachable because `not ext` was always False when the
    filename ended with ".paprikarecipes".  The fix splits this into two
    independent `if` statements.
    """

    def test_paprika_via_application_zip_content_type(self):
        """
        Regression: file named 'export.paprikarecipes' sent as application/zip
        must still route to the paprika reader.
        """
        result = _select_reader("export.paprikarecipes", "application/zip")
        assert result == "paprika", (
            "Bug 1 regression: paprika file with application/zip content-type "
            "was not routed to the paprika reader"
        )

    def test_paprika_via_octet_stream_content_type(self):
        """
        Regression: file named 'my_recipes.paprikarecipes' sent as
        application/octet-stream must still route to the paprika reader.
        """
        result = _select_reader("my_recipes.paprikarecipes", "application/octet-stream")
        assert result == "paprika", (
            "Bug 1 regression: paprika file with application/octet-stream content-type "
            "was not routed to the paprika reader"
        )

    def test_paprika_filename_uppercase_via_octet_stream(self):
        """Case-insensitive filename match must work for the fallback path too."""
        result = _select_reader("BACKUP.PAPRIKARECIPES", "application/octet-stream")
        assert result == "paprika"

    def test_non_paprika_zip_not_misrouted(self):
        """A plain .zip file must raise ValueError — it is not a supported type."""
        with pytest.raises(ValueError, match="Unsupported file type"):
            _select_reader("archive.zip", "application/zip")

    def test_pdf_via_octet_stream_still_routes_to_pdf(self):
        """PDF with octet-stream content-type should still route to pdf."""
        result = _select_reader("recipe.pdf", "application/octet-stream")
        assert result == "pdf"

    def test_epub_via_octet_stream_still_routes_to_epub(self):
        """EPUB with octet-stream content-type should still route to epub."""
        result = _select_reader("cookbook.epub", "application/octet-stream")
        assert result == "epub"
