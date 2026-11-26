"""Reading a picture back out of the recipe-images bucket (Cayenne restore).

An export may name a picture by its address in the old account's bucket without carrying
its bytes. That address dies with the account, so a restore downloads the object and stores
it again as the new recipe's own picture.
"""
from __future__ import annotations

from typing import Any, Optional, Tuple

from recipeparser.adapters.share_worker import _TYPE_BY_EXTENSION, bucket_key
from recipeparser.exceptions import DailyQuotaExhaustedError, ImageStoreError, JobCancelledError
from recipeparser.io.writers.image_store import BUCKET


def download_bucket_picture(image_url: str, *, supabase_url: str, client: Any) -> Optional[Tuple[bytes, str]]:
    """(bytes, content type) of a bucket picture; None for a URL that is not one.

    Raises ``ImageStoreError`` when the download fails or comes back empty: that is a
    lost picture, which the pipeline reports through ``on_picture_lost``, not "no picture".
    """
    key = bucket_key(image_url, supabase_url)
    if key is None:
        return None
    try:
        data = client.storage.from_(BUCKET).download(key)
    except (JobCancelledError, DailyQuotaExhaustedError):
        raise
    except Exception as exc:  # noqa: BLE001  # fail-loud.bare-except-ok: re-raised as ImageStoreError
        raise ImageStoreError(f"could not download the picture {key}: {exc}") from exc
    if not data:
        raise ImageStoreError(f"the picture {key} downloaded empty")
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
