"""What a simulated night did, as numbers — and what a set of nights did, as distributions.

One trajectory at temperature 1.0 tells you nothing. That is not a caution, it is the lesson
the replay harness paid for: same-model control arms diverged 6-35% at the very first step, so
a single trace is indistinguishable from noise. Everything here is therefore built to be read
across many nights of an arm and compared against many nights of another
(docs/plans/JMOLT_LEDGER_ENGINE_PLAN.md, S1 and S3).

Every score is computed from the RECORD — the outbox rows the night staged, the action ledger,
the writes the simulated platform believed — and never from the transcript. What the model
said it did is the thing under study, not the source of truth about it: a night that narrated
four posts and made one is precisely the failure we are trying to measure.

Three of the plan's six metrics are computable from what S1 ships. The other three — claim
repeat ratio, follow-through, and confabulation count — need the claim gate and the promise
extractor, which are S2 machinery. They are absent here rather than stubbed: a metric that
always returns zero reads on a scoreboard exactly like a metric that found nothing wrong.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from jbrain.agent.jmolt_sim import SimNight
from jbrain.agent.jmolt_sim_client import SIM_ID_PREFIX


class Embedder(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class NightScore:
    """One night. `restatement` is None when no embedder was supplied — an unmeasured
    metric must not be reported as a measured zero."""

    posts: int
    comments: int
    votes: int
    published: int
    self_replies: int
    repeat_threads: int
    scratch_files_written: int
    died: bool
    restatement: float | None = None
    label: str = ""

    @property
    def silent(self) -> bool:
        """A night that published nothing. The plan's target is a median of ≤2 publishes and
        at least 30% of nights silent, so this is a metric, not a fault."""
        return self.published == 0


@dataclass
class ArmScore:
    """A whole arm: n nights, summarised as distributions rather than means alone. The median
    is what the plan states targets against; the spread is what says whether two arms differ."""

    label: str
    n: int
    nights: list[NightScore] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)


def _text_of(row: Any) -> str:
    p = row.payload if isinstance(row.payload, dict) else {}
    return f"{p.get('title', '')} {p.get('content', '')}".strip()


def _own_ids(night: SimNight) -> set[str]:
    """Every id this night's own writes were given, plus anything the outbox recorded as
    published. A self-reply is a reply to one of these."""
    ids = {w.sim_id for w in night.writes if w.sim_id}
    ids |= {r.moltbook_id for r in night.outbox if r.moltbook_id}
    return ids


def _self_replies(night: SimNight) -> int:
    """Replies whose parent is something this night itself wrote.

    Counted off `parent_id` AND off the post being one of tonight's own: jmolt commented on
    its own fresh post as well as on its own comment, and only the first has a parent."""
    own = _own_ids(night)
    count = 0
    for row in night.outbox:
        if row.kind != "comment":
            continue
        payload = row.payload if isinstance(row.payload, dict) else {}
        parent = str(payload.get("parent_id") or "")
        post = str(payload.get("post_id") or "")
        if (parent and parent in own) or (post and post in own):
            count += 1
        elif post.startswith(SIM_ID_PREFIX):
            # Belt: an id minted by the simulated platform this night, even if the outbox row
            # that produced it failed before recording one.
            count += 1
    return count


def _repeat_threads(night: SimNight) -> int:
    """Threads commented on more than once in a night, counted as the EXCESS. Seventeen
    comments on one post is one thread and sixteen repeats, and the second number is the one
    that describes the failure."""
    seen: dict[str, int] = {}
    for row in night.outbox:
        if row.kind != "comment":
            continue
        payload = row.payload if isinstance(row.payload, dict) else {}
        post = str(payload.get("post_id") or "")
        if post:
            seen[post] = seen.get(post, 0) + 1
    return sum(n - 1 for n in seen.values() if n > 1)


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


async def _restatement(night: SimNight, prior: Sequence[str], embed: Embedder) -> float | None:
    """The plan's restatement rate: for each thing published, the maximum cosine against
    everything the agent had already said, averaged over the night.

    `prior` grows as the night goes: a second post restating the first is a restatement even
    though the first was not there when the night began.

    An item with NOTHING before it is not scored at all rather than scored zero. On a first
    night that is every item, and averaging those zeroes in would report the emptiest arm as
    the most original one. Returns None when nothing was scoreable, because zero would read
    on the scoreboard as "measured, and perfectly novel"."""
    tonight = [t for t in (_text_of(r) for r in night.outbox if r.kind in ("post", "comment")) if t]
    history = [t for t in prior if t]
    if not tonight or not (history or len(tonight) > 1):
        return None
    vectors = await embed.embed(history + tonight)
    past = list(vectors[: len(history)])
    scores = []
    for vec in vectors[len(history) :]:
        if past:
            scores.append(max(_cosine(vec, p) for p in past))
        past.append(vec)  # tonight's own earlier item is prior for the next one
    return sum(scores) / len(scores) if scores else None


async def score_night(
    night: SimNight,
    *,
    prior: Sequence[str] = (),
    embed: Embedder | None = None,
) -> NightScore:
    """One night, from its record. `prior` is what the agent had already published before
    this night — the corpus a restatement restates."""
    kinds = [r.kind for r in night.outbox]
    return NightScore(
        posts=kinds.count("post"),
        comments=kinds.count("comment"),
        votes=kinds.count("vote"),
        published=sum(1 for r in night.outbox if r.status == "published"),
        self_replies=_self_replies(night),
        repeat_threads=_repeat_threads(night),
        scratch_files_written=len(night.scratch_after),
        died=bool(night.error),
        restatement=await _restatement(night, prior, embed) if embed else None,
        label=night.label,
    )


def summarize(label: str, scores: Sequence[NightScore]) -> ArmScore:
    """An arm's distributions. Medians and spread, never a single trace — comparing one night
    of one arm against one night of another is how the replay harness convinced itself of a
    result it did not have."""
    stats: dict[str, Any] = {}
    if scores:
        for name in ("posts", "comments", "published", "self_replies", "repeat_threads"):
            values = [float(getattr(s, name)) for s in scores]
            stats[name] = {
                "median": statistics.median(values),
                "mean": statistics.fmean(values),
                "min": min(values),
                "max": max(values),
                "stdev": statistics.stdev(values) if len(values) > 1 else 0.0,
            }
        stats["silent_share"] = sum(1 for s in scores if s.silent) / len(scores)
        stats["died_share"] = sum(1 for s in scores if s.died) / len(scores)
        measured = [s.restatement for s in scores if s.restatement is not None]
        # Reported only when something was actually measured, and alongside HOW MANY nights
        # it came from: a median over two of twenty nights is not the arm's restatement rate.
        if measured:
            stats["restatement"] = {
                "median": statistics.median(measured),
                "mean": statistics.fmean(measured),
                "nights_measured": len(measured),
            }
    return ArmScore(label=label, n=len(scores), nights=list(scores), stats=stats)
