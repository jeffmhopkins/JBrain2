import hashlib
import os
from pathlib import Path

from jbrain.storage import FsBlobStore


async def test_put_is_content_addressed_and_idempotent(tmp_path: Path) -> None:
    store = FsBlobStore(tmp_path)
    data = b"lab results pdf bytes"

    digest = await store.put(data)

    assert digest == hashlib.sha256(data).hexdigest()
    assert store.path_for(digest).read_bytes() == data
    assert await store.exists(digest)
    # Re-putting identical content dedupes to the same path.
    assert await store.put(data) == digest


async def test_sharded_layout_and_missing_blob(tmp_path: Path) -> None:
    store = FsBlobStore(tmp_path)
    digest = await store.put(b"x")
    assert store.path_for(digest) == tmp_path / digest[:2] / digest[2:4] / digest
    assert not await store.exists("0" * 64)


async def test_put_stream_matches_put_and_leaves_no_temp(tmp_path: Path) -> None:
    store = FsBlobStore(tmp_path)
    chunks = [b"es_1e12 ", b"artifact ", b"bytes"]
    whole = b"".join(chunks)

    async def _gen():
        for c in chunks:
            yield c

    digest = await store.put_stream(_gen())

    assert digest == hashlib.sha256(whole).hexdigest()
    assert store.path_for(digest).read_bytes() == whole
    assert await store.exists(digest)
    # No leftover spool files after a successful stream.
    assert not [n for n in os.listdir(tmp_path) if n.startswith(".incoming")]
    # Streaming identical content dedupes to the same digest/path.
    assert await store.put_stream(_gen()) == digest
