"""The jcode OCR bridge (api.jcode_llm ocr): shared-token auth, RapidOCR passthrough for the
sandbox's `ocr` helper, PDF page-by-page rasterization, and the fail modes. Fake sidecar,
no network (docs/plans/RAPIDOCR_PLAN.md)."""

from types import SimpleNamespace

import httpx
import pymupdf
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jbrain.api import jcode_llm
from jbrain.vision import OcrResult, OcrServiceError

_AUTH = {"Authorization": "Bearer sk-test"}


class _FakeRapid:
    def __init__(self, *results: OcrResult, error: bool = False) -> None:
        self._results = list(results)
        self._error = error
        self.calls = 0

    async def ocr(self, data: bytes, media_type: str = "application/octet-stream") -> OcrResult:
        self.calls += 1
        if self._error:
            raise OcrServiceError("sidecar down")
        return self._results[min(self.calls - 1, len(self._results) - 1)]


def _app(*, token: str = "sk-test", rapidocr: object = "unset") -> FastAPI:
    app = FastAPI()
    app.include_router(jcode_llm.router, prefix="/api")
    app.state.settings = SimpleNamespace(jcode_gateway_token=token)
    if rapidocr != "unset":
        app.state.rapidocr = rapidocr
    return app


def _post(client: TestClient, data: bytes, content_type: str) -> httpx.Response:
    return client.post(
        "/api/jcode/llm/v1/ocr",
        content=data,
        headers={**_AUTH, "Content-Type": content_type},
    )


def test_ocr_reads_an_image() -> None:
    fake = _FakeRapid(OcrResult("Traceback (most recent call last)", 0.9))
    resp = _post(TestClient(_app(rapidocr=fake)), b"\x89PNGdata", "image/png")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "Traceback (most recent call last)" in resp.text
    assert fake.calls == 1


def test_ocr_reads_a_pdf_page_by_page() -> None:
    doc = pymupdf.open()
    doc.new_page()
    doc.new_page()
    fake = _FakeRapid(OcrResult("page one", 0.9), OcrResult("page two", 0.9))
    resp = _post(TestClient(_app(rapidocr=fake)), doc.tobytes(), "application/pdf")
    assert resp.status_code == 200
    assert "[page 1]" in resp.text and "page one" in resp.text
    assert "[page 2]" in resp.text and "page two" in resp.text
    assert fake.calls == 2


def test_ocr_no_text_message() -> None:
    resp = _post(TestClient(_app(rapidocr=_FakeRapid(OcrResult("  ", 0.0)))), b"img", "image/png")
    assert resp.status_code == 200
    assert "No legible text" in resp.text


def test_ocr_empty_body_is_400() -> None:
    resp = _post(TestClient(_app(rapidocr=_FakeRapid())), b"", "image/png")
    assert resp.status_code == 400


def test_ocr_503_when_sidecar_absent() -> None:
    resp = _post(TestClient(_app(rapidocr="unset")), b"img", "image/png")
    assert resp.status_code == 503


def test_ocr_502_on_service_error() -> None:
    resp = _post(TestClient(_app(rapidocr=_FakeRapid(error=True))), b"img", "image/png")
    assert resp.status_code == 502


def test_ocr_rejects_a_bad_token() -> None:
    resp = TestClient(_app(rapidocr=_FakeRapid())).post(
        "/api/jcode/llm/v1/ocr",
        content=b"img",
        headers={"Authorization": "Bearer nope", "Content-Type": "image/png"},
    )
    assert resp.status_code == 401
