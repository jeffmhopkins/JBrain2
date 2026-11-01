"""Install one pinned Chrome for Testing build into a directory, sha256-checked.

Every browser sidecar that reads untrusted pages (htmlrender, browser, reader) takes its
Chromium from here instead of from its base image. Base images lag Chrome's weekly security
releases by months (Playwright bundles a pre-stable branch build; the reader image froze in
2024), and a pinned base tag never moves, so the weekly base-image refresh cannot catch them
up. One pin file means one place to bump when Chrome ships a fix.

Usage: python -I fetch.py chrome|chrome-headless-shell DEST
"""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

_PIN = Path(__file__).with_name("chrome-for-testing.json")
_BASE = "https://storage.googleapis.com/chrome-for-testing-public"
_PLATFORMS = {"x86_64": "linux64", "amd64": "linux64", "aarch64": "linux-arm64"}


def main(product: str, dest: Path) -> None:
    pin = json.loads(_PIN.read_text())
    plat = _PLATFORMS[platform.machine().lower()]
    name = f"{product}-{plat}"
    want = pin["sha256"][name]
    url = f"{_BASE}/{pin['version']}/{plat}/{name}.zip"
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / f"{name}.zip"
        with urllib.request.urlopen(url, timeout=300) as resp, archive.open("wb") as out:
            shutil.copyfileobj(resp, out)
        got = hashlib.sha256(archive.read_bytes()).hexdigest()
        if got != want:
            sys.exit(f"{name}: sha256 {got} != pinned {want}")
        dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                # Strip the archive's single top-level dir so the binary lands at DEST/<product>.
                rel = Path(*Path(info.filename).parts[1:])
                if not rel.parts or info.is_dir():
                    continue
                target = dest / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, target.open("wb") as out:
                    shutil.copyfileobj(src, out)
                # zipfile drops Unix modes; without this the binary is not executable.
                mode = info.external_attr >> 16
                if mode:
                    target.chmod(mode & 0o777)
    print(f"installed {name} {pin['version']} -> {dest}")


if __name__ == "__main__":
    main(sys.argv[1], Path(sys.argv[2]))
