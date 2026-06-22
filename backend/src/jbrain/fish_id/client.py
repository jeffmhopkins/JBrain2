"""HTTP client for the local fishial identification service (docs/FISH_ID_PLAN.md).

The fishial pipeline (segment → detect → embed → nearest-species over 866 classes)
is wrapped behind a tiny loopback HTTP API so the backend never imports torch and
the API image stays dep-free: the model serving lives in the `fish-id` compose
profile, this is just the wire to it. The frozen contract (validated by the F0
on-box spike):

  POST /identify  (multipart `file`, query `top_k`) → {"candidates": [{species,
                  common_name, score}, …]}  — ranked, score in 0..1, len ≤ top_k

The admin/unload side (POST /free) lives in `gateway.py`, the sibling split the
image stack uses (jbrain.image_gen.comfyui vs .gateway).

`client` is the app's shared `httpx.AsyncClient` (a single on-box host, no auth —
the service is host-managed on the loopback), mirroring `ComfyUiImageGen`; tests
drive it through a `MockTransport` with no network or GPU.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
import structlog

log = structlog.get_logger()

# The model can't see arbitrarily deep into 866 near-identical classes usefully, and the
# hero card shows one verdict + a short "also considered" line — so cap the request.
DEFAULT_TOP_K = 5
MAX_TOP_K = 10

# Overall budget for one identification (cold model load + segment/detect/embed); the
# single source the config default (fish_id_timeout) and the client default share.
DEFAULT_TIMEOUT = 120.0


class FishIdError(Exception):
    """An identification failed — the service was unreachable, returned a non-2xx,
    or sent a payload we couldn't read. The handler turns this into a clean
    tool-error string, never a stack trace to the model."""


@dataclass(frozen=True)
class FishMatch:
    """One candidate species the classifier returned. `score` is its confidence in
    0..1; `common_name` is empty when the service has no common name for the class."""

    species: str
    common_name: str
    score: float


@dataclass(frozen=True)
class FishResult:
    """A ranked identification — `matches` is highest-confidence first (possibly
    empty when the service detected no fish)."""

    matches: tuple[FishMatch, ...]

    @property
    def top(self) -> FishMatch | None:
        return self.matches[0] if self.matches else None


class FishIdentifier:
    """The identify capability the tool depends on, so it takes the action rather
    than the concrete HTTP client (the in-memory fake satisfies it — the same seam
    as the `ImageGen` protocol). Not a runtime Protocol: `HttpFishIdentifier` and
    `FakeFishIdentifier` subclass it so the type is explicit."""

    async def identify(self, image: bytes, top_k: int = DEFAULT_TOP_K) -> FishResult:
        raise NotImplementedError


def _coerce_matches(payload: object, top_k: int) -> tuple[FishMatch, ...]:
    """Parse the service's `{"candidates": [...]}` into matches, dropping any entry
    that isn't a well-formed {species, score}; truncated to top_k. Tolerant by
    design — a malformed row is skipped, not fatal, so one odd class can't blank a
    good identification."""
    if not isinstance(payload, dict):
        raise FishIdError("identification service returned an unexpected shape")
    rows = payload.get("candidates")
    if not isinstance(rows, list):
        raise FishIdError("identification service returned no candidates list")
    matches: list[FishMatch] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        species = row.get("species")
        score = row.get("score")
        if not isinstance(species, str) or not species.strip():
            continue
        if not isinstance(score, int | float) or isinstance(score, bool):
            continue
        common = row.get("common_name")
        matches.append(
            FishMatch(
                species=species.strip(),
                common_name=common.strip() if isinstance(common, str) else "",
                score=max(0.0, min(1.0, float(score))),
            )
        )
    return tuple(matches[:top_k])


def _clamp_top_k(top_k: int) -> int:
    return max(1, min(MAX_TOP_K, top_k))


class HttpFishIdentifier(FishIdentifier):
    """Drive the localhost fishial service over its HTTP API. `client` is the app's
    shared `httpx.AsyncClient`; `timeout` covers a cold model load + inference (the
    service loads lazily and we free it after each call, so a request pays the load)."""

    def __init__(
        self, base_url: str, client: httpx.AsyncClient, *, timeout: float = DEFAULT_TIMEOUT
    ):
        self._root = base_url.rstrip("/")
        self._client = client
        self._timeout = timeout

    async def identify(self, image: bytes, top_k: int = DEFAULT_TOP_K) -> FishResult:
        k = _clamp_top_k(top_k)
        try:
            resp = await self._client.post(
                f"{self._root}/identify",
                files={"file": ("fish", image, "application/octet-stream")},
                params={"top_k": k},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            return FishResult(_coerce_matches(resp.json(), k))
        except httpx.HTTPError as exc:
            log.warning("fish_id.identify_failed", error=str(exc))
            raise FishIdError(str(exc)) from exc
        except ValueError as exc:  # non-JSON body
            log.warning("fish_id.identify_bad_json", error=str(exc))
            raise FishIdError("identification service returned a non-JSON body") from exc
