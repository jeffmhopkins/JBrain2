"""Fetch, unpack and configure Bedrock Dedicated Server inside the data volume.

BDS lives in the VOLUME, not the image, because Bedrock clients only join a server on
their exact version and clients auto-update: the server has to follow them on a
container restart, not on an image rebuild the owner would have to trigger
(docs/plans/MINECRAFT_BEDROCK_PLAN.md §M3). An update never overwrites what the owner
or the game wrote — the world, the allowlist and the permission files — only Mojang's
own binaries and data.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import urllib.request
import zipfile
from collections.abc import Mapping
from pathlib import Path

LINKS_URL = "https://net-secondary.web.minecraft-services.net/api/v1.0/download/links"
ZIP_URL = (
    "https://www.minecraft.net/bedrockdedicatedserver/bin-linux/bedrock-server-{v}.zip"
)
# minecraft.net refuses requests without a browser-shaped agent.
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) jbrain-minecraft"
VERSION_FILE = ".bds_version"
# Written by the owner or by the game; an update must never replace them.
PRESERVE = frozenset(
    {"server.properties", "allowlist.json", "permissions.json", "worlds"}
)
_VERSION_RE = re.compile(r"bedrock-server-([0-9][0-9.]*[0-9])\.zip$")


def _get(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def version_from_url(url: str) -> str:
    m = _VERSION_RE.search(url)
    if not m:
        raise ValueError(f"no version in download url: {url}")
    return m.group(1)


def latest_url(fetch=_get) -> str:
    links = json.loads(fetch(LINKS_URL, 20))["result"]["links"]
    for link in links:
        if link.get("downloadType") == "serverBedrockLinux":
            return str(link["downloadUrl"])
    raise ValueError("no serverBedrockLinux link in the download index")


def resolve(want: str, fetch=_get) -> tuple[str, str]:
    """(version, url) for `latest` or an exact pinned version like 1.26.52.3."""
    if want in ("", "latest"):
        url = latest_url(fetch)
        return version_from_url(url), url
    return want, ZIP_URL.format(v=want)


def installed_version(server_dir: Path) -> str:
    try:
        return (server_dir / VERSION_FILE).read_text().strip()
    except FileNotFoundError:
        return ""


def _top(name: str) -> str:
    return name.split("/", 1)[0]


def extract(data: bytes, server_dir: Path, *, first_install: bool) -> None:
    """Unpack a BDS zip over `server_dir`, refusing any member that escapes it.

    On an update the PRESERVE set is skipped wholesale, so a world folder or an
    allowlist edited in game survives; on a first install the stock files are needed
    as the starting point and are written like everything else."""
    root = server_dir.resolve()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            if not first_install and _top(info.filename) in PRESERVE:
                continue
            target = (root / info.filename).resolve()
            if target != root and root not in target.parents:
                raise ValueError(f"zip member escapes the server dir: {info.filename}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
    binary = server_dir / "bedrock_server"
    if binary.exists():
        binary.chmod(0o755)


def ensure(server_dir: Path, want: str, fetch=_get, log=print) -> str:
    """Install or update BDS to `want`; return the version that will run.

    A failed lookup or download keeps whatever is installed, because a box that lost
    its internet should still serve the LAN — the only hard failure is having no
    server at all."""
    server_dir.mkdir(parents=True, exist_ok=True)
    have = installed_version(server_dir)
    try:
        version, url = resolve(want, fetch)
    except Exception as exc:
        if have:
            log(f"[install] version lookup failed ({exc}); keeping {have}")
            return have
        raise
    if version == have:
        return have
    log(f"[install] downloading BDS {version} (installed: {have or 'none'})")
    try:
        data = fetch(url, 600)
    except Exception as exc:
        if have:
            log(f"[install] download failed ({exc}); keeping {have}")
            return have
        raise
    extract(data, server_dir, first_install=not have)
    (server_dir / VERSION_FILE).write_text(version + "\n")
    log(f"[install] BDS {version} installed")
    return version


def apply_properties(path: Path, overrides: dict[str, str]) -> None:
    """Set keys in server.properties in place, keeping Mojang's comments and order.

    Keys the stock file only has commented out (`# server-ip=`) are appended rather
    than uncommented, so the documentation lines stay intact."""
    lines = path.read_text().splitlines() if path.exists() else []
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if not line.lstrip().startswith("#") and "=" in line and key in overrides:
            out.append(f"{key}={overrides[key]}")
            seen.add(key)
        else:
            out.append(line)
    out.extend(f"{k}={v}" for k, v in overrides.items() if k not in seen)
    path.write_text("\n".join(out) + "\n")


def env_overrides(env: Mapping[str, str]) -> dict[str, str]:
    """The server.properties keys this box pins, from the compose environment.

    `transport=raknet` is deliberate: BDS 1.26 defaults to `nethernet`, which can
    allocate client UDP ports beyond 19132 that a single published port does not
    cover. RakNet is one port plus LAN discovery on it — what M0 tests first.
    `content-log-console-output-enabled` puts script `console.log` on stdout, which is
    the companion bridge's return channel (M5)."""
    out = {
        "server-name": env.get("MC_SERVER_NAME", "JBrain"),
        "level-name": env.get("MC_LEVEL_NAME", "world"),
        "gamemode": env.get("MC_GAMEMODE", "survival"),
        "difficulty": env.get("MC_DIFFICULTY", "normal"),
        "allow-list": env.get("MC_ALLOW_LIST", "false"),
        "online-mode": "true",
        "transport": env.get("MC_TRANSPORT", "raknet"),
        "enable-lan-visibility": "true",
        "server-port": "19132",
        "server-portv6": "19133",
        "content-log-console-output-enabled": "true",
    }
    seed = env.get("MC_LEVEL_SEED", "")
    if seed:
        out["level-seed"] = seed
    return out
