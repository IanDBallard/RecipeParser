"""
scripts/backfill_recipe_images.py — give the library back the photographs its
archive has been holding all along.

The bulk import of 2026-09-05 ran hours before c934b48 taught the pipeline to
store a Paprika entry's photo, so 768 recipes came in with image_url null and
nothing has gone back for them since.  backfill_paprika_metadata.py filled the
six text columns migration 012 added; it never touched images, which is why the
library still shows none.

This reads the original archive, matches its entries to rows by title exactly as
that script does, and for a row whose image_url is null uploads the entry's
photograph to the recipe-images bucket, then PATCHes the resulting public URL
onto the row.  An entry with no embedded photo but a remote image_url is
downloaded and stored the same way, so every picture the library serves comes
from our own bucket and cannot rot when a source site reorganises.

    # dry run -- prints what it would do, uploads nothing and writes nothing
    python scripts/backfill_recipe_images.py \\
        --archive "/path/to/Export.paprikarecipes" --user-id <uuid>

    # the real thing
    python scripts/backfill_recipe_images.py \\
        --archive "/path/to/Export.paprikarecipes" --user-id <uuid> --live

Reads SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY from the environment.

One archive per run.  Run it once per export: a row filled by the first run is
no longer null, so a later run passes over it, and pooling the archives would
only turn a recipe that appears in two of them into an ambiguous title.

This script never creates a recipe and never replaces a picture a row already
has.  It only fills the gap.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

# Running this file directly puts scripts/ on sys.path rather than the repository
# root, so `recipeparser` would not import. Make the root importable either way,
# so the command in the docstring works from a plain checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from recipeparser.io.readers.paprika import PaprikaReader, _decode_photo  # noqa: E402
from recipeparser.io.writers.image_store import SupabaseImageStore  # noqa: E402

# The title key is the matching rule, and it belongs to the backfill that worked
# it out -- the punctuation the pipeline rewrote is the same punctuation here.
from scripts.backfill_paprika_metadata import normalise_title  # noqa: E402

log = logging.getLogger("backfill-images")

#: How long to wait on a third-party image host before giving up on one picture.
FETCH_TIMEOUT = 30.0


@dataclass(frozen=True)
class ImageUpdate:
    """One row to fill, and where its picture is coming from.

    Exactly one of *photo_bytes* and *remote_url* is set: the archive's own bytes
    when it has them, the remote URL only as a fallback.
    """

    recipe_id: str
    title: str
    photo_bytes: Optional[bytes]
    content_type: str
    remote_url: Optional[str]


@dataclass
class ImageBackfillPlan:
    """What a run would do, before it does any of it."""

    updates: List[ImageUpdate] = field(default_factory=list)
    ambiguous: List[str] = field(default_factory=list)
    unmatched: List[str] = field(default_factory=list)
    untouched: int = 0


def plan_image_updates(
    entries: Sequence[Dict[str, Any]], rows: Sequence[Dict[str, Any]]
) -> ImageBackfillPlan:
    """
    Decide which rows get which picture, uploading nothing and writing nothing.

    Kept pure and separate from the network so the rules below are testable
    without a database or a storage bucket: they are the whole risk of this
    script.  Decoding the archive's base64 is pure too, so it happens here and a
    photo that will not decode is caught before any upload starts.
    """
    plan = ImageBackfillPlan()

    rows_by_title: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_title[normalise_title(row.get("title"))].append(row)

    entries_by_title: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for entry in entries:
        entries_by_title[normalise_title(entry.get("name"))].append(entry)

    for key, matched_entries in entries_by_title.items():
        matched_rows = [] if key == "" else rows_by_title.get(key, [])
        # Report the name as the archive wrote it; the key is for matching only.
        title = str(matched_entries[0].get("name") or "")

        if not matched_rows:
            # No row to fill. This script does not create recipes; an entry that
            # never made it into the library is the separate repair's business.
            plan.unmatched.append(title)
            continue

        # Two recipes of one name, on either side, leave no way to tell which
        # photograph belongs to which. A wrong picture is worse than none.
        if len(matched_rows) > 1 or len(matched_entries) > 1:
            plan.ambiguous.append(title)
            continue

        row = matched_rows[0]
        if row.get("image_url") is not None:
            # Already has a picture -- from the ingest path, or from an earlier
            # run of this script over another archive.
            plan.untouched += 1
            continue

        entry = matched_entries[0]
        # _decode_photo is the reader's own rule, base64 quirks and all: the
        # bug that made 466 photographs unusable is not worth reproducing here.
        photo_bytes, content_type = _decode_photo(entry)
        remote_url = str(entry.get("image_url") or "").strip() or None

        if photo_bytes is None and remote_url is None:
            # Nothing to give this row. Not every archive entry has a picture.
            plan.untouched += 1
            continue

        plan.updates.append(
            ImageUpdate(
                recipe_id=row["id"],
                title=title,
                photo_bytes=photo_bytes,
                content_type=content_type,
                # The bytes win when both exist, so the fallback is only ever
                # reached when there are none.
                remote_url=None if photo_bytes is not None else remote_url,
            )
        )

    return plan


# ---------------------------------------------------------------------------
# The I/O shell
# ---------------------------------------------------------------------------

def _creds() -> Tuple[str, str]:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must both be set.")
    return url, key


def fetch_rows(url: str, key: str, user_id: str) -> List[Dict[str, Any]]:
    """Every recipe this user owns, with only the columns this script reasons about."""
    resp = httpx.get(
        f"{url}/rest/v1/recipes",
        params={"user_id": f"eq.{user_id}", "select": "id,title,image_url"},
        headers={"Authorization": f"Bearer {key}", "apikey": key},
        timeout=60.0,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_remote_image(remote_url: str) -> Tuple[Optional[bytes], str]:
    """Download a third-party image, or report why it could not be had."""
    try:
        resp = httpx.get(remote_url, timeout=FETCH_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log.warning("could not fetch %s: %s", remote_url, exc)
        return None, "image/jpeg"
    # Hosts serve "image/jpeg; charset=binary" and the odd empty header alike.
    content_type = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
    if not content_type.startswith("image/"):
        log.warning("%s served %r, not an image -- skipping.", remote_url, content_type)
        return None, "image/jpeg"
    return resp.content, content_type


def apply_update(url: str, key: str, recipe_id: str, image_url: str) -> None:
    resp = httpx.patch(
        f"{url}/rest/v1/recipes",
        params={"id": f"eq.{recipe_id}"},
        headers={
            "Authorization": f"Bearer {key}",
            "apikey": key,
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
        json={"image_url": image_url},
        timeout=30.0,
    )
    resp.raise_for_status()


def store_and_record(
    url: str, key: str, store: SupabaseImageStore, update: ImageUpdate
) -> bool:
    """Upload one picture and point its row at it. True when the row was filled."""
    photo_bytes, content_type = update.photo_bytes, update.content_type
    if photo_bytes is None and update.remote_url is not None:
        photo_bytes, content_type = fetch_remote_image(update.remote_url)
    if not photo_bytes:
        return False

    public_url = store.put(photo_bytes, update.recipe_id, content_type)
    if not public_url:
        # put() logs its own reason and returns None rather than raising.
        log.error("could not store the image for %r (%s).", update.title, update.recipe_id)
        return False

    apply_update(url, key, update.recipe_id, public_url)
    return True


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill recipe images from a Paprika archive."
    )
    parser.add_argument("--archive", required=True, help="Path to the .paprikarecipes export.")
    parser.add_argument("--user-id", required=True, help="The owner whose library to fill.")
    parser.add_argument("--live", action="store_true", help="Actually upload and write. Omit for a dry run.")
    parser.add_argument("--verbose", action="store_true", help="List every row that would change.")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after this many rows. Useful for a small live run before the whole library.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    url, key = _creds()

    entries = PaprikaReader().read_entries(args.archive)
    rows = fetch_rows(url, key, args.user_id)
    plan = plan_image_updates(entries, rows)

    updates = plan.updates if args.limit is None else plan.updates[: args.limit]

    if args.verbose:
        for update in updates:
            source = "embedded" if update.photo_bytes is not None else f"remote {update.remote_url}"
            log.info("%s <- %s", update.title, source)
        for title in plan.ambiguous:
            log.warning("ambiguous, skipped: %s", title)

    embedded = sum(1 for u in updates if u.photo_bytes is not None)
    written = 0
    failed = 0
    if args.live:
        store = SupabaseImageStore(url, key)
        for update in updates:
            try:
                if store_and_record(url, key, store, update):
                    written += 1
                else:
                    failed += 1
            except Exception as exc:  # noqa: BLE001
                # One unreachable host must not abandon the other 465.
                failed += 1
                log.error("failed to fill %r (%s): %s", update.title, update.recipe_id, exc)

    # Printed on a dry run and a live run alike, so the two are comparable.
    log.info(
        "%s: %d entries, %d rows, %d to fill (%d embedded, %d remote), "
        "%d already pictured or pictureless, %d ambiguous, %d not in the library.",
        f"WROTE {written}" + (f", {failed} FAILED" if failed else "") if args.live else "DRY RUN",
        len(entries),
        len(rows),
        len(updates),
        embedded,
        len(updates) - embedded,
        plan.untouched,
        len(plan.ambiguous),
        len(plan.unmatched),
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
