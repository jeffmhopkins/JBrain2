"""Unit tests for the guided-intake flow orchestration (no DB).

Fakes stand in for the repos so the security-relevant branches — empty secret,
claim miss, cookie minting on a win, show-once hashing — are covered at 100%. The
RLS firewall and the atomic claim are proven separately against real Postgres
(test_intake_pg / test_intake_rls)."""

from datetime import UTC, datetime

from jbrain.auth import keys
from jbrain.db.session import SessionContext
from jbrain.intake import service
from jbrain.intake.service import (
    ClaimResult,
    IntakeLinkConfig,
    IntakeLinkRecord,
    IntakeSessionRecord,
    IntakeSubmissionRecord,
)
from tests.unit.fakes import FakeAuthRepo

_CTX = SessionContext(principal_id="owner-1", principal_kind="owner")


def _config() -> IntakeLinkConfig:
    return IntakeLinkConfig(
        subject_id="s-1",
        domain_code="general",
        label="intake",
        persona_brief="",
        fields_brief="a phone number",
        opening_blurb="hi",
        max_runs=5,
        max_opens=20,
        bind_on_first=False,
        ttl_hours=24.0,
    )


class _FakeIntakeRepo:
    """A structural `IntakeRepo`; only the methods the flows touch are meaningful."""

    def __init__(self, claim_result: ClaimResult | None) -> None:
        self.claim_result = claim_result
        self.created_secret_hash: str | None = None
        self.claim_calls = 0

    async def create_link(
        self, ctx: SessionContext, *, secret_hash: str, config: IntakeLinkConfig
    ) -> IntakeLinkRecord:
        self.created_secret_hash = secret_hash
        now = datetime.now(UTC)
        return IntakeLinkRecord(
            id="link-1",
            subject_id=config.subject_id,
            domain_code=config.domain_code,
            label=config.label,
            persona_brief=config.persona_brief,
            fields_brief=config.fields_brief,
            opening_blurb=config.opening_blurb,
            max_runs=config.max_runs,
            runs_used=0,
            max_opens=config.max_opens,
            opens_used=0,
            bind_on_first=config.bind_on_first,
            capture_enterer_name=config.capture_enterer_name,
            disclose_owner_identity=config.disclose_owner_identity,
            status="active",
            created_at=now,
            expires_at=now,
        )

    async def list_links(self, ctx: SessionContext) -> list[IntakeLinkRecord]:
        return []

    async def get_link(self, ctx: SessionContext, link_id: str) -> IntakeLinkRecord | None:
        return None

    async def revoke_link(self, ctx: SessionContext, link_id: str) -> bool:
        return link_id == "link-1"

    async def list_sessions(self, ctx: SessionContext, link_id: str) -> list[IntakeSessionRecord]:
        return []

    async def list_submissions(
        self, ctx: SessionContext, link_id: str
    ) -> list[IntakeSubmissionRecord]:
        return []

    async def get_submission(
        self, ctx: SessionContext, submission_id: str
    ) -> IntakeSubmissionRecord | None:
        return None

    async def claim(
        self, *, secret_hash: str, principal_key_hash: str, label: str
    ) -> ClaimResult | None:
        self.claim_calls += 1
        return self.claim_result


async def test_mint_returns_secret_once_and_stores_only_its_hash() -> None:
    repo = _FakeIntakeRepo(claim_result=None)
    secret, record = await service.mint_intake_link(repo, _CTX, _config())
    assert record.id == "link-1"
    # The stored hash matches the returned secret, and is NOT the secret itself (#14).
    assert repo.created_secret_hash == keys.hash_token(secret)
    assert repo.created_secret_hash != secret


async def test_redeem_empty_secret_short_circuits() -> None:
    repo = _FakeIntakeRepo(claim_result=None)
    auth = FakeAuthRepo()
    assert await service.redeem_intake_link(repo, auth, "") is None
    assert repo.claim_calls == 0 and auth.sessions == []


async def test_redeem_claim_miss_writes_no_cookie() -> None:
    repo = _FakeIntakeRepo(claim_result=None)
    auth = FakeAuthRepo()
    assert await service.redeem_intake_link(repo, auth, "live-secret") is None
    assert repo.claim_calls == 1 and auth.sessions == []


async def test_redeem_win_mints_cookie_bound_to_the_session_principal() -> None:
    claim = ClaimResult(
        principal_id="p-9",
        session_id="sess-9",
        link_id="link-1",
        config_snapshot={"opening_blurb": "hi"},
        expires_at=datetime.now(UTC),
    )
    repo = _FakeIntakeRepo(claim_result=claim)
    auth = FakeAuthRepo()
    result = await service.redeem_intake_link(repo, auth, "live-secret")
    assert result is not None
    assert result.session_id == "sess-9" and result.link_id == "link-1"
    # The cookie is bound to the per-session principal, stored as its hash.
    assert len(auth.sessions) == 1
    assert auth.sessions[0].principal_id == "p-9"
    assert auth.sessions[0].token_hash == keys.hash_token(result.cookie_token)


async def test_revoke_delegates_to_repo() -> None:
    repo = _FakeIntakeRepo(claim_result=None)
    assert await service.revoke_intake_link(repo, _CTX, "link-1") is True
    assert await service.revoke_intake_link(repo, _CTX, "nope") is False
