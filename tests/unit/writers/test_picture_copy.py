"""Copying a bucket picture out of an export's address (Cayenne restore)."""
from __future__ import annotations

import logging
from unittest.mock import MagicMock

from recipeparser.io.writers.picture_copy import BucketPictures, download_bucket_picture

SUPABASE = "https://old.supabase.co"
BUCKET_URL = f"{SUPABASE}/storage/v1/object/public/recipe-images/abc.jpg?v=123"


def _client(data=b"imgbytes", error=None):
    client = MagicMock()
    bucket = client.storage.from_.return_value
    if error:
        bucket.download.side_effect = error
    else:
        bucket.download.return_value = data
    return client


def test_bucket_url_is_downloaded():
    client = _client()
    assert download_bucket_picture(BUCKET_URL, supabase_url=SUPABASE, client=client) == (b"imgbytes", "image/jpeg")
    client.storage.from_.assert_called_with("recipe-images")
    client.storage.from_.return_value.download.assert_called_once_with("abc.jpg")


def test_png_key_gets_a_png_type():
    url = f"{SUPABASE}/storage/v1/object/public/recipe-images/x.png"
    assert download_bucket_picture(url, supabase_url=SUPABASE, client=_client())[1] == "image/png"


def test_foreign_url_is_not_downloaded():
    client = _client()
    assert download_bucket_picture("https://example.com/p.jpg", supabase_url=SUPABASE, client=client) is None
    client.storage.from_.assert_not_called()


def test_download_error_returns_none_and_logs(caplog):
    with caplog.at_level(logging.WARNING):
        got = download_bucket_picture(BUCKET_URL, supabase_url=SUPABASE, client=_client(error=RuntimeError("gone")))
    assert got is None
    assert any("gone" in r.getMessage() for r in caplog.records)


def test_empty_download_returns_none():
    assert download_bucket_picture(BUCKET_URL, supabase_url=SUPABASE, client=_client(data=b"")) is None


def test_bucket_pictures_owns_only_bucket_urls():
    pics = BucketPictures(SUPABASE, _client())
    assert pics.owns(BUCKET_URL)
    assert not pics.owns("https://example.com/p.jpg")
    assert not pics.owns(None)
