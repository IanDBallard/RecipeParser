"""
scripts/extract_unmatched_paprika.py — write the Paprika archive entries the bulk
import lost into a smaller archive, for one re-import through Add Recipe.

Stage C (2026-09-12) found 68 archive entries with no recipe row and only
counted them: `restore_source_urls.py` restores URLs and never creates a recipe.
Re-importing the whole export would duplicate the other 759. This writes the
unmatched members — the original gzip bytes, untouched — into
`<archive stem>-unmatched.paprikarecipes` beside the archive and prints their
titles; the cook drops that one file on Add Recipe.

    # dry run -- lists the titles, writes nothing
    python scripts/extract_unmatched_paprika.py --archive "/path/to/Export.paprikarecipes" --user-id <uuid>

    # write the archive
    python scripts/extract_unmatched_paprika.py --archive "/path/to/Export.paprikarecipes" --user-id <uuid> --write

Reads SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY from the environment. The
matching rule is the restore script's: a title reduced to letters and digits.
An entry whose title matches no row is unmatched, whatever else shares its title.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.backfill_paprika_metadata import normalise_title  # noqa: E402

PAGE = 500
Member = Tuple[str, bytes, Dict[str, Any]]


def _creds() -> Tuple[str, str]:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must both be set.")
    return url, key


def read_members(path: Path | str) -> List[Member]:
    """Every .paprikarecipe member: its name, its raw bytes, and the entry it decodes to.

    The decoding is PaprikaReader.read_entries's (gzip then JSON; plain JSON for the
    exporters that skip gzip); the reader itself drops the member name, which the
    output archive needs.
    """
    path = Path(path)
    if not zipfile.is_zipfile(path):
        raise ValueError(f"Not a valid ZIP archive: {path}")
    members: List[Member] = []
    with zipfile.ZipFile(path, "r") as zf:
        for name in zf.namelist():
            if not name.lower().endswith(".paprikarecipe"):
                continue
            raw = zf.read(name)
            try:
                entry = json.loads(gzip.decompress(raw))
            except gzip.BadGzipFile:
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    print(f"  skipping {name!r}: not gzip or JSON")
                    continue
            except json.JSONDecodeError:
                print(f"  skipping {name!r}: JSON decode error")
                continue
            except Exception as exc:  # noqa: BLE001
                print(f"  skipping {name!r}: {exc}")
                continue
            members.append((name, raw, entry))
    return members


def select_unmatched(members: Sequence[Member], rows: Sequence[Dict[str, Any]]) -> List[Member]:
    """The members whose title matches no row. A blank title matches nothing and is skipped."""
    rows_by_title: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_title[normalise_title(row.get("title"))].append(row)
    unmatched: List[Member] = []
    for name, raw, entry in members:
        key = normalise_title(entry.get("name"))
        if key == "":
            continue
        if not rows_by_title.get(key):
            unmatched.append((name, raw, entry))
    return unmatched


def write_archive(out_path: Path | str, members: Sequence[Member]) -> int:
    """A .paprikarecipes holding the members, bytes as they were. Returns how many."""
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, raw, _entry in members:
            zf.writestr(name, raw)
    return len(members)


def main() -> int:
    ap = argparse.ArgumentParser(description="Write a Paprika archive's unmatched entries into their own archive.")
    ap.add_argument("--archive", required=True, help="The .paprikarecipes export.")
    ap.add_argument("--user-id", required=True)
    ap.add_argument("--out", help="Output path; default <archive stem>-unmatched.paprikarecipes beside the archive.")
    ap.add_argument("--write", action="store_true", help="Actually write the archive. Omit to list only.")
    args = ap.parse_args()
    from supabase import create_client  # noqa: PLC0415
    sb = create_client(*_creds())

    rows: List[Dict[str, Any]] = []
    offset = 0
    while True:
        page = (sb.table("recipes").select("id,title").eq("user_id", args.user_id)
                .order("id").range(offset, offset + PAGE - 1).execute().data or [])
        if not page:
            break
        rows.extend(page)
        offset += PAGE

    archive = Path(args.archive)
    members = read_members(archive)
    unmatched = select_unmatched(members, rows)
    for _name, _raw, entry in unmatched:
        print(f"  {entry.get('name')!r}")
    print(f"{len(members)} archive entries, {len(rows)} rows; {len(unmatched)} unmatched")
    if not args.write:
        print("LISTED ONLY — nothing was written. Re-run with --write to produce the archive.")
        return 0
    out = Path(args.out) if args.out else archive.with_name(f"{archive.stem}-unmatched.paprikarecipes")
    written = write_archive(out, unmatched)
    print(f"wrote {written} entries to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())