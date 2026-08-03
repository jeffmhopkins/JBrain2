"""The thin httpx client to the jlaunch control server (and a fake for tests).

Mirrors ``JcodeClient``: a pinned base URL from config (never model-supplied), a bearer
token, an injectable transport so tests need no network, and a clean ``JlaunchError``
instead of a leaked stack trace. The control server lives on the internal ``jlaunch``
network; this is the only place the api contacts it.
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx

# Control calls are quick request/response; the artifact stream has its own longer path.
_TIMEOUT = httpx.Timeout(30.0)
# The artifact can be hundreds of MB — no overall deadline on the streamed read.
_ARTIFACT_TIMEOUT = httpx.Timeout(30.0, read=None)


class JlaunchError(RuntimeError):
    """A control-server call failed — surfaced to the route as a clean message. ``status``
    carries the upstream HTTP status when the failure was a response (else None), so a route
    can map a 409 (busy) / 404 (unknown) distinctly from a transport error."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class JlaunchApi(Protocol):
    """What the api routes depend on — satisfied by the real client and the fake."""

    async def list_specs(self) -> list[dict[str, Any]]: ...

    async def list_jobs(self) -> list[dict[str, Any]]: ...

    async def get_job(self, jid: str) -> dict[str, Any]: ...

    async def start_job(self, spec: str) -> dict[str, Any]: ...

    async def stop(self, jid: str) -> dict[str, Any]: ...

    async def kill(self, jid: str) -> dict[str, Any]: ...

    async def delete(self, jid: str) -> None: ...

    def stream_artifact(self, jid: str) -> Any: ...


class JlaunchClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._transport = transport

    def _client(self, *, timeout: httpx.Timeout = _TIMEOUT) -> httpx.AsyncClient:
        if not self._base_url:
            raise JlaunchError("the job launcher is not configured")
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=timeout,
            transport=self._transport,
        )

    async def _json(self, method: str, path: str, **kw: Any) -> Any:
        try:
            async with self._client() as client:
                resp = await client.request(method, path, **kw)
                resp.raise_for_status()
                return resp.json() if resp.content else None
        except httpx.HTTPStatusError as exc:
            raise JlaunchError(
                f"jlaunch control server error: {exc}", status=exc.response.status_code
            ) from exc
        except httpx.HTTPError as exc:
            raise JlaunchError(f"jlaunch control server error: {exc}") from exc

    async def list_specs(self) -> list[dict[str, Any]]:
        return await self._json("GET", "/specs")

    async def list_jobs(self) -> list[dict[str, Any]]:
        return await self._json("GET", "/jobs")

    async def get_job(self, jid: str) -> dict[str, Any]:
        return await self._json("GET", f"/jobs/{jid}")

    async def start_job(self, spec: str) -> dict[str, Any]:
        return await self._json("POST", "/jobs", json={"spec": spec})

    async def stop(self, jid: str) -> dict[str, Any]:
        return await self._json("POST", f"/jobs/{jid}/stop")

    async def kill(self, jid: str) -> dict[str, Any]:
        return await self._json("POST", f"/jobs/{jid}/kill")

    async def delete(self, jid: str) -> None:
        # Idempotent: a 404 means the control server already dropped this job, which is
        # the delete's desired end state — swallow it so the route still removes the
        # durable mirror row instead of 502-ing and stranding it.
        try:
            async with self._client() as client:
                resp = await client.request("DELETE", f"/jobs/{jid}")
                if resp.status_code != httpx.codes.NOT_FOUND:
                    resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise JlaunchError(f"jlaunch control server error: {exc}") from exc

    async def stream_artifact(self, jid: str):
        """Stream a finished job's artifact bytes from the control server. Yields chunks;
        the api pipes them straight into the content-addressed blob store at mint time."""
        try:
            async with (
                self._client(timeout=_ARTIFACT_TIMEOUT) as client,
                client.stream("GET", f"/jobs/{jid}/artifact") as resp,
            ):
                resp.raise_for_status()
                async for chunk in resp.aiter_bytes():
                    yield chunk
        except httpx.HTTPError as exc:
            raise JlaunchError(f"jlaunch control server error: {exc}") from exc


class FakeJlaunchClient:
    """In-memory control server for tests: no httpx, no network."""

    def __init__(self, *, artifact: bytes = b"FAKE-ARTIFACT") -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._n = 0
        self._artifact = artifact
        # Specs the fake advertises; tests assert the api forwards the selection.
        self.started_specs: list[str] = []

    def _make(self, jid: str, spec: str) -> dict[str, Any]:
        return {
            "id": jid,
            "spec_name": spec,
            "title": f"job {spec}",
            "repo": "https://example.invalid/repo",
            "state": "running",
            "phase": "run",
            "artifact_name": "es_1e12_artifacts.tar.gz",
            "started_at": "2026-08-03T00:00:00+00:00",
            "finished_at": None,
            "verify_ok": None,
            "headline": [],
            "machine": {"cpu": "Fake CPU", "cores": 8, "mem_bytes": 16 * 1024**3},
            "phase_times": [],
            "error": None,
            "artifact_present": False,
            "artifact_bytes": None,
            "console_tail": "",
        }

    async def list_specs(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "erdos_straus_1e12",
                "title": "Erdos-Straus census to 10^12",
                "repo": "https://github.com/jeffmhopkins/Erd-s-Straus-attack",
                "phases": ["install", "smoke", "run"],
                "artifact": "es_1e12_artifacts.tar.gz",
                "est_hours": "6-10 h",
                "disk_gb": 15,
                "all_cores": True,
                "notes": "test",
            }
        ]

    async def list_jobs(self) -> list[dict[str, Any]]:
        return list(self._jobs.values())

    async def get_job(self, jid: str) -> dict[str, Any]:
        if jid not in self._jobs:
            raise JlaunchError(f"unknown job: {jid}")
        return self._jobs[jid]

    async def start_job(self, spec: str) -> dict[str, Any]:
        self._n += 1
        jid = f"job{self._n}"
        self.started_specs.append(spec)
        job = self._make(jid, spec)
        self._jobs[jid] = job
        return job

    async def stop(self, jid: str) -> dict[str, Any]:
        job = await self.get_job(jid)
        job["state"] = "killed"
        return job

    async def kill(self, jid: str) -> dict[str, Any]:
        job = await self.get_job(jid)
        job["state"] = "killed"
        return job

    async def delete(self, jid: str) -> None:
        self._jobs.pop(jid, None)

    async def stream_artifact(self, jid: str):
        await self.get_job(jid)
        yield self._artifact

    # -- test helpers ------------------------------------------------------------

    def mark_succeeded(self, jid: str) -> None:
        """Flip a fake job to a finished, artifact-bearing state (for share tests)."""
        job = self._jobs[jid]
        job.update(
            state="succeeded",
            phase="headline",
            finished_at="2026-08-03T06:00:00+00:00",
            verify_ok=True,
            headline=[
                "hard primes: 1170000000",
                "max minimal R: 107 at p = 8803369 (record R=107 stands)",
                "R >= 87 counts: {87: 3}",
            ],
            artifact_present=True,
            artifact_bytes=len(self._artifact),
        )
