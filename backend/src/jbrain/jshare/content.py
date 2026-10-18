"""What a JShare upload is, and how a zipped site's files are found inside it.

Pure functions over a filename and an open zip, so the public route and the upload route
share one answer and both are testable without a database. Media types come from the table
here, never from the uploader's `Content-Type` and never from the container's mimetypes
database, so what a visitor's browser is told is decided by this file alone.
"""

from __future__ import annotations

import base64
import posixpath
import re
import secrets
import zipfile
from dataclasses import dataclass
from typing import IO

SLUG_RE = re.compile(r"^[a-z2-7]{26}$")

#: Generous for a phone video over the LAN; a tunnelled upload hits Cloudflare's own
#: per-request cap (100 MB on the free plan) long before this.
MAX_UPLOAD_BYTES = 4 * 1024**3
#: A zipped site is unpacked file by file as it is served, so these bound what a hostile or
#: broken archive can claim, not what is ever held in memory.
MAX_SITE_MEMBERS = 20_000
MAX_SITE_UNPACKED_BYTES = 4 * 1024**3

_KINDS: dict[str, tuple[str, str]] = {
    "png": ("image", "image/png"),
    "jpg": ("image", "image/jpeg"),
    "jpeg": ("image", "image/jpeg"),
    "gif": ("image", "image/gif"),
    "webp": ("image", "image/webp"),
    "avif": ("image", "image/avif"),
    "svg": ("image", "image/svg+xml"),
    "bmp": ("image", "image/bmp"),
    "ico": ("image", "image/x-icon"),
    "heic": ("image", "image/heic"),
    "mp3": ("audio", "audio/mpeg"),
    "m4a": ("audio", "audio/mp4"),
    "aac": ("audio", "audio/aac"),
    "wav": ("audio", "audio/wav"),
    "ogg": ("audio", "audio/ogg"),
    "oga": ("audio", "audio/ogg"),
    "opus": ("audio", "audio/ogg"),
    "flac": ("audio", "audio/flac"),
    "weba": ("audio", "audio/webm"),
    "mp4": ("video", "video/mp4"),
    "m4v": ("video", "video/mp4"),
    "mov": ("video", "video/quicktime"),
    "webm": ("video", "video/webm"),
    "ogv": ("video", "video/ogg"),
    "html": ("html", "text/html"),
    "htm": ("html", "text/html"),
}

#: What the files inside a zipped site are served as. Anything not listed goes out as
#: octet-stream, which a browser will not run or render.
_SITE_TYPES: dict[str, str] = {
    **{ext: media for ext, (_, media) in _KINDS.items()},
    "css": "text/css",
    "js": "text/javascript",
    "mjs": "text/javascript",
    "json": "application/json",
    "map": "application/json",
    "webmanifest": "application/manifest+json",
    "txt": "text/plain",
    "md": "text/plain",
    "csv": "text/csv",
    "xml": "application/xml",
    "wasm": "application/wasm",
    "woff": "font/woff",
    "woff2": "font/woff2",
    "ttf": "font/ttf",
    "otf": "font/otf",
    "pdf": "application/pdf",
    "glb": "model/gltf-binary",
    "gltf": "model/gltf+json",
}

_OCTET = "application/octet-stream"
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class ShareRejected(ValueError):
    """An upload JShare will not publish; the message is shown to the owner as is."""


@dataclass(frozen=True)
class Classified:
    kind: str
    media_type: str
    #: For a site, the folder inside the zip that holds its index.html ('' = the root).
    site_root: str = ""


def new_slug() -> str:
    # 16 random bytes are 26 base32 characters: 128 bits, lowercase, one DNS label.
    return base64.b32encode(secrets.token_bytes(16)).decode().rstrip("=").lower()


def clean_filename(name: str | None) -> str:
    """The uploaded name with any client path and control characters stripped."""
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = _CONTROL.sub("", base).strip()
    return base[:200] or "file"


def _ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def site_media_type(name: str) -> str:
    return _SITE_TYPES.get(_ext(name), _OCTET)


def classify(filename: str, data: IO[bytes]) -> Classified:
    """Decide what an upload is from its name, and for a zip, from what it holds.

    A zip with an index.html is a site; a zip without one is just a file to download.
    `data` is read only for a zip, and left wherever it was.
    """
    ext = _ext(filename)
    if ext == "zip":
        root = site_root(data)
        if root is not None:
            return Classified("site", "text/html", root)
        return Classified("file", "application/zip")
    kind, media = _KINDS.get(ext, ("file", _OCTET))
    return Classified(kind, media)


def _index(zf: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    # Zips made on Windows can use backslashes; macOS adds a __MACOSX shadow of every file.
    out: dict[str, zipfile.ZipInfo] = {}
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        if info.is_dir() or name.startswith("__MACOSX/"):
            continue
        out[name] = info
    return out


def site_root(data: IO[bytes]) -> str | None:
    """The folder holding a zipped site's index.html, or None when it has none.

    The root itself wins; otherwise the shallowest index.html, as long as only one sits at
    that depth (a zip of a folder, the usual way a site gets zipped). Raises ShareRejected
    for an archive that is not a zip or claims more than this box will serve.
    """
    pos = data.tell()
    try:
        try:
            zf = zipfile.ZipFile(data)
        except (zipfile.BadZipFile, OSError) as exc:
            raise ShareRejected("That .zip could not be opened.") from exc
        with zf:
            infos = zf.infolist()
            if len(infos) > MAX_SITE_MEMBERS:
                raise ShareRejected(f"That zip holds more than {MAX_SITE_MEMBERS} files.")
            if sum(i.file_size for i in infos) > MAX_SITE_UNPACKED_BYTES:
                raise ShareRejected("That zip unpacks to more than 4 GB.")
            if any(i.flag_bits & 0x1 for i in infos):
                raise ShareRejected("That zip is password-protected.")
            names = _index(zf)
    finally:
        data.seek(pos)
    if "index.html" in names:
        return ""
    candidates = [n for n in names if posixpath.basename(n) == "index.html"]
    if not candidates:
        return None
    depth = min(n.count("/") for n in candidates)
    shallowest = [n for n in candidates if n.count("/") == depth]
    if len(shallowest) != 1:
        return None
    return shallowest[0][: -len("index.html")]


@dataclass(frozen=True)
class SiteLookup:
    """A request path resolved against a site: a file to serve, or a folder to redirect
    into (so its page's relative links resolve), or neither."""

    info: zipfile.ZipInfo | None = None
    redirect_to_folder: bool = False


def lookup(zf: zipfile.ZipFile, root: str, path: str) -> SiteLookup:
    """Find `path` (the URL path under the share, already percent-decoded) in the site.

    A dictionary lookup over the archive's own names, never a filesystem path, so `..` and
    friends can only ever miss.
    """
    names = _index(zf)
    rel = path.lstrip("/")
    if rel == "" or rel.endswith("/"):
        rel += "index.html"
    info = names.get(root + rel)
    if info is not None:
        return SiteLookup(info=info)
    if root + rel + "/index.html" in names:
        return SiteLookup(redirect_to_folder=True)
    return SiteLookup()
