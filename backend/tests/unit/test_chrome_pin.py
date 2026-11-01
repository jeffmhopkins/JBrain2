"""The pinned Chrome every page-rendering sidecar runs, asserted on the files the box builds from.

htmlrender, the browser agent and the reader all render pages a model or the open web wrote.
Their base images lag Chrome's weekly security releases by months, so each one replaces its
bundled browser with the one Chrome for Testing build pinned in deploy/chrome/. These tests
keep a sidecar from quietly drifting back to its base image's browser, and drive the fetch
script that does the swap against a fake download.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import re
import sys
import types
import zipfile
from pathlib import Path
from typing import Any

import pytest
import yaml

_DEPLOY = Path(__file__).resolve().parents[3] / "deploy"
_PIN = _DEPLOY / "chrome" / "chrome-for-testing.json"
_FETCH = _DEPLOY / "chrome" / "fetch.py"
_SIDECARS = {
    "htmlrender": ("Dockerfile.htmlrender", "chrome-headless-shell"),
    "browser": ("Dockerfile.browser", "chrome"),
    "reader": ("Dockerfile.reader", "chrome"),
}


def _compose() -> dict[str, Any]:
    return yaml.safe_load((_DEPLOY / "docker-compose.yml").read_text())


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_pin_names_a_stable_version_and_a_hash_for_every_artifact() -> None:
    pin = json.loads(_PIN.read_text())
    assert re.fullmatch(r"\d+\.0\.\d+\.\d+", pin["version"])
    wanted = {
        f"{product}-{plat}"
        for product in ("chrome", "chrome-headless-shell")
        for plat in ("linux64", "linux-arm64")
    }
    assert set(pin["sha256"]) == wanted
    assert all(re.fullmatch(r"[0-9a-f]{64}", h) for h in pin["sha256"].values())


@pytest.mark.parametrize("service", sorted(_SIDECARS))
def test_each_sidecar_installs_the_pinned_chrome_and_drops_its_own(service: str) -> None:
    dockerfile, product = _SIDECARS[service]
    text = (_DEPLOY / dockerfile).read_text()
    assert f"fetch.py {product} /opt/chrome" in text
    assert "COPY --from=chrome /opt/chrome /opt/chrome" in text
    # The base image's browser is deleted, so nothing can silently fall back to it.
    assert re.search(r"rm -rf /ms-playwright/chromium\*|dpkg --purge google-chrome-stable", text)
    spec = _compose()["services"][service]
    assert spec["build"]["dockerfile"] == f"deploy/{dockerfile}"
    assert spec["image"] == f"jbrain2-{service}:local"


@pytest.mark.parametrize(
    ("service", "arg"), [("browser", "BROWSER_BASE"), ("reader", "READER_BASE")]
)
def test_the_upstream_images_stay_pinned_by_digest(service: str, arg: str) -> None:
    base = _compose()["services"][service]["build"]["args"][arg]
    default = base.split(":-", 1)[1].rstrip("}")
    assert re.search(r"@sha256:[0-9a-f]{64}$", default)
    assert f"ARG {arg}\n" in (_DEPLOY / _SIDECARS[service][0]).read_text()


def test_the_browser_agent_launches_the_pinned_chrome() -> None:
    assert "--executable-path=/opt/chrome/chrome" in _compose()["services"]["browser"]["command"]


def test_the_reader_points_puppeteer_at_the_pinned_chrome() -> None:
    text = (_DEPLOY / "Dockerfile.reader").read_text()
    assert "ENV PUPPETEER_EXECUTABLE_PATH=/opt/chrome/chrome\n" in text


@pytest.mark.asyncio
async def test_htmlrender_launches_the_binary_its_image_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched: dict[str, Any] = {}

    class _Chromium:
        async def launch(self, **kwargs: Any) -> object:
            launched.update(kwargs)
            return object()

    class _Playwright:
        chromium = _Chromium()

    class _Starter:
        async def start(self) -> _Playwright:
            return _Playwright()

    fake = types.ModuleType("playwright.async_api")
    fake.Browser = object  # type: ignore[attr-defined]
    fake.async_playwright = _Starter  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake)
    monkeypatch.setenv("HTMLRENDER_CHROME", "/opt/chrome/chrome-headless-shell")
    server = _load(_DEPLOY / "htmlrender" / "server.py", "htmlrender_chrome_under_test")
    await server._get_browser()
    assert launched["executable_path"] == "/opt/chrome/chrome-headless-shell"
    assert "/opt/chrome/chrome-headless-shell" in (_DEPLOY / "Dockerfile.htmlrender").read_text()


# --- fetch.py ---------------------------------------------------------------------------


def _archive() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        exe = zipfile.ZipInfo("chrome-linux64/chrome")
        exe.external_attr = 0o100755 << 16
        zf.writestr(exe, b"\x7fELF")
        zf.writestr("chrome-linux64/locales/en-US.pak", b"pak")
    return buf.getvalue()


def _fetcher(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, digest: str) -> tuple[Any, list[str]]:
    module = _load(_FETCH, "chrome_fetch_under_test")
    pin = tmp_path / "pin.json"
    pin.write_text(json.dumps({"version": "1.0.0.1", "sha256": {"chrome-linux64": digest}}))
    urls: list[str] = []

    def _urlopen(url: str, timeout: float) -> io.BytesIO:
        urls.append(url)
        return io.BytesIO(_archive())

    monkeypatch.setattr(module, "_PIN", pin)
    monkeypatch.setattr(module.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(module.urllib.request, "urlopen", _urlopen)
    return module, urls


def test_fetch_installs_the_verified_build_flat_and_executable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module, urls = _fetcher(monkeypatch, tmp_path, hashlib.sha256(_archive()).hexdigest())
    dest = tmp_path / "opt"
    module.main("chrome", dest)
    assert urls == [
        "https://storage.googleapis.com/chrome-for-testing-public/1.0.0.1/linux64/chrome-linux64.zip"
    ]
    assert (dest / "chrome").stat().st_mode & 0o111
    assert (dest / "locales" / "en-US.pak").read_bytes() == b"pak"


def test_fetch_refuses_a_download_that_does_not_match_the_pin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module, _ = _fetcher(monkeypatch, tmp_path, "0" * 64)
    dest = tmp_path / "opt"
    with pytest.raises(SystemExit, match="sha256"):
        module.main("chrome", dest)
    assert not dest.exists()
