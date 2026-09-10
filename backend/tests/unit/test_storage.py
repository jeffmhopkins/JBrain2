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


async def test_delete_removes_one_blob_and_leaves_the_others(tmp_path: Path) -> None:
    """The first thing in the repo that deletes a blob (SDR trim), so the property that
    matters is that it removes exactly one: a trim frees the original and every other
    recording, attachment and image in the same store must survive it."""
    store = FsBlobStore(tmp_path)
    doomed = await store.put(b"the untrimmed capture")
    kept = await store.put(b"someone else's attachment")

    assert await store.delete(doomed) is True

    assert not await store.exists(doomed)
    assert not store.path_for(doomed).exists()
    assert await store.exists(kept)
    assert await store.get(kept) == b"someone else's attachment"


async def test_deleting_a_missing_blob_is_not_an_error(tmp_path: Path) -> None:
    """Idempotent by contract: a trim whose old blob is already gone must still be able
    to finish repointing its row rather than raising halfway through."""
    store = FsBlobStore(tmp_path)
    digest = await store.put(b"x")
    assert await store.delete(digest) is True

    # Second delete of the same digest, and one that was never stored at all.
    assert await store.delete(digest) is False
    assert await store.delete("0" * 64) is False


async def test_deleting_then_re_putting_the_same_bytes_works(tmp_path: Path) -> None:
    """`put` skips the write when the target exists; a stale directory left behind by a
    delete must not make the re-put a no-op that returns a digest with no file."""
    store = FsBlobStore(tmp_path)
    data = b"a clip that gets recorded twice"
    digest = await store.put(data)
    await store.delete(digest)

    assert await store.put(data) == digest
    assert await store.get(digest) == data
