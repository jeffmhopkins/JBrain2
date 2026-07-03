"""Intake for the EMR importer (docs/plans/EMR_IMPORT_PLAN.md §6.1) — the pure,
security-critical core: pull the decrypt password from the note body, extract an
AES-encrypted ZIP with hostile-archive guards, and scrub the password from the
body. The password lives ONLY in memory for the decrypt step — never a chunk,
embedding, log, or setting. Extraction is hardened (per-entry + total
uncompressed-size caps = zip-bomb guard; path-traversal rejection; regular-files
only). The orchestration (attach decrypted files, chunk, then delete-last /
scrub-before-index, fail-closed) is in `intake_handler.py`.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

import pyzipper

# Per-entry and total uncompressed caps — a zip-bomb guard. Sized well above a
# real ~350-page EMR corpus (tens of MB) but far below memory exhaustion.
MAX_ENTRY_BYTES = 200 * 1024 * 1024
MAX_TOTAL_BYTES = 500 * 1024 * 1024
MAX_MEMBERS = 64

# The password is stated in plain language; a deterministic matcher pulls it. The
# `(?:\s+is|\s+was)?` connector folds "password is X"/"password was X" into the
# same pattern as "password: X"/"password X" so the generic form can't misfire and
# capture the word "is". The value may be backtick/quote-wrapped ("password `xyz`");
# those wrappers are stripped. Every distinct candidate is tried at decrypt.
_PW_PATTERNS = (
    re.compile(r"password(?:\s+is|\s+was)?[:=\s]+[`'\"]?(?P<pw>[^`'\"\s]+)", re.I),
    re.compile(r"passcode(?:\s+is|\s+was)?[:=\s]+[`'\"]?(?P<pw>[^`'\"\s]+)", re.I),
    re.compile(r"\bpw[:=\s]+[`'\"]?(?P<pw>[^`'\"\s]+)", re.I),
)


class ArchiveGuardError(Exception):
    """A hostile-archive guard tripped (zip-bomb, path traversal, non-regular member)."""


@dataclass(frozen=True)
class ExtractedFile:
    filename: str
    data: bytes


def extract_passwords(body: str) -> list[str]:
    """Deterministically pull candidate decrypt passwords from the note body, in
    priority order, de-duplicated. Empty when none parse (the run fails closed)."""
    out: list[str] = []
    for pat in _PW_PATTERNS:
        for m in pat.finditer(body):
            pw = m.group("pw")
            if pw and pw not in out:
                out.append(pw)
    return out


def scrub_password(body: str, passwords: list[str]) -> str:
    """Redact every occurrence of each candidate password from the body BEFORE it
    is chunked/embedded, so the secret never reaches an index or the LLM. Replaces
    the raw value everywhere it appears (belt-and-suspenders over the matched span)."""
    scrubbed = body
    for pw in sorted(set(passwords), key=len, reverse=True):
        if pw:
            scrubbed = scrubbed.replace(pw, "[redacted]")
    return scrubbed


def _reject_unsafe_name(name: str) -> None:
    if not name or name.endswith("/"):
        return
    p = PurePosixPath(name)
    if p.is_absolute() or name.startswith("/") or "\\" in name or ".." in p.parts:
        raise ArchiveGuardError(f"unsafe archive member path: {name!r}")


def safe_extract(zip_bytes: bytes, password: str) -> list[ExtractedFile]:
    """Extract an AES-encrypted ZIP in memory with the given password, enforcing
    the hostile-archive guards. Raises `ArchiveGuardError` on a guard breach and
    lets pyzipper's own error propagate on a wrong password / unreadable archive
    (the caller tries the next candidate, else fails closed). Never writes to a
    real filesystem path (#2) — everything stays in memory / behind BlobStore."""
    out: list[ExtractedFile] = []
    total = 0
    with pyzipper.AESZipFile(io.BytesIO(zip_bytes)) as zf:
        zf.setpassword(password.encode())
        infos = zf.infolist()
        members = [i for i in infos if not i.filename.endswith("/")]
        if len(members) > MAX_MEMBERS:
            raise ArchiveGuardError(f"archive has {len(members)} members (> {MAX_MEMBERS})")
        for info in members:
            _reject_unsafe_name(info.filename)
            if info.file_size > MAX_ENTRY_BYTES:
                raise ArchiveGuardError(f"member {info.filename!r} exceeds the per-entry cap")
            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                raise ArchiveGuardError("archive exceeds the total uncompressed cap")
            data = zf.read(info.filename)  # decrypts; raises on a bad password
            out.append(ExtractedFile(filename=info.filename, data=data))
    return out
