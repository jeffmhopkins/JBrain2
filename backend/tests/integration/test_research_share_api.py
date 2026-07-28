"""The report share-link API end-to-end against real Postgres (migration 0150).

Drives the actual FastAPI app: the owner mints report + folder links (secret shown once),
then an UNAUTHENTICATED client reads them at the public `/research-share` surface — proving
the no-login grant works, that a folder link tracks membership, that revocation and unknown
tokens 404, that the anti-leak headers are set, and that a shared library report's private
(non-URL) citations are stripped from the public payload.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.config import Settings
from jbrain.db.session import scoped_session
from jbrain.main import create_app
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed(maker: async_sessionmaker[AsyncSession]) -> dict[str, str]:
    """A web report, a library report with a private (URL-less) citation, and two more for a
    folder — all under the owner context. Returns their ids by key."""
    ids = {k: str(uuid.uuid4()) for k in ("web", "lib", "m1", "m2")}
    async with scoped_session(maker, OWNER) as s:
        await s.execute(text("DELETE FROM app.research_reports"))
        await s.execute(
            text(
                "INSERT INTO app.research_reports"
                " (id, question, question_hash, report_md, source_mode, sources, status)"
                " VALUES (:id, 'web q', :h, '## Web', 'web',"
                " '[{\"url\": \"https://example.com\", \"title\": \"Ex\"}]'::jsonb, 'done')"
            ),
            {"id": ids["web"], "h": uuid.uuid4().hex},
        )
        await s.execute(
            text(
                "INSERT INTO app.research_reports"
                " (id, question, question_hash, report_md, source_mode, sources, status)"
                " VALUES (:id, 'lib q', :h, '## Lib', 'library',"
                " '[{\"url\": \"https://ok.com\", \"title\": \"Ok\"},"
                "   {\"title\": \"Private note snippet\"}]'::jsonb, 'done')"
            ),
            {"id": ids["lib"], "h": uuid.uuid4().hex},
        )
        for key, question in (("m1", "member one"), ("m2", "member two")):
            await s.execute(
                text(
                    "INSERT INTO app.research_reports"
                    " (id, question, question_hash, report_md, status)"
                    " VALUES (:id, :q, :h, 'body', 'done')"
                ),
                {"id": ids[key], "q": question, "h": uuid.uuid4().hex},
            )
    return ids


async def test_share_api_end_to_end(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],
) -> None:
    ids = await _seed(maker)
    key = await service.rotate_owner_key(SqlAuthRepo(maker))
    app = create_app(Settings(secure_cookies=False, database_url=database_url))
    lib_base = "/api/research-library"
    share_base = "/api/research-share"
    with TestClient(app) as client:
        # Owner-gated management: no session → 401.
        assert client.post(f"{lib_base}/shares", json={"target_kind": "report"}).status_code == 401
        assert client.get(f"{lib_base}/shares").status_code == 401

        assert client.post("/api/auth/session", json={"owner_key": key}).status_code == 204

        # A folder with two members (via the real folder + move endpoints).
        gid = client.post(f"{lib_base}/report-groups", json={"name": "Medical"}).json()["id"]
        for key_ in ("m1", "m2"):
            assert (
                client.patch(f"{lib_base}/reports/{ids[key_]}/group", json={"group_id": gid})
            ).status_code == 204

        # Mint: a web report link (no warning), a library report link (warning), a folder link.
        web = client.post(
            f"{lib_base}/shares", json={"target_kind": "report", "report_id": ids["web"]}
        ).json()
        assert web["library_warning"] is False and web["token"]
        libr = client.post(
            f"{lib_base}/shares", json={"target_kind": "report", "report_id": ids["lib"]}
        ).json()
        assert libr["library_warning"] is True  # sharing a notes-derived report is flagged
        folder = client.post(
            f"{lib_base}/shares", json={"target_kind": "group", "group_id": gid}
        ).json()
        assert folder["label"] == "Medical"  # folder name snapshotted for the public index

        # The secret is shown once and is NOT recoverable from the list.
        listed = client.get(f"{lib_base}/shares").json()
        assert {row["id"] for row in listed} == {web["id"], libr["id"], folder["id"]}
        assert all("token" not in row for row in listed)

        # Mint validation: mismatched target/kind → 422; unknown target → 404.
        assert client.post(f"{lib_base}/shares", json={"target_kind": "report"}).status_code == 422
        assert (
            client.post(
                f"{lib_base}/shares",
                json={"target_kind": "report", "report_id": str(uuid.uuid4())},
            ).status_code
            == 404
        )

        # --- public reads, UNAUTHENTICATED ---
        client.cookies.clear()
        assert client.get(f"{lib_base}/shares").status_code == 401  # owner surface now closed

        web_view = client.get(f"{share_base}/{web['token']}")
        assert web_view.status_code == 200
        body = web_view.json()
        assert body["kind"] == "report" and body["report"]["report_md"] == "## Web"
        # Anti-leak headers on the public response.
        assert web_view.headers["referrer-policy"] == "no-referrer"
        assert "noindex" in web_view.headers["x-robots-tag"]

        # A library report's private (URL-less) citation is stripped; the real URL survives.
        lib_sources = client.get(f"{share_base}/{libr['token']}").json()["report"]["sources"]
        assert [s.get("url") for s in lib_sources] == ["https://ok.com"]

        # The folder link is an index of its current members; each member is fetchable.
        index = client.get(f"{share_base}/{folder['token']}").json()
        assert index["kind"] == "group" and index["label"] == "Medical"
        assert {r["id"] for r in index["reports"]} == {ids["m1"], ids["m2"]}
        assert (
            client.get(f"{share_base}/{folder['token']}/reports/{ids['m1']}").status_code == 200
        )
        # A report the link does not grant (the web report, not in the folder) → 404 via RLS.
        assert (
            client.get(f"{share_base}/{folder['token']}/reports/{ids['web']}").status_code == 404
        )
        # An unknown token → 404.
        assert client.get(f"{share_base}/{uuid.uuid4().hex}").status_code == 404

        # --- revoke closes the public read ---
        assert client.post("/api/auth/session", json={"owner_key": key}).status_code == 204
        assert client.delete(f"{lib_base}/shares/{web['id']}").status_code == 204
        assert client.delete(f"{lib_base}/shares/{web['id']}").status_code == 404  # already gone
        client.cookies.clear()
        assert client.get(f"{share_base}/{web['token']}").status_code == 404
