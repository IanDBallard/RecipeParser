"""
recipeparser/adapters/idempotency.py: one answer per Idempotency-Key (Cayenne Fix Roadmap F-200).

A cook whose request passed the device's deadline is told it "took too long", but the server may
already have made the job or the share. Pressing again would make a second one. The device sends
the same key for the same intent until it has an answer, and this cache hands a repeat the first
answer instead of running it again.

In process memory, like RecipientCheckLimiter: the API is one container on one VM, so a restart
forgets the keys, which reopens a window of seconds that #186's review already judged unlikely.
Endpoints may be plain ``def``s on FastAPI's thread pool, hence the lock.
"""
from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, Optional, Tuple

from fastapi import HTTPException, status

HEADER = "Idempotency-Key"
BAD_KEY = "The Idempotency-Key header must be 8 to 128 letters, digits or hyphens."
IN_FLIGHT = "That request is still being handled. Wait a moment and look again."

_KEY = re.compile(r"[A-Za-z0-9-]{8,128}")

# (when it was claimed or finished, the stored answer or None while pending, the claim that owns it)
Entry = Tuple[float, Optional[Dict[str, Any]], Optional[object]]


class Slot:
    """What ``once`` hands its block: the stored answer to replay, or a place to record one."""

    def __init__(self, user_id: str, key: Optional[str], token: Optional[object],
                 replay: Optional[Dict[str, Any]], cache: Optional["IdempotencyCache"]) -> None:
        self._user_id, self._key, self._token, self._cache = user_id, key, token, cache
        self.replay = replay
        self.finished = False
        self._answer: Optional[Dict[str, Any]] = None

    def done(self, answer: Dict[str, Any]) -> None:
        """Record the answer. It is stored only if the block then exits without an exception."""
        self.finished = True
        self._answer = dict(answer)


class IdempotencyCache:
    def __init__(self, ttl: float = 86_400.0, pending_ttl: float = 600.0, max_entries: int = 10_000,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl, self._pending_ttl, self._max, self._clock = ttl, pending_ttl, max_entries, clock
        self._entries: "OrderedDict[Tuple[str, str], Entry]" = OrderedDict()
        self._lock = threading.Lock()

    def _live(self, entry: Entry, now: float) -> bool:
        stamp, answer, _ = entry
        return now - stamp < (self._pending_ttl if answer is None else self._ttl)

    def _claim(self, user_id: str, key: str) -> Tuple[Optional[Dict[str, Any]], object]:
        """The stored answer (a copy), or None with a fresh claim token. 409 while another runs."""
        now = self._clock()
        token = object()
        with self._lock:
            entry = self._entries.get((user_id, key))
            if entry is not None and self._live(entry, now):
                if entry[1] is None:
                    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=IN_FLIGHT)
                return dict(entry[1]), token
            self._entries[(user_id, key)] = (now, None, token)
            self._entries.move_to_end((user_id, key))
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)
            return None, token

    def _commit(self, user_id: str, key: str, token: object, answer: Dict[str, Any]) -> None:
        """Store the answer, but only if this claim still owns the key (it may have expired or been evicted)."""
        with self._lock:
            entry = self._entries.get((user_id, key))
            if entry is not None and entry[2] is token:
                self._entries[(user_id, key)] = (self._clock(), dict(answer), None)
                self._entries.move_to_end((user_id, key))

    def _release(self, user_id: str, key: str, token: object) -> None:
        """Free the key, but only if this claim still owns it."""
        with self._lock:
            entry = self._entries.get((user_id, key))
            if entry is not None and entry[2] is token:
                del self._entries[(user_id, key)]

    @contextmanager
    def once(self, user_id: str, key: Optional[str]) -> Iterator[Slot]:
        """Run the block once per (user, key). No key: the block runs as it always did."""
        if key is None:
            yield Slot(user_id, None, None, None, None)
            return
        if not _KEY.fullmatch(key):
            # A literal: the HTTP_422_UNPROCESSABLE_ENTITY name is deprecated in newer Starlette.
            raise HTTPException(status_code=422, detail=BAD_KEY)
        replay, token = self._claim(user_id, key)
        slot = Slot(user_id, key, token, replay, self)
        if replay is not None:
            yield slot
            return
        try:
            yield slot
        except BaseException:
            self._release(user_id, key, token)
            raise
        if slot._answer is not None:
            self._commit(user_id, key, token, slot._answer)
        else:
            self._release(user_id, key, token)

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()
