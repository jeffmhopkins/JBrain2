"""Dependency smoke test for the EMR importer (docs/plans/EMR_IMPORT_PLAN.md §6.1,
§9 config-guarding rule). Fails fast if `pyzipper` (AES-zip extraction) or the
shipped PyMuPDF (PDF decrypt + page raster) is missing from a synced environment —
so a broken `uv sync` / dev-setup step reddens here instead of deep in Wave 2/3.
"""

from __future__ import annotations


def test_pyzipper_provides_aes_zip() -> None:
    import pyzipper

    # The AES surface the importer actually uses (§6.1): decrypt an AES-encrypted
    # member. Presence of the class is enough — the guard is "installed & importable".
    assert hasattr(pyzipper, "AESZipFile")
    assert hasattr(pyzipper, "WZ_AES")


def test_pymupdf_present_for_decrypt_and_raster() -> None:
    import pymupdf

    # authenticate() decrypts a password-protected PDF; the page raster (W3 ARIA OCR)
    # rides get_pixmap — both on the shipped dep, so no new dependency is added there.
    assert hasattr(pymupdf, "open")
