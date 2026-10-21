"""What a JShare upload is, and how a zipped site's files are found inside it."""

from __future__ import annotations

import io
import zipfile

import pytest

from jbrain.jshare.content import (
    SLUG_RE,
    ShareRejected,
    classify,
    clean_filename,
    lookup,
    new_slug,
    site_media_type,
    site_root,
)


def _zip(files: dict[str, bytes], **kwargs: object) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", **kwargs) as zf:  # type: ignore[arg-type]
        for name, data in files.items():
            zf.writestr(name, data)
    buf.seek(0)
    return buf


def test_slugs_are_unguessable_dns_labels() -> None:
    slugs = {new_slug() for _ in range(200)}
    assert len(slugs) == 200
    assert all(SLUG_RE.fullmatch(s) for s in slugs)


@pytest.mark.parametrize(
    ("name", "kind", "media"),
    [
        ("Beach.JPG", "image", "image/jpeg"),
        ("song.mp3", "audio", "audio/mpeg"),
        ("clip.mov", "video", "video/quicktime"),
        ("page.html", "html", "text/html"),
        ("notes.pdf", "file", "application/pdf"),
        ("README", "file", "application/octet-stream"),
    ],
)
def test_kind_comes_from_the_name(name: str, kind: str, media: str) -> None:
    got = classify(name, io.BytesIO(b""))
    assert (got.kind, got.media_type) == (kind, media)


def test_filenames_lose_paths_and_control_characters() -> None:
    assert clean_filename("C:\\Users\\j\\pic.png") == "pic.png"
    assert clean_filename("../../etc/passwd") == "passwd"
    assert clean_filename("a\x00b\nc.txt") == "abc.txt"
    assert clean_filename("") == "file"
    assert clean_filename(None) == "file"


def test_a_zip_with_index_at_its_root_is_a_site() -> None:
    data = _zip({"index.html": b"<h1>hi</h1>", "app.js": b""})
    got = classify("site.zip", data)
    assert (got.kind, got.site_root) == ("site", "")
    assert data.tell() == 0


def test_a_zipped_folder_is_a_site_rooted_in_that_folder() -> None:
    data = _zip({"dist/index.html": b"", "dist/a/index.html": b"", "__MACOSX/x": b""})
    assert site_root(data) == "dist/"


def test_two_candidate_roots_is_not_a_site() -> None:
    data = _zip({"a/index.html": b"", "b/index.html": b""})
    assert classify("two.zip", data).kind == "file"


def test_a_zip_without_index_is_a_plain_download() -> None:
    got = classify("photos.zip", _zip({"one.jpg": b"x"}))
    assert (got.kind, got.media_type) == ("file", "application/zip")


def test_not_a_zip_is_refused() -> None:
    with pytest.raises(ShareRejected, match="could not be opened"):
        classify("broken.zip", io.BytesIO(b"definitely not a zip"))


def test_a_zip_claiming_too_much_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    from jbrain.jshare import content

    monkeypatch.setattr(content, "MAX_SITE_UNPACKED_BYTES", 10)
    with pytest.raises(ShareRejected, match="unpacks"):
        classify("big.zip", _zip({"index.html": b"x" * 11}))
    monkeypatch.setattr(content, "MAX_SITE_MEMBERS", 1)
    with pytest.raises(ShareRejected, match="more than 1 files"):
        classify("many.zip", _zip({"index.html": b"", "b": b""}))


def test_site_lookup() -> None:
    zf = zipfile.ZipFile(
        _zip({"dist/index.html": b"home", "dist/docs/index.html": b"docs", "dist/a.css": b""})
    )
    found = lookup(zf, "dist/", "")
    assert found.info is not None and zf.read(found.info) == b"home"
    assert lookup(zf, "dist/", "a.css").info is not None
    assert lookup(zf, "dist/", "docs/").info is not None
    assert lookup(zf, "dist/", "docs").redirect_to_folder
    assert lookup(zf, "dist/", "missing.png").info is None
    # A dictionary lookup over the archive's names: climbing out of the root only misses.
    assert lookup(zf, "dist/", "../dist/index.html").info is None
    assert lookup(zf, "dist/", "/etc/passwd").info is None


def test_site_types_never_guess() -> None:
    assert site_media_type("app.mjs") == "text/javascript"
    assert site_media_type("x.WOFF2") == "font/woff2"
    assert site_media_type("payload.exe") == "application/octet-stream"
