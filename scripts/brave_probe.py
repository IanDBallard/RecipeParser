"""One-off probe: does Brave honour ``site: OR site:`` in a links query?

Usage: python scripts/brave_probe.py <env_file>
Reads BRAVE_SEARCH_API_KEY from the env file (never printed) and prints each result's host.
"""

from __future__ import annotations

import sys
from urllib.parse import urlparse

from dotenv import dotenv_values

from recipeparser.adapters.brave import BraveSearch, links_query

QUERIES = ["shakshuka", "beef bourguignon", "french onion soup"]
SITES = ["seriouseats.com", "theguardian.com", "bbcgoodfood.com"]


def main(env_file: str) -> None:
    key = dotenv_values(env_file).get("BRAVE_SEARCH_API_KEY")
    if not key:
        raise SystemExit("BRAVE_SEARCH_API_KEY not found in the env file")
    brave = BraveSearch(key)
    for q in QUERIES:
        results = brave.search(links_query(q, SITES))
        print(q)
        for r in results:
            print("  ", urlparse(r.url).hostname)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: brave_probe.py <env_file>")
    main(sys.argv[1])
