"""Core identity tables: who data is about, who can act, and firewall domains.

`subjects` (who/what data is about) is deliberately separate from
`principals` (credentials that can act): intake links and tracker keys are
principals bound to a subject, while the owner is a principal with no
subject restriction.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, SmallInteger, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Domain(Base):
    __tablename__ = "domains"
    __table_args__ = {"schema": "app"}

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)


class Subject(Base):
    __tablename__ = "subjects"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    display_name: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)  # 'person' | 'device'
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Principal(Base):
    __tablename__ = "principals"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(Text)  # 'owner' | 'capability_token' | 'device_key'
    subject_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.subjects.id"), nullable=True
    )
    key_hash: Mapped[str] = mapped_column(Text, unique=True)
    label: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set only for capability_token (debug-console) principals: a time-box so the
    # grant lapses on its own. NULL = never expires (owner/device principals).
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Stamped on each successful capability-token auth so the owner's token list
    # shows liveness. NULL until first use.
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set only for capability_token principals: a reversible pause. A suspended
    # token fails auth (like revoked) but the owner can clear this to resume it.
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set only for jcode_share_link principals: the single code-mode session this
    # grant is scoped to (the control-server session id — a hex token, NOT a UUID).
    # NULL for every other kind. Redeem + every operational jcode route checks it.
    jcode_session_id: Mapped[str | None] = mapped_column(Text, nullable=True)


class DeviceSession(Base):
    __tablename__ = "device_sessions"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    principal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.principals.id")
    )
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    label: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
