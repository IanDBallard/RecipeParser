"""
scripts/clear_book_non_photos.py -- take the ornaments and blank crops off the
recipes a cookbook import pictured with them (Fix Roadmap F-203).

Until 9.4.1 a PDF or EPUB offered the model every image on a page over
MIN_PHOTO_BYTES as the recipe's photo, so a library imported from books holds
recipes pictured with a blank white page crop, a paper texture, or a black
ornament ("Perfect Minestrone", 2026-10-02). The readers now refuse those
(recipeparser/io/readers/photo_check.py). This applies the same test to the
pictures already stored, and clears the ones it refuses.

    # dry run -- lists what it would clear and why, writes nothing
    python scripts/clear_book_non_photos.py --user-id <uuid>

    # the real thing, keeping a record of every picture it clears
    python scripts/clear_book_non_photos.py --user-id <uuid> --live --record cleared.csv

Reads SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY from the environment.

Only a recipe whose source is a book (source_kind 'book') and whose picture is
not AI-generated (image_source null) is looked at: a web page's picture came
from its og:image, and a generated one was asked for. A picture the cook chose
for a book recipe is looked at too -- the database does not say who set it --
but a photograph passes the test, so only one that is itself a blank or an
ornament is cleared.

Clearing sets image_url to null and nothing else. The stored file is left in
the bucket, and --record writes each cleared row's id, title, URL and reason,
so a wrong call is undone by writing the URL back.
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import httpx

# Running this file directly puts scripts/ on sys.path rather than the repository
# root, so `recipeparser` would not import. Make the root importable either way.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from recipeparser.io.readers.photo_check import photo_refusal  # noqa: E402

log = logging.getLogger("clear-book-non-photos")

FETCH_TIMEOUT = 30.0


@dataclass(frozen=True)
class Clear:
    recipe_id: str
    title: str
    image_url: str
    reason: str


@dataclass
class Plan:
    clears: List[Clear]
    kept: int
    unreadable: List[Tuple[str, str]]  # (title, image_url) whose picture could not be fetched


def is_candidate(row: Dict[str, Any]) -> bool:
    """A book recipe with a picture that is not AI-generated."""
    return bool(row.get("image_url")) and row.get("image_source") is None and row.get("source_kind") == "book"


def plan_clears(rows: Sequence[Dict[str, Any]], fetch: Callable[[str], Optional[bytes]]) -> Plan:
    """Which candidate rows' pictures the readers' photo test refuses. No writes."""
    plan = Plan(clears=[], kept=0, unreadable=[])
    for row in rows:
        if not is_candidate(row):
            continue
        data = fetch(row["image_url"])
        if data is None:
            plan.unreadable.append((row.get("title") or row["id"], row["image_url"]))
            continue
        reason = photo_refusal(data)
        if reason is None:
            plan.kept += 1
        else:
            plan.clears.append(Clear(row["id"], row.get("title") or row["id"], row["image_url"], reason))
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
    """This user's pictured book recipes, with only the columns this script reasons about."""
    resp = httpx.get(
        f"{url}/rest/v1/recipes",
        params={
            "user_id": f"eq.{user_id}",
            "source_kind": "eq.book",
            "image_url": "not.is.null",
            "select": "id,title,image_url,image_source,source_kind",
        },
        headers={"Authorization": f"Bearer {key}", "apikey": key},
        timeout=60.0,
    )
    resp.raise_for_status()
    rows: List[Dict[str, Any]] = resp.json()
    return rows


def fetch_image(image_url: str) -> Optional[bytes]:
    try:
        resp = httpx.get(image_url, timeout=FETCH_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 -- one dead URL must not stop the run
        log.warning("could not fetch %s: %s", image_url, exc)
        return None
    return resp.content


def clear_picture(url: str, key: str, recipe_id: str) -> None:
    resp = httpx.patch(
        f"{url}/rest/v1/recipes",
        # Only while it is still null-sourced: a picture generated since the dry run is not this script's.
        params={"id": f"eq.{recipe_id}", "image_source": "is.null"},
        headers={
            "Authorization": f"Bearer {key}",
            "apikey": key,
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
        json={"image_url": None},
        timeout=30.0,
    )
    resp.raise_for_status()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Clear book recipes' pictures that are not photographs (F-203).")
    parser.add_argument("--user-id", required=True, help="The owner whose library to check.")
    parser.add_argument("--live", action="store_true", help="Actually clear. Omit for a dry run.")
    parser.add_argument("--record", help="CSV to write every cleared row to (id, title, image_url, reason).")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.live and not args.record:
        raise SystemExit("--live needs --record: the record is how a wrong call is undone.")
    url, key = _creds()

    plan = plan_clears(fetch_rows(url, key, args.user_id), fetch_image)
    for clear in plan.clears:
        log.info("%s: %s", clear.title, clear.reason)
    for title, image_url in plan.unreadable:
        log.warning("could not read the picture of %s (%s), left as it is.", title, image_url)

    cleared = failed = 0
    if args.live:
        with open(args.record, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["id", "title", "image_url", "reason"])
            for clear in plan.clears:
                try:
                    clear_picture(url, key, clear.recipe_id)
                except Exception as exc:  # noqa: BLE001 -- report it and carry on with the rest
                    failed += 1
                    log.error("could not clear %r (%s): %s", clear.title, clear.recipe_id, exc)
                    continue
                writer.writerow([clear.recipe_id, clear.title, clear.image_url, clear.reason])
                cleared += 1

    log.info(
        "%s: %d to clear, %d kept as photographs, %d unreadable.",
        f"CLEARED {cleared}" + (f", {failed} FAILED" if failed else "") if args.live else "DRY RUN",
        len(plan.clears),
        plan.kept,
        len(plan.unreadable),
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
