"""Owner-only family-membership management (JBrain360 M7a).

The owner curates the family roster — the `view_scope` membership that gates
family-sees-family location reads. Every route is `OwnerDep`-gated and runs under
the owner's `SessionContext`, so the owner-only `view_scope` RLS is the real
barrier; adding a subject opens the mutual read path, removing it closes it.
"""

from datetime import datetime
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from jbrain.api.deps import OwnerDep
from jbrain.auth.service import PrincipalInfo
from jbrain.db.session import SessionContext
from jbrain.family import FamilyMember, SqlFamilyRepo

router = APIRouter(prefix="/family")


def get_family_repo(request: Request) -> SqlFamilyRepo:
    return cast(SqlFamilyRepo, request.app.state.family_repo)


FamilyRepoDep = Annotated[SqlFamilyRepo, Depends(get_family_repo)]


def _owner_ctx(owner: PrincipalInfo) -> SessionContext:
    return SessionContext(principal_id=owner.id, principal_kind="owner")


class MemberOut(BaseModel):
    subject_id: str
    label: str
    added_at: datetime

    @classmethod
    def of(cls, m: FamilyMember) -> "MemberOut":
        return cls(subject_id=m.subject_id, label=m.label, added_at=m.added_at)


class AddMemberRequest(BaseModel):
    subject_id: str


@router.get("/members")
async def list_members(owner: OwnerDep, repo: FamilyRepoDep) -> list[MemberOut]:
    return [MemberOut.of(m) for m in await repo.members(_owner_ctx(owner))]


@router.post("/members", status_code=204)
async def add_member(owner: OwnerDep, repo: FamilyRepoDep, body: AddMemberRequest) -> None:
    """Add a subject to the family (idempotent). Opens the mutual family-sees-family
    read path between this subject and the rest of the group."""
    await repo.add_member(_owner_ctx(owner), body.subject_id)


@router.delete("/members/{subject_id}", status_code=204)
async def remove_member(owner: OwnerDep, repo: FamilyRepoDep, subject_id: str) -> None:
    """Remove a subject from the family — its family-sees-family read path ends."""
    await repo.remove_member(_owner_ctx(owner), subject_id)
