"""EMR intake security core (docs/plans/EMR_IMPORT_PLAN.md §6.1) — password
extraction/scrub and the hardened AES-unzip guards. Pure; a real encrypted ZIP is
built in-memory with pyzipper. This is a 100%-coverage security path.
"""

from __future__ import annotations

import io

import pytest
import pyzipper

from jbrain.ingest.emr import intake


def _aes_zip(files: dict[str, bytes], password: str) -> bytes:
    buf = io.BytesIO()
    with pyzipper.AESZipFile(
        buf, "w", compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES
    ) as zf:
        zf.setpassword(password.encode())
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


# --- password extraction --------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("here are my records, password: hunter2", ["hunter2"]),
        ("password is `s3cr3t!`", ["s3cr3t!"]),
        ("pw abc123", ["abc123"]),
        ("the passcode = zzz", ["zzz"]),
        ('unlock with password "Quoted-One"', ["Quoted-One"]),
        ("no secret here", []),
    ],
)
def test_extract_passwords(body: str, expected: list[str]) -> None:
    assert intake.extract_passwords(body) == expected


def test_extract_passwords_dedupes_and_orders() -> None:
    got = intake.extract_passwords("password: abc and again password is abc")
    assert got == ["abc"]


# --- scrub ----------------------------------------------------------------


def test_scrub_removes_every_occurrence() -> None:
    body = "password: hunter2. (also hunter2 appears here)"
    out = intake.scrub_password(body, ["hunter2"])
    assert "hunter2" not in out and "[redacted]" in out


def test_scrub_is_noop_without_a_password() -> None:
    assert intake.scrub_password("nothing to hide", []) == "nothing to hide"


# --- hardened extraction --------------------------------------------------


def test_safe_extract_reads_members() -> None:
    z = _aes_zip({"a.pdf": b"PDF-A", "b.pdf": b"PDF-B"}, "pw")
    files = intake.safe_extract(z, "pw")
    assert {f.filename: f.data for f in files} == {"a.pdf": b"PDF-A", "b.pdf": b"PDF-B"}


def test_wrong_password_raises() -> None:
    z = _aes_zip({"a.pdf": b"x"}, "correct")
    with pytest.raises(Exception):  # noqa: B017 — pyzipper's own bad-password error
        intake.safe_extract(z, "wrong")


def test_path_traversal_member_rejected() -> None:
    z = _aes_zip({"../evil.pdf": b"x"}, "pw")
    with pytest.raises(intake.ArchiveGuardError):
        intake.safe_extract(z, "pw")


def test_absolute_path_member_rejected() -> None:
    z = _aes_zip({"/etc/passwd": b"x"}, "pw")
    with pytest.raises(intake.ArchiveGuardError):
        intake.safe_extract(z, "pw")


def test_per_entry_size_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(intake, "MAX_ENTRY_BYTES", 4)
    z = _aes_zip({"big.pdf": b"0123456789"}, "pw")
    with pytest.raises(intake.ArchiveGuardError):
        intake.safe_extract(z, "pw")


def test_total_size_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(intake, "MAX_TOTAL_BYTES", 8)
    z = _aes_zip({"a.pdf": b"01234", "b.pdf": b"56789"}, "pw")
    with pytest.raises(intake.ArchiveGuardError):
        intake.safe_extract(z, "pw")


def test_too_many_members(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(intake, "MAX_MEMBERS", 1)
    z = _aes_zip({"a.pdf": b"x", "b.pdf": b"y"}, "pw")
    with pytest.raises(intake.ArchiveGuardError):
        intake.safe_extract(z, "pw")
