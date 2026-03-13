"""Shared utility functions for file handling and text processing."""
import contextlib
import os
import tempfile
from typing import Generator

from bs4 import BeautifulSoup


@contextlib.contextmanager
def temp_file_from_upload(upload_file) -> Generator[str, None, None]:
    """
    Context manager that reads an UploadFile (FastAPI), writes it to a
    temporary file on disk, yields the path, and ensures cleanup.
    """
    suffix = os.path.splitext(upload_file.filename or "")[1]
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(upload_file.file.read())
        tmp_path = tmp.name

    try:
        yield tmp_path
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def html_to_text(html_content: str) -> str:
    """
    Convert HTML to plain text using BeautifulSoup, stripping all tags
    and preserving newlines.
    """
    soup = BeautifulSoup(html_content, "html.parser")
    return soup.get_text(separator="\n", strip=True)
