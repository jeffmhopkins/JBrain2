"""`jbrain.blob_refs` — the list a delete consults before it unlinks.

There is ONE content-addressed store on this box, so "whose file is this" is never
answerable from the row you happen to be holding. The list is the whole of that answer,
and its failure mode is an ABSENCE: add a blob-holding table, forget to add it here, and
nothing goes wrong until something deletes — after which that feature's files start
disappearing under it, with a 200 on the delete that caused it.

An absence cannot be tested from the other side, so what is testable is the shape: every
entry names a real table and binds `:sha`, the SQL composes into one statement, and the
`except_row` carve-out applies to exactly one table. The list is checked against the REAL
schema in `tests/integration/test_sdr_recordings_rls.py`, which is what catches a table
or column renamed out from under it.
"""

from __future__ import annotations

from typing import Any

import pytest

from jbrain.blob_refs import BLOB_REFERENCES, blob_referenced


def test_every_reference_names_a_table_and_matches_on_the_bound_digest() -> None:
    for ref in BLOB_REFERENCES:
        assert ref.table.startswith("app."), ref
        assert ":sha" in ref.where, ref
        assert ref.holds, f"{ref.table} must say whose file it is, for the log line"
    # No duplicates: a table appears once, with all its digest columns in one predicate,
    # so the composed statement stays one EXISTS per table.
    tables = [ref.table for ref in BLOB_REFERENCES]
    assert len(tables) == len(set(tables))


def test_the_recordings_table_is_in_the_list() -> None:
    """The bug this module exists for was the opposite arrangement: the recordings table
    was the ONLY one consulted. It is now one row of many, and still present."""
    assert "app.sdr_recordings" in {ref.table for ref in BLOB_REFERENCES}


def test_the_list_covers_the_features_that_can_hold_an_identical_recording() -> None:
    """Named individually because each is a reachable way for a recording's bytes to be
    somebody else's file too: the PWA offers Download (.mp3), chat attachments allow-list
    `audio/mpeg`, notes take attachments, and any of them re-uploaded is the SAME digest.
    """
    tables = {ref.table for ref in BLOB_REFERENCES}
    assert {"app.turn_attachments", "app.attachments", "app.generated_images"} <= tables


def fake(value: object) -> Any:
    """Hand the stand-in below to a function typed for a real `AsyncSession`.

    A cast at the call keeps that one fact in one place instead of a per-line ignore on
    every call — which a multi-line call puts on the wrong line anyway."""
    return value


class _Session:
    """A session that records the statement it was given and answers it."""

    def __init__(self, answer: Any = False, boom: Exception | None = None) -> None:
        self.answer = answer
        self.boom = boom
        self.sql = ""
        self.params: dict[str, Any] = {}

    async def execute(self, statement: Any, params: dict[str, Any]) -> Any:
        if self.boom is not None:
            raise self.boom
        self.sql = str(statement)
        self.params = params
        answer = self.answer

        class _Result:
            def scalar_one(self) -> Any:
                return answer

        return _Result()


async def test_the_check_asks_every_table_in_one_statement() -> None:
    """One statement, not one query per table. A failure part-way through a sequence
    aborts the transaction and leaves the rest unasked — which is the shape that answers
    "nothing points at it" for the wrong reason, on the one question where a wrong no
    deletes a file."""
    session = _Session(answer=False)

    assert await blob_referenced(fake(session), "a" * 64) is False

    for ref in BLOB_REFERENCES:
        assert f"FROM {ref.table}" in session.sql, ref.table
    assert session.sql.count("EXISTS") == len(BLOB_REFERENCES)
    assert session.params["sha"] == "a" * 64


async def test_the_excepted_row_is_excluded_from_its_own_table_only() -> None:
    """A trim repoints one row and then asks whether anything ELSE still points at the
    old digest. The carve-out must not leak into the other tables — an attachment with
    the same id as the recording is not the recording."""
    session = _Session(answer=False)

    await blob_referenced(
        fake(session),
        "b" * 64,
        except_row=("app.sdr_recordings", "11111111-1111-1111-1111-111111111111"),
    )

    assert session.sql.count("id <> CAST(") == 1
    assert "11111111-1111-1111-1111-111111111111" in session.params.values()


async def test_no_except_id_carves_nothing_out() -> None:
    """`(table, None)` is what a plain DELETE passes, so callers need no branch."""
    session = _Session(answer=False)

    await blob_referenced(fake(session), "c" * 64, except_row=("app.sdr_recordings", None))

    assert "id <>" not in session.sql


@pytest.mark.parametrize("answer", [True, 1])
async def test_a_hit_anywhere_keeps_the_blob(answer: Any) -> None:
    assert await blob_referenced(fake(_Session(answer=answer)), "d" * 64) is True


async def test_a_check_that_could_not_run_keeps_the_blob() -> None:
    """Fails CLOSED. A check that could not run is not permission to unlink: a leaked
    blob costs disk, and a wrong `False` costs somebody's file — a chat attachment whose
    download 500s for ever, on a box whose owner cannot go and look."""
    session = _Session(boom=RuntimeError("the database went away"))

    assert await blob_referenced(fake(session), "e" * 64) is True
