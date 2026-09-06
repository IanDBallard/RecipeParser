"""Supabase Storage implementation of the ImageStore port.

Holds the bucket logic that was previously inline in adapters/api.py, so both
the pipeline's byte uploads and the API's URL uploads go through one path.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from recipeparser.core.ports import ImageStore

log = logging.getLogger(__name__)

BUCKET = "recipe-images"

_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}


class SupabaseImageStore(ImageStore):
    """Uploads to the public ``recipe-images`` bucket using the service-role key."""

    def __init__(self, url: Optional[str] = None, service_key: Optional[str] = None) -> None:
        self._url = url or os.environ.get("SUPABASE_URL", "")
        self._key = service_key or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

    def put(self, image_bytes: bytes, recipe_id: str, content_type: str = "image/jpeg") -> Optional[str]:
        if not image_bytes:
            return None
        if not self._url or not self._key:
            log.warning("SupabaseImageStore: credentials not set — cannot store the image for %s.", recipe_id)
            return None
        ext = _EXTENSIONS.get(content_type.lower(), "jpg")
        path = f"{BUCKET}/{recipe_id}.{ext}"
        try:
            from supabase import create_client  # noqa: PLC0415

            sb = create_client(self._url, self._key)
            sb.storage.from_(BUCKET).upload(
                path,
                image_bytes,
                {"content-type": content_type, "upsert": "true"},
            )
            public_url: str = sb.storage.from_(BUCKET).get_public_url(path)
            return public_url
        except Exception:
            log.exception("SupabaseImageStore: failed to store the image for %s — continuing without it.", recipe_id)
            return None
