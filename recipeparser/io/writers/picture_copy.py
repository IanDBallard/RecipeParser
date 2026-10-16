"""Reading a picture back out of the recipe-images bucket (Cayenne restore).

An export may name a picture by its address in the old account's bucket without carrying
its bytes. That address dies with the account, so a restore downloads the object and stores
it again as the new recipe's own picture.
"""
from __future__ import annotations

import logging
from typing import Any, Optional, Tuple

from recipeparser.adapters.share_worker import _TYPE_BY_EXTENSION, bucket_key
from recipeparser.io.writers.image_store import BUCKET

log = logging.getLogger(__name__)


def download_bucket_picture(image_url: str, *, supabase_url: str, client: Any) -> Optional[Tuple[bytes, str]]:
    """(bytes, content type) of a bucket picture; None for any other URL or on any error."""
    key = bucket_key(image_url, supabase_url)
    if key is None:
        return None
    try:
        data = client.storage.from_(BUCKET).download(key)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not download the picture %s (%s).", key, exc)
        return None
    if not data:
        log.warning("the picture %s downloaded empty.", key)
        return None
    return data, _TYPE_BY_EXTENSION.get(key.rsplit(".", 1)[-1].lower(), "image/jpeg")


class BucketPictures:
    """The pipeline's picture source: which URLs are bucket pictures, and how to fetch one."""

    def __init__(self, supabase_url: str, client: Any) -> None:
        self._supabase_url = supabase_url
        self._client = client

    def owns(self, image_url: Optional[str]) -> bool:
        return bucket_key(image_url, self._supabase_url) is not None

    def download(self, image_url: str) -> Optional[Tuple[bytes, str]]:
        return download_bucket_picture(image_url, supabase_url=self._supabase_url, client=self._client)
