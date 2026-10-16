"""
recipeparser/adapters/household_api.py — the household endpoints (Cayenne's household shopping
design, Part 3; Stage 3b).

A household shares one shopping list between Cayenne accounts. Membership changes, invites and
Copy to my library are each one Cayenne database function, executable by the service role
alone (Stage 3a): supabase-py sends one request per call, so it cannot hold a transaction, and
the function is the transaction.

Kept out of api.py, as shares_api.py is. ``build_router`` takes the auth dependency and the
client factory rather than importing them, so this module never imports api.py.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Callable, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel

from recipeparser.adapters.idempotency import HEADER as IDEMPOTENCY_HEADER
from recipeparser.adapters.idempotency import IdempotencyCache
from recipeparser.adapters.shares_api import (
    NO_ACCOUNT,
    OWN_EMAIL,
    TOO_MANY_CHECKS,
    RecipientCheckLimiter,
    normalise_email,
)

log = logging.getLogger(__name__)

# A literal, as idempotency.py does: the HTTP_422_UNPROCESSABLE_ENTITY name is deprecated in newer Starlette.
HTTP_422 = 422

NOT_CONFIGURED = "Households are not configured on this server."
UNREACHABLE = "Could not reach the household service. Try again."
NO_EMAIL = "Your account has no email address."
ENTER_EMAIL = "Enter an email address."
ALREADY_MEMBER = "They're already in your household."
ALREADY_INVITED = "They already have an invite to your household."
HOUSEHOLD_FULL = "A household has at most 8 people, counting invites."
INVITE_NOT_OPEN = "This invite is no longer open."
ONLY_ONE = "You're the only one in your household."
NOT_A_MEMBER = "They're not in your household."
NOT_COPYABLE = "This recipe is no longer pinned in your household."

# (SQLSTATE, message) -> (HTTP status, sentence): Stage 3a's refusal contract (plan ruling 7).
# A message of None matches any message with that code.
Refusals = Dict[Tuple[str, Optional[str]], Tuple[int, str]]


class InviteRequest(BaseModel):
    email: str


class InviteCreated(BaseModel):
    invite_id: str


class HouseholdResponse(BaseModel):
    household_id: str


class ClosedResponse(BaseModel):
    status: str


class CopyRequest(BaseModel):
    recipe_id: uuid.UUID


class CopyResponse(BaseModel):
    job_id: str


def build_router(
    verify: Callable[..., Dict[str, Any]],
    service_client: Callable[[], Any],
    limiter: RecipientCheckLimiter,
    idempotency: IdempotencyCache,
) -> APIRouter:
    """The six household endpoints. ``limiter`` is the sharing endpoints' own, so the device's
    recipient check and the invite share one budget (sharing D6)."""
    router = APIRouter()

    def _client() -> Any:
        sb = service_client()
        if sb is None:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=NOT_CONFIGURED)
        return sb

    def _rpc(sb: Any, fn: str, params: Dict[str, Any], refusals: Optional[Refusals] = None) -> Any:
        try:
            return sb.rpc(fn, params).execute().data
        except Exception as exc:  # noqa: BLE001
            code, message = getattr(exc, "code", None), getattr(exc, "message", None)
            for (want_code, want_message), (http, detail) in (refusals or {}).items():
                if code == want_code and (want_message is None or message == want_message):
                    raise HTTPException(status_code=http, detail=detail) from exc
            log.exception("Household function %s failed.", fn)
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=UNREACHABLE) from exc

    def _own_email(user: Dict[str, Any]) -> str:
        email = normalise_email(user.get("email") or "")
        if not email:
            raise HTTPException(status_code=HTTP_422, detail=NO_EMAIL)
        return email

    def _invitee(sb: Any, user: Dict[str, Any], raw_email: str) -> Tuple[str, str]:
        email = normalise_email(raw_email)
        if "@" not in email:
            raise HTTPException(status_code=HTTP_422, detail=ENTER_EMAIL)
        if email == normalise_email(user.get("email") or ""):
            raise HTTPException(status_code=HTTP_422, detail=OWN_EMAIL)
        if not limiter.allow(user["sub"]):
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=TOO_MANY_CHECKS)
        found = _rpc(sb, "user_id_for_email", {"p_email": email})
        if not found:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NO_ACCOUNT)
        if str(found) == user["sub"]:
            raise HTTPException(status_code=HTTP_422, detail=OWN_EMAIL)
        return email, str(found)

    @router.post("/household/invites", response_model=InviteCreated, status_code=status.HTTP_201_CREATED)
    def create_invite(body: InviteRequest, user: Dict[str, Any] = Depends(verify),
                      idempotency_key: Optional[str] = Header(default=None, alias=IDEMPOTENCY_HEADER)) -> InviteCreated:
        """Invite an account into the caller's household (Part 4's Invite). The device asks
        /shares/recipient first; the lookup here is the fallback for an account gone since."""
        inviter_email = _own_email(user)
        with idempotency.once(user["sub"], idempotency_key) as slot:
            # Before the lookup: a replay must not spend one of the caller's twenty checks.
            if slot.replay is not None:
                return InviteCreated(**slot.replay)
            sb = _client()
            email, invitee = _invitee(sb, user, body.email)
            invite_id = _rpc(sb, "create_household_invite", {
                "p_inviter": user["sub"], "p_inviter_email": inviter_email,
                "p_invitee": invitee, "p_invitee_email": email,
            }, {
                ("23505", "already a member"): (status.HTTP_409_CONFLICT, ALREADY_MEMBER),
                ("23505", "already invited"): (status.HTTP_409_CONFLICT, ALREADY_INVITED),
                ("23514", "household full"): (HTTP_422, HOUSEHOLD_FULL),
                ("22023", None): (HTTP_422, OWN_EMAIL),
            })
            if not invite_id:
                raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=UNREACHABLE)
            log.info("Household invite %s created.", invite_id)
            slot.done({"invite_id": str(invite_id)})
            return InviteCreated(invite_id=str(invite_id))

    @router.post("/household/invites/{invite_id}/accept", response_model=HouseholdResponse)
    def accept_invite(invite_id: uuid.UUID, user: Dict[str, Any] = Depends(verify)) -> HouseholdResponse:
        """Join the inviting household (D5). The device has already waited for its upload queue (D14)."""
        household = _rpc(_client(), "accept_household_invite", {"p_invite": str(invite_id), "p_invitee": user["sub"]})
        if not household:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=INVITE_NOT_OPEN)
        return HouseholdResponse(household_id=str(household))

    def _close(invite_id: uuid.UUID, actor: str, status_word: str) -> ClosedResponse:
        if not _rpc(_client(), "close_household_invite",
                    {"p_invite": str(invite_id), "p_actor": actor, "p_status": status_word}):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=INVITE_NOT_OPEN)
        return ClosedResponse(status=status_word)

    @router.post("/household/invites/{invite_id}/decline", response_model=ClosedResponse)
    def decline_invite(invite_id: uuid.UUID, user: Dict[str, Any] = Depends(verify)) -> ClosedResponse:
        """The invitee turns an invite down."""
        return _close(invite_id, user["sub"], "declined")

    @router.post("/household/invites/{invite_id}/cancel", response_model=ClosedResponse)
    def cancel_invite(invite_id: uuid.UUID, user: Dict[str, Any] = Depends(verify)) -> ClosedResponse:
        """Any member of the inviting household withdraws a pending invite (D4)."""
        return _close(invite_id, user["sub"], "cancelled")

    @router.post("/household/leave", response_model=HouseholdResponse)
    def leave(user: Dict[str, Any] = Depends(verify)) -> HouseholdResponse:
        """Leave for a new household of one with a copy of the preferences (D6)."""
        household = _rpc(_client(), "leave_household", {"p_user": user["sub"], "p_actor": user["sub"]},
                         {("23514", "household of one"): (status.HTTP_409_CONFLICT, ONLY_ONE)})
        if not household:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_A_MEMBER)
        return HouseholdResponse(household_id=str(household))

    @router.post("/household/members/{member_id}/remove", response_model=HouseholdResponse)
    def remove(member_id: uuid.UUID, user: Dict[str, Any] = Depends(verify)) -> HouseholdResponse:
        """Any member removes another (D4); the removed member leaves as D6 says. Not yourself:
        that is /household/leave."""
        if str(member_id) == user["sub"]:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_A_MEMBER)
        household = _rpc(_client(), "leave_household", {"p_user": str(member_id), "p_actor": user["sub"]},
                         {("23514", "household of one"): (status.HTTP_404_NOT_FOUND, NOT_A_MEMBER)})
        if not household:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_A_MEMBER)
        return HouseholdResponse(household_id=str(household))

    @router.post("/household/copy", response_model=CopyResponse, status_code=status.HTTP_202_ACCEPTED)
    def copy(body: CopyRequest, user: Dict[str, Any] = Depends(verify),
             idempotency_key: Optional[str] = Header(default=None, alias=IDEMPOTENCY_HEADER)) -> CopyResponse:
        """Copy a member's pinned recipe into the caller's library (D11): one call, one transaction,
        a share accepted at once, so the copy runs as an unchanged share_accept job."""
        caller_email = _own_email(user)
        with idempotency.once(user["sub"], idempotency_key) as slot:
            if slot.replay is not None:
                return CopyResponse(**slot.replay)
            job_id = _rpc(_client(), "copy_household_recipe", {
                "p_caller": user["sub"], "p_caller_email": caller_email, "p_recipe": str(body.recipe_id),
            })
            if not job_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_COPYABLE)
            log.info("Household copy of %s queued as job %s.", body.recipe_id, job_id)
            slot.done({"job_id": str(job_id)})
            return CopyResponse(job_id=str(job_id))

    return router
