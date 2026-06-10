"""TEI client wire format and batching, plus the pgvector literal encoder."""

import json

import httpx
import pytest

from jbrain.embed import EMBED_BATCH, TeiEmbedClient, vector_literal


def tei_transport(requests: list[dict]) -> httpx.MockTransport:
    """Fake TEI: records request bodies, returns one 3-dim vector per input."""

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/embed"
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json=[[float(i), 0.0, 1.0] for i in range(len(body["inputs"]))])

    return httpx.MockTransport(handle)


async def test_embed_posts_inputs_with_truncate() -> None:
    seen: list[dict] = []
    client = TeiEmbedClient("http://embed:80", transport=tei_transport(seen))
    vectors = await client.embed(["alpha", "beta"])
    assert seen == [{"inputs": ["alpha", "beta"], "truncate": True}]
    assert vectors == [[0.0, 0.0, 1.0], [1.0, 0.0, 1.0]]


async def test_embed_batches_at_sixteen() -> None:
    seen: list[dict] = []
    client = TeiEmbedClient("http://embed:80", transport=tei_transport(seen))
    texts = [f"chunk {i}" for i in range(EMBED_BATCH * 2 + 3)]
    vectors = await client.embed(texts)
    assert [len(r["inputs"]) for r in seen] == [16, 16, 3]
    assert len(vectors) == len(texts)  # order preserved across batches
    assert seen[2]["inputs"][-1] == texts[-1]


async def test_embed_raises_on_http_error() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="model loading")

    client = TeiEmbedClient("http://embed:80", transport=httpx.MockTransport(boom))
    # Failing normally is the contract: queue backoff handles container startup.
    with pytest.raises(httpx.HTTPStatusError):
        await client.embed(["x"])


def test_vector_literal_is_pgvector_input() -> None:
    assert vector_literal([0.25, -1.0, 2]) == "[0.25,-1.0,2.0]"


def test_vector_literal_coerces_to_float_only() -> None:
    # float() on every element: non-numeric input cannot reach the SQL string.
    with pytest.raises((TypeError, ValueError)):
        vector_literal(["1; DROP TABLE app.chunks"])  # type: ignore[list-item]
