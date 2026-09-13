"""Every place in this repo that stores a blob digest — the list a delete must consult.

There is ONE content-addressed store on the box (`jbrain.storage`), and it holds
everything: note attachments, chat attachments, generated images, tool artifacts,
jlaunch build outputs, entity portraits, and now SDR recordings. Identical bytes are
ONE file with one digest, so "whose file is this" is never answerable from the row you
happen to be holding — it is answerable only by asking every table.

**Why this module exists.** `SDR_RECORDING_PLAN.md` §7 argued that no other table could
share a recording's blob "by accident", and checked only `app.sdr_recordings` before
unlinking. That is false today, not hypothetically: the PWA offers Download (.mp3),
`agent/attachments.py` allow-lists `audio/mpeg` so the owner can attach audio to a chat,
and `api/chat_attachments.py` stores it with `blobs.put(data)`. Download a recording,
attach it, delete the recording — identical bytes, identical digest, one file, and the
attachment's download 500s. The same holds for a portrait, an image, a note attachment.

**What happens if you add a blob-holding table and do not add it here.** Nothing, until
something deletes a blob — and then that feature's files start disappearing under it,
silently, with a 200 on the delete that caused it and a 500 on the read that finds out.
There is no test that can catch the omission from the other side, because the omission is
an absence. So: a migration that adds a column holding a `blobs.put(...)` digest adds a
row to `BLOB_REFERENCES` in the same PR. `test_blob_refs.py` guards the shape of the list
and `test_sdr_recordings_rls.py` runs it against the real schema, so a table or column
named wrongly here fails CI rather than the owner's disk.

**This is a guard, not a refcount.** The right fix is for the store itself to count
references, so that no caller can forget; that is a bigger change than a bug fix and is
noted as such. Until then, every `blobs.delete(...)` in the repo must run through
`blob_referenced` first — this module is the whole of that contract.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class BlobRef:
    """One table that stores blob digests, and the SQL that matches one.

    `where` is a predicate over the table's own columns binding `:sha`, rather than a
    bare column name, because a table may hold digests in more than one column and some
    hold them inside jsonb (see `_frames`).
    """

    table: str
    where: str
    #: Whose file it is, for the log line when a delete is refused. The owner never sees
    #: this; the next person reading the box's logs does.
    holds: str


def _frames(column: str, path: str) -> str:
    """Match a digest inside a `[{t_ms, caption, thumb_id}, ...]` frame list.

    A video's frame thumbnails are ordinary blobs whose ONLY reference is a `thumb_id`
    inside a cached analysis, so a delete that looked only at columns would empty a
    chat's filmstrip. `jsonb_path_exists` with a bound variable rather than string-built
    json: the digest arrives as a parameter either way, and this keeps it one.
    """
    return (
        f"jsonb_path_exists({column},"
        f" '{path} ? (@ == $sha)',"
        " jsonb_build_object('sha', CAST(:sha AS text)))"
    )


#: Every blob-digest column in the schema, enumerated from `information_schema` rather
#: than from memory. Deliberately NOT included, because they are hashes of something
#: other than a stored blob: `app.note_conversations.note_body_sha` (a note's text),
#: `app.research_reports.question_hash` (a dedup key), `app.deploy_history.git_sha`.
BLOB_REFERENCES: tuple[BlobRef, ...] = (
    BlobRef("app.sdr_recordings", "blob_sha256 = :sha", "a radio recording"),
    BlobRef("app.attachments", "sha256 = :sha", "a note attachment"),
    BlobRef(
        "app.turn_attachments",
        f"sha256 = :sha OR {_frames('analysis', '$.frames[*].thumb_id')}",
        "a chat attachment",
    ),
    BlobRef("app.turn_tool_artifacts", "sha256 = :sha", "a saved tool artifact"),
    BlobRef(
        "app.attachment_extracts",
        _frames("analysis", "$.frames[*].thumb_id"),
        "a video frame thumbnail",
    ),
    BlobRef(
        "app.external_sources",
        _frames("frames", "$[*].thumb_id"),
        "a research video's frame thumbnail",
    ),
    BlobRef(
        "app.media_analysis_results",
        _frames("result", "$.frames[*].thumb_id"),
        "a cached media analysis' frame thumbnail",
    ),
    BlobRef(
        "app.generated_images",
        "blob_sha256 = :sha OR source_sha256 = :sha",
        "a generated image (or the source it was edited from)",
    ),
    BlobRef("app.jlaunch_runs", "artifact_sha256 = :sha", "a jlaunch build artifact"),
    BlobRef("app.entities", "image_sha = :sha", "an entity's portrait"),
    BlobRef("app.wiki_articles", "image_sha = :sha", "a wiki article's portrait"),
)


async def blob_referenced(
    session: AsyncSession,
    sha256: str,
    *,
    except_row: tuple[str, str | None] | None = None,
) -> bool:
    """Whether ANY table still stores this digest — ask before you unlink.

    `except_row` is `(table, id)`: the one row whose reference does not count, because it
    is the row being repointed or was just deleted. A None id excepts nothing, so callers
    need no branch.

    **Runs on the session it is given, and therefore under that session's scope.** It
    must be a context that can see every domain — an `OwnerDep` context is exactly that
    (`app.has_domain_scope` is true for an owner who is not `owner_scoped`). A narrowed
    scope would hide a health-domain attachment and report its file free to delete, so
    the callers are owner-only routes and this stays where the reader can see it.

    **Fails closed.** A check that could not run is not permission to unlink: this
    answers True and the blob is kept. A leaked blob costs disk; a wrong False costs
    somebody's file.
    """
    clauses: list[str] = []
    params: dict[str, str] = {"sha": sha256}
    for i, ref in enumerate(BLOB_REFERENCES):
        where = f"({ref.where})"
        if except_row is not None and except_row[0] == ref.table and except_row[1]:
            params[f"skip{i}"] = except_row[1]
            where = f"{where} AND id <> CAST(:skip{i} AS uuid)"
        clauses.append(f"EXISTS (SELECT 1 FROM {ref.table} WHERE {where})")
    # One statement, not one per table: a failure part-way through a sequence of queries
    # aborts the transaction and leaves the rest unasked, which is the shape that answers
    # "nothing points at it" for the wrong reason.
    sql = "SELECT " + " OR ".join(clauses)
    try:
        return bool((await session.execute(text(sql), params)).scalar_one())
    except Exception as exc:  # noqa: BLE001 — see "fails closed" above
        log.warning("blob_refs.check_failed", error=repr(exc), sha=sha256[:12])
        return True
