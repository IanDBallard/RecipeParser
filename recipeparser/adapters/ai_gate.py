"""The AI gate (AI caps spec §2): the kill switch, the recipe cap and the search rate limit.

All three read state from Postgres through the service role (plan A's tables).
A process with no database configured (local runs and tests) allows everything
and says so once, at WARNING. When the database is configured, a missing
allowance row fails closed, as paused. Refusals are 429 and 503, never 403:
the client reads a 403 as "sign in again" (spec D10).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, Optional

from fastapi import HTTPException, status

log = logging.getLogger(__name__)

PAUSED_DETAIL = "Cayenne's AI features are paused for now. Everything else still works."
CAP_DETAIL = "You've used all {cap} of your beta recipe imports."
EMBED_RATE_DETAIL = "Searching too quickly. Try again in a moment."


@dataclass(frozen=True)
class Allowance:
    recipes_processed: int
    recipe_cap: int
    exempt: bool


class AiGate:
    def __init__(
        self,
        get_client: Callable[[], Any],
        *,
        clock: Callable[[], float] = time.monotonic,
        embed_per_minute: int = 60,
        paused_ttl: float = 30.0,
    ) -> None:
        self._get_client = get_client
        self._clock = clock
        self._embed_per_minute = embed_per_minute
        self._paused_ttl = paused_ttl
        self._paused_at: Optional[float] = None
        self._paused_value = False
        self._lock = threading.Lock()
        self._embeds: Dict[str, Deque[float]] = defaultdict(deque)
        self._warned_unconfigured = False

    def _client(self) -> Any:
        sb = self._get_client()
        if sb is None and not self._warned_unconfigured:
            self._warned_unconfigured = True
            log.warning("ai_gate: no database configured — the AI gate allows everything.")
        return sb

    def paused(self) -> bool:
        """ai_settings.paused, cached for paused_ttl seconds; a read error counts as paused."""
        now = self._clock()
        with self._lock:
            if self._paused_at is not None and now - self._paused_at < self._paused_ttl:
                return self._paused_value
        try:
            sb = self._client()
            if sb is None:
                value = False
            else:
                rows = sb.table("ai_settings").select("paused").eq("id", True).limit(1).execute().data or []
                value = bool(rows[0]["paused"]) if rows else False
        except Exception:  # noqa: BLE001
            log.exception("ai_gate: could not read ai_settings; treating AI as paused.")
            value = True
        with self._lock:
            self._paused_at, self._paused_value = now, value
        return value

    def allowance(self, user_id: str) -> Optional[Allowance]:
        sb = self._client()
        if sb is None:
            return Allowance(0, 0, True)
        rows = (
            sb.table("ai_allowance")
            .select("recipes_processed,recipe_cap,exempt")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
            .data
            or []
        )
        if not rows:
            log.error("ai_gate: no ai_allowance row for %s; refusing its AI work.", user_id)
            return None
        r = rows[0]
        return Allowance(int(r["recipes_processed"]), int(r["recipe_cap"]), bool(r["exempt"]))

    def ai_allowed(self, user_id: str) -> bool:
        """For the background workers: not paused, or the owner is exempt."""
        if not self.paused():
            return True
        try:
            row = self.allowance(user_id)
        except Exception:  # noqa: BLE001
            log.exception("ai_gate: could not read the allowance for %s.", user_id)
            return False
        return row is not None and row.exempt

    def check(self, user_id: str, *, imports: bool = False, embed: bool = False) -> None:
        try:
            row = self.allowance(user_id)
        except Exception:  # noqa: BLE001
            log.exception("ai_gate: could not read the allowance for %s; refusing.", user_id)
            row = None
        if row is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=PAUSED_DETAIL)
        if not row.exempt and self.paused():
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=PAUSED_DETAIL)
        if imports and not row.exempt and row.recipes_processed >= row.recipe_cap:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, detail=CAP_DETAIL.format(cap=row.recipe_cap))
        if embed and not self._embed_slot(user_id):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, detail=EMBED_RATE_DETAIL)

    def _embed_slot(self, user_id: str) -> bool:
        now = self._clock()
        with self._lock:
            window = self._embeds[user_id]
            while window and now - window[0] >= 60.0:
                window.popleft()
            if len(window) >= self._embed_per_minute:
                return False
            window.append(now)
            return True

    def consume_recipe(self, user_id: str) -> bool:
        """Spend one recipe of the allowance; false at the cap, and false on any error (fail closed)."""
        try:
            sb = self._client()
            if sb is None:
                return True
            return bool(sb.rpc("consume_recipe_allowance", {"p_user": user_id}).execute().data)
        except Exception:  # noqa: BLE001
            log.exception("ai_gate: consume_recipe_allowance failed for %s; stopping at the cap.", user_id)
            return False
