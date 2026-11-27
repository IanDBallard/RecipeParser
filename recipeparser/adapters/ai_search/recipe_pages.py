"""The look's recipe check (Cayenne F-866): a page that declares no recipe is not offered.

The look's prompt asks the agent to skip lists and round-ups, and the links lane keeps whatever
Brave returns, so a look could offer "13 Crowd-Pleasing Christmas Dinner Sides" and the import then
failed with "Nothing there looked like a recipe" (owner's holiday test, 2026-11-26).

Before the look answers, every surviving candidate page, from both lanes, is fetched here with the
URL import's own direct fetch (``fetch_page_html``: its browser user-agent, page proxy and
private-address checks) and read with its own JSON-LD reader (``find_json_ld_recipe``). A page whose
HTML declares no schema.org Recipe is dropped. A page that cannot be read (a timeout, an error
status, a refusal, a challenge page, or still loading when the check's time is up) is kept: the
import says why when the cook adds it.

The pages are fetched in parallel, each with ``PAGE_TIMEOUT_S`` for the connect and each read, and
the whole check waits at most ``TOTAL_TIMEOUT_S``. A fetch still running then is abandoned to its
own timeout on its worker thread; its page is kept.
"""

from __future__ import annotations

import concurrent.futures
import logging
from collections.abc import Callable, Sequence

from recipeparser.adapters.ai_search.look import RawCandidate
from recipeparser.exceptions import UrlFetchError
from recipeparser.io.readers.url import fetch_page_html, find_json_ld_recipe

logger = logging.getLogger(__name__)

PAGE_TIMEOUT_S = 3.0
TOTAL_TIMEOUT_S = 4.0
WORKER_NAME = "search-look-pages"

#: Fetches one page: (address, timeout in seconds) -> its HTML; raises ``UrlFetchError`` when it cannot.
PageFetcher = Callable[[str, float], str]


def _declares_recipe(fetch: PageFetcher, url: str, timeout: float) -> bool:
    return find_json_ld_recipe(fetch(url, timeout)) is not None


def keep_recipe_pages(
    candidates: Sequence[RawCandidate],
    *,
    fetch: PageFetcher = fetch_page_html,
    page_timeout: float = PAGE_TIMEOUT_S,
    total_timeout: float = TOTAL_TIMEOUT_S,
) -> list[RawCandidate]:
    """The candidates whose page declares a schema.org Recipe or could not be read, in their order."""
    if not candidates:
        return []
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=len(candidates), thread_name_prefix=WORKER_NAME)
    try:
        futures = [executor.submit(_declares_recipe, fetch, c.url, page_timeout) for c in candidates]
        concurrent.futures.wait(futures, timeout=total_timeout)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    kept: list[RawCandidate] = []
    unread = 0
    for candidate, future in zip(candidates, futures):
        if not future.done() or future.cancelled():
            unread += 1
            kept.append(candidate)
            continue
        error = future.exception()
        if error is None:
            if future.result():
                kept.append(candidate)
            continue
        unread += 1
        kept.append(candidate)
        if not isinstance(error, UrlFetchError):
            # Not a fetch failure, so a bug: the page is kept as an unread one, and the traceback logged.
            logger.warning("search look: the recipe check failed: %s", type(error).__name__, exc_info=error)
    dropped = len(candidates) - len(kept)
    logger.info(
        "search look: recipe check of %d pages dropped %d without a recipe, kept %d unread",
        len(candidates),
        dropped,
        unread,
    )
    return kept
