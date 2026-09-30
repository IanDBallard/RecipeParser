"""
recipeparser/adapters/shares_api.py — the recipe-sharing endpoints (Cayenne's recipe-sharing
design, Part 2).

A share hands another Cayenne user a copy of some recipes; they accept it, all or some, and
the copies become theirs. The client reads both queues by sync and asks for every change
here. Each change is one Cayenne database function, executable by the service role alone
(the database plan, ruling 1): supabase-py sends one request per call, so it cannot hold a
transaction, and the function is the transaction.

Kept out of api.py, which is already long and has a refactor queued (Fix Roadmap F-068).
``build_router`` takes the auth dependency and the client factory rather than importing
them, so this module never imports api.py and api.py stays the only place they are defined.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

log = logging.getLogger(__name__)

CHECKS_PER_HOUR = 20
MAX_RECIPES = 200

NO_ACCOUNT = "No Cayenne account uses that email."
OWN_EMAIL = "That's your own email."
TOO_MANY_CHECKS = "Too many checks. Try again in an hour."
NOT_CONFIGURED = "Sharing is not configured on this server."
UNREACHABLE = "Could not reach the share queue. Try again."
RECIPES_NOT_FOUND = "One or more of those recipes were not found."
NOT_PENDING = "This share is no longer pending."
ALL_SKIPPED = "Choose at least one recipe, or decline the share."
SHARE_NOT_FOUND = "Share not found."


class RecipientCheckLimiter:
    """D6's limit: at most ``limit`` recipient lookups per sender in any ``window`` seconds.

    In process memory. The API is one container on one VM, so a restart forgets the counts;
    that is acceptable for a limit whose job is to slow enumeration, not to stop it (Part 2).
    Endpoints are plain ``def``s that FastAPI runs on a thread pool, hence the lock.
    """

    def __init__(self, limit: int = CHECKS_PER_HOUR, window: float = 3600.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._limit, self._window, self._clock = limit, window, clock
        self._hits: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, sender_id: str) -> bool:
        now = self._clock()
        with self._lock:
            hits = self._hits.setdefault(sender_id, deque())
            while hits and hits[0] <= now - self._window:
                hits.popleft()
            if len(hits) >= self._limit:
                return False
            hits.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


def normalise_email(raw: str) -> str:
    return raw.strip().lower()


class RecipientRequest(BaseModel):
    email: str


class RecipientResponse(BaseModel):
    email: str


class ShareRequest(BaseModel):
    email: str
    recipe_ids: List[uuid.UUID]


class ShareCreated(BaseModel):
    share_id: str


class AcceptRequest(BaseModel):
    skip_item_ids: List[uuid.UUID] = []


class AcceptResponse(BaseModel):
    job_id: str


class ClosedResponse(BaseModel):
    status: str


def build_router(
    verify: Callable[..., Dict[str, Any]],
    service_client: Callable[[], Any],
    limiter: RecipientCheckLimiter,
) -> APIRouter:
    """The five sharing endpoints. ``verify`` is the auth dependency; ``service_client`` returns the
    service-role client or None when the server has none."""
    router = APIRouter()

    def _client() -> Any:
        sb = service_client()
        if sb is None:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=NOT_CONFIGURED)
        return sb

    def _rpc(sb: Any, fn: str, params: Dict[str, Any], *, invalid: Optional[str] = None) -> Any:
        """Call a sharing function. SQLSTATE 22023 is the functions' refusal of an argument:
        a 422 carrying ``invalid`` where the caller expects one. Anything else is a 503."""
        try:
            return sb.rpc(fn, params).execute().data
        except Exception as exc:  # noqa: BLE001
            if invalid is not None and getattr(exc, "code", None) == "22023":
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=invalid) from exc
            log.exception("Sharing function %s failed.", fn)
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=UNREACHABLE) from exc

    def _recipient(sb: Any, user: Dict[str, Any], raw_email: str) -> Tuple[str, str]:
        """D6: (the normalised address, its account's id), or the refusal Part 2's table gives."""
        email = normalise_email(raw_email)
        if "@" not in email:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Enter an email address.")
        if email == normalise_email(user.get("email") or ""):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=OWN_EMAIL)
        # Counted only once a lookup is about to run: a malformed address or one's own teaches
        # nothing about who has an account, so neither spends one of the twenty.
        if not limiter.allow(user["sub"]):
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=TOO_MANY_CHECKS)
        found = _rpc(sb, "user_id_for_email", {"p_email": email})
        if not found:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NO_ACCOUNT)
        if str(found) == user["sub"]:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=OWN_EMAIL)
        return email, str(found)

    def _refusal(sb: Any, share_id: str, user_id: str, role: str) -> HTTPException:
        """Why a transition touched nothing. 404 unless the caller is this share's ``role``
        (``sender_id`` or ``recipient_id``), so a share id cannot be probed; 409 otherwise."""
        try:
            rows = (sb.table("recipe_shares").select("sender_id,recipient_id")
                    .eq("id", share_id).limit(1).execute().data or [])
        except Exception:  # noqa: BLE001
            log.exception("Could not read share %s after a refused transition.", share_id)
            return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=UNREACHABLE)
        if not rows or rows[0].get(role) != user_id:
            return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=SHARE_NOT_FOUND)
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=NOT_PENDING)

    @router.post("/shares/recipient", response_model=RecipientResponse)
    def check_recipient(body: RecipientRequest, user: Dict[str, Any] = Depends(verify)) -> RecipientResponse:
        """D6: does a Cayenne account use this address? Asked before a share is created."""
        email, _ = _recipient(_client(), user, body.email)
        return RecipientResponse(email=email)

    @router.post("/shares", response_model=ShareCreated, status_code=status.HTTP_201_CREATED)
    def create_share(body: ShareRequest, user: Dict[str, Any] = Depends(verify)) -> ShareCreated:
        """Create a share of the caller's recipes. The recipient is checked again: the account
        may have gone since *Next*. 404, naming no id, if any recipe is not the caller's."""
        ids = list(dict.fromkeys(str(i) for i in body.recipe_ids))
        if not ids:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail="Choose at least one recipe to share.")
        if len(ids) > MAX_RECIPES:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail=f"A share holds at most {MAX_RECIPES} recipes.")
        sender_email = normalise_email(user.get("email") or "")
        if not sender_email:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail="Your account has no email address to share from.")
        sb = _client()
        email, recipient = _recipient(sb, user, body.email)
        share_id = _rpc(sb, "create_recipe_share", {
            "p_sender": user["sub"], "p_sender_email": sender_email,
            "p_recipient": recipient, "p_recipient_email": email, "p_recipe_ids": ids,
        })
        if not share_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=RECIPES_NOT_FOUND)
        log.info("Share %s created (%d recipe(s)).", share_id, len(ids))
        return ShareCreated(share_id=str(share_id))

    @router.post("/shares/{share_id}/accept", response_model=AcceptResponse, status_code=status.HTTP_202_ACCEPTED)
    def accept_share(share_id: uuid.UUID, body: Optional[AcceptRequest] = None,
                     user: Dict[str, Any] = Depends(verify)) -> AcceptResponse:
        """Accept all but the listed items. The copy runs as a ``share_accept`` job (Part 3)."""
        sb = _client()
        skip = [str(i) for i in (body.skip_item_ids if body else [])]
        job_id = _rpc(sb, "accept_recipe_share",
                      {"p_share": str(share_id), "p_recipient": user["sub"], "p_skip": skip},
                      invalid=ALL_SKIPPED)
        if not job_id:
            raise _refusal(sb, str(share_id), user["sub"], "recipient_id")
        log.info("Share %s accepted; copy job %s queued.", share_id, job_id)
        return AcceptResponse(job_id=str(job_id))

    @router.post("/shares/{share_id}/decline", response_model=ClosedResponse)
    def decline_share(share_id: uuid.UUID, user: Dict[str, Any] = Depends(verify)) -> ClosedResponse:
        """The recipient turns a pending share down (D7)."""
        sb = _client()
        if not _rpc(sb, "close_recipe_share",
                    {"p_share": str(share_id), "p_actor": user["sub"], "p_status": "declined"}):
            raise _refusal(sb, str(share_id), user["sub"], "recipient_id")
        return ClosedResponse(status="declined")

    @router.post("/shares/{share_id}/cancel", response_model=ClosedResponse)
    def cancel_share(share_id: uuid.UUID, user: Dict[str, Any] = Depends(verify)) -> ClosedResponse:
        """The sender withdraws a share while it is still pending (D7)."""
        sb = _client()
        if not _rpc(sb, "close_recipe_share",
                    {"p_share": str(share_id), "p_actor": user["sub"], "p_status": "cancelled"}):
            raise _refusal(sb, str(share_id), user["sub"], "sender_id")
        return ClosedResponse(status="cancelled")

    return router
