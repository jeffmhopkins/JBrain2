"""GrowBot's policy library (migration 0268): walks the Jobs launcher trained, kept on the box
so the Body tab's remote can drive them after the job that made them is gone.

A policy is the exported JSON the creature's runner (`growbot/site/growbot_runner.js`) loads.
`summary` reads its contract out of `obs_spec` (what the remote must feed it each tick), so the
library and the Body tab never have to parse the whole file.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session

# The largest library policy: the trained MLPs are tens of KB, upstream's biggest stock walk
# about 600 KB; the creature parses it whole on the phone.
MAX_POLICY_BYTES = 4 * 1024 * 1024


# The channels growbot_runner.js's Walker._validate takes; it throws on any other, so the library
# refuses those rather than offer a walk that can only fail on the phone.
_RUNNER_CHANNELS = frozenset(
    {
        "accel_x",
        "accel_y",
        "accel_z",
        "gyro_x",
        "gyro_y",
        "gyro_z",
        "cmd_forward",
        "cmd_yaw",
        "cmd_pitch",
        "euler_roll",
        "euler_pitch",
        "euler_yaw",
    }
)
_RUNNER_PATTERN = re.compile(r"prev_action_\d+|cycle:.+|cmd_mode_[a-z0-9_]+")


class PolicyError(ValueError):
    """A file that is not a policy the creature's runner can drive from the Body tab."""


@dataclass(frozen=True)
class Contract:
    # Which end walks first and counts as ahead: "nose", or "camera" (the tail end).
    facing: str
    # cmd_yaw is a heading error (the GrowBot_Training export), so a turn is a target heading.
    heading: bool
    # It takes a speed command (cmd_forward), so Back can back away rather than turn around.
    speed: bool
    # It takes a camera-height command (cmd_pitch).
    pitch: bool
    control_hz: int


@dataclass(frozen=True)
class PolicyRow:
    id: str
    name: str
    source_job: str
    source_spec: str
    bytes: int
    created_at: datetime
    contract: Contract


def summary(policy: Any) -> Contract:
    """The policy's contract, or PolicyError when the runner could not drive it from the pad:
    it must be the segments/channels dialect, and a cmd_yaw it takes must be the heading error
    the trained walks declare (a constant ±1 turn command reads opposite, README §robot)."""
    if not isinstance(policy, dict):
        raise PolicyError("not a policy file")
    spec = policy.get("obs_spec")
    if not isinstance(spec, dict):
        raise PolicyError("the file has no obs_spec, so it isn't a runner policy")
    channels: list[str] = []
    segments = spec.get("segments")
    if isinstance(segments, list):
        for seg in segments:
            got = seg.get("channels") if isinstance(seg, dict) else None
            if isinstance(got, list):
                channels.extend(str(c) for c in got)
    elif isinstance(spec.get("channels"), list):
        channels = [str(c) for c in spec["channels"]]
    if not channels:
        raise PolicyError("the policy declares no input channels")
    odd = [c for c in channels if c not in _RUNNER_CHANNELS and not _RUNNER_PATTERN.fullmatch(c)]
    if odd:
        raise PolicyError(f"the phone's runner can't feed its input {odd[0]!r}")
    for key in ("mean", "std", "layers"):
        if not isinstance(policy.get(key), list) or not policy[key]:
            raise PolicyError(f"the policy has no {key}")
    yaw = spec.get("cmd_yaw")
    heading = isinstance(yaw, dict) and yaw.get("meaning") == "heading_error"
    if "cmd_yaw" in channels and not heading:
        raise PolicyError("its turn input isn't a heading error, so the pad can't steer it")
    if "cmd_yaw" not in channels:
        raise PolicyError("it takes no heading, so the pad can't steer it")
    facing = str(yaw.get("facing") or "nose") if isinstance(yaw, dict) else "nose"
    try:
        hz = int(spec.get("control_hz") or 0)
    except (TypeError, ValueError):
        hz = 0
    if not 1 <= hz <= 100:
        raise PolicyError("its control rate (obs_spec.control_hz) is missing or out of range")
    return Contract(
        facing="camera" if facing == "camera" else "nose",
        heading=True,
        speed="cmd_forward" in channels,
        pitch="cmd_pitch" in channels,
        control_hz=hz,
    )


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} isn't a number JSON allows")


def parse(raw: bytes) -> tuple[dict[str, Any], Contract]:
    if len(raw) > MAX_POLICY_BYTES:
        raise PolicyError("the policy file is too large")
    try:
        # NaN/Infinity would pass json.loads only for Postgres's jsonb to refuse them (a 500).
        policy = json.loads(raw, parse_constant=_no_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise PolicyError("the file isn't JSON a policy can hold") from exc
    return policy, summary(policy)


def _row(r: Any) -> PolicyRow:
    return PolicyRow(
        id=str(r.id),
        name=r.name,
        source_job=r.source_job,
        source_spec=r.source_spec,
        bytes=r.bytes,
        created_at=r.created_at,
        contract=summary(r.policy),
    )


class PolicyRepo(Protocol):
    async def list(self, ctx: SessionContext) -> list[PolicyRow]: ...

    async def add(
        self,
        ctx: SessionContext,
        name: str,
        policy: dict[str, Any],
        size: int,
        source_job: str,
        source_spec: str,
    ) -> PolicyRow: ...

    async def rename(self, ctx: SessionContext, pid: str, name: str) -> PolicyRow | None: ...

    async def delete(self, ctx: SessionContext, pid: str) -> bool: ...

    async def get_json(self, ctx: SessionContext, pid: str) -> dict[str, Any] | None: ...

    async def exists(self, ctx: SessionContext, pid: str) -> bool: ...


def _stub(key: str) -> str:
    # A one-element stand-in for a weight array, so a listing never ships megabytes of weights
    # out of Postgres just for summary() to see that they are there.
    v = f"policy->'{key}'"
    return (
        f"'{key}', CASE WHEN jsonb_typeof({v}) = 'array' THEN"
        f" CASE WHEN jsonb_array_length({v}) > 0 THEN '[0]'::jsonb END END"
    )


_COLS = (
    "id, name, source_job, source_spec, bytes, created_at,"
    " jsonb_build_object('obs_spec', policy->'obs_spec', "
    + ", ".join(_stub(k) for k in ("mean", "std", "layers"))
    + ") AS policy"
)


class SqlPolicyRepo:
    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    async def list(self, ctx: SessionContext) -> list[PolicyRow]:
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                await session.execute(
                    text(f"SELECT {_COLS} FROM app.growbot_policies ORDER BY created_at DESC")
                )
            ).all()
        out: list[PolicyRow] = []
        for r in rows:
            # A row the summary no longer reads (a runner contract that moved on) is left out
            # of the pad rather than offered and refused on the phone.
            try:
                out.append(_row(r))
            except PolicyError:
                continue
        return out

    async def add(
        self,
        ctx: SessionContext,
        name: str,
        policy: dict[str, Any],
        size: int,
        source_job: str,
        source_spec: str,
    ) -> PolicyRow:
        async with scoped_session(self._maker, ctx) as session:
            r = (
                await session.execute(
                    text(
                        "INSERT INTO app.growbot_policies"
                        " (name, source_job, source_spec, policy, bytes)"
                        " VALUES (:n, :j, :s, CAST(:p AS jsonb), :b)"
                        f" RETURNING {_COLS}"
                    ),
                    {
                        "n": name,
                        "j": source_job,
                        "s": source_spec,
                        "p": json.dumps(policy),
                        "b": size,
                    },
                )
            ).one()
        return _row(r)

    async def rename(self, ctx: SessionContext, pid: str, name: str) -> PolicyRow | None:
        """None when there is no such policy; PolicyError when the row is one summary() no
        longer reads (list() leaves those out, so the owner can only reach one by id)."""
        async with scoped_session(self._maker, ctx) as session:
            r = (
                await session.execute(
                    text(
                        "UPDATE app.growbot_policies SET name = :n"
                        f" WHERE id = CAST(:i AS uuid) RETURNING {_COLS}"
                    ),
                    {"n": name, "i": pid},
                )
            ).first()
            # Read inside the transaction, so a row summary() refuses rolls the rename back.
            return _row(r) if r is not None else None

    async def delete(self, ctx: SessionContext, pid: str) -> bool:
        async with scoped_session(self._maker, ctx) as session:
            done = await session.execute(
                text("DELETE FROM app.growbot_policies WHERE id = CAST(:i AS uuid) RETURNING id"),
                {"i": pid},
            )
            return done.first() is not None

    async def get_json(self, ctx: SessionContext, pid: str) -> dict[str, Any] | None:
        async with scoped_session(self._maker, ctx) as session:
            r = (
                await session.execute(
                    text("SELECT policy FROM app.growbot_policies WHERE id = CAST(:i AS uuid)"),
                    {"i": pid},
                )
            ).first()
        return dict(r.policy) if r is not None and isinstance(r.policy, dict) else None

    async def exists(self, ctx: SessionContext, pid: str) -> bool:
        async with scoped_session(self._maker, ctx) as session:
            r = await session.execute(
                text("SELECT 1 FROM app.growbot_policies WHERE id = CAST(:i AS uuid)"),
                {"i": pid},
            )
            return r.first() is not None
