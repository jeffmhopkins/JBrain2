"""World slots and the backup index (docs/plans/MINECRAFT_BEDROCK_PLAN.md §M2, §M3).

The server holds several worlds, one loaded at a time. A **slot** is a folder under
`worlds/` plus a record here (name, seed, per-world game settings). Slot 1's folder is
`world`, the name the M0a rig already used, so the existing world becomes slot 1 with
nothing moved. Every other slot's folder is its id.

Backups are `.mcworld` files in the snapshot folder. This index adds what a filename
can't hold: which slot a backup belongs to, whether it is automatic or the owner's,
whether it is pinned, and when a copy last left the box (Minecraft backups are kept
apart from the box backups, so a download is the only off-box copy).
"""

from __future__ import annotations

import json
import re
import secrets
import time
import zipfile
from pathlib import Path
from typing import Any

DEFAULT_SLOTS = 5
# Newest N unpinned backups kept per world; automatic ones go before the owner's.
KEEP_PER_SLOT = 20
# An imported .mcworld is a zip of a world; bigger than this is not a family world.
MAX_IMPORT_BYTES = 1024 * 1024 * 1024
GAMEMODES = ("survival", "creative", "adventure")
# The world rules this BDS version reports (`gamerule` on 1.26.52.3), with its defaults.
# Used to validate a change for a world that isn't loaded; a loaded world's live list
# (parsed from the console) supersedes it, so a new rule in a later version just works.
DEFAULT_RULES: dict[str, bool | int | str] = {
    "commandBlockOutput": True,
    "doDayLightCycle": True,
    "doEntityDrops": True,
    "doFireTick": True,
    "recipesUnlock": True,
    "doLimitedCrafting": False,
    "doMobLoot": True,
    "doMobSpawning": True,
    "doTileDrops": True,
    "doWeatherCycle": True,
    "drowningDamage": True,
    "fallDamage": True,
    "fireDamage": True,
    "keepInventory": False,
    "mobGriefing": True,
    "pvp": True,
    "showCoordinates": False,
    "playerWaypoints": "everyone",
    "locatorbar": True,
    "showDaysPlayed": False,
    "naturalRegeneration": True,
    "tntExplodes": True,
    "sendCommandFeedback": True,
    "maxCommandChainLength": 65535,
    "doInsomnia": True,
    "commandBlocksEnabled": True,
    "randomTickSpeed": 1,
    "doImmediateRespawn": False,
    "showDeathMessages": True,
    "functionCommandLimit": 10000,
    "spawnRadius": 10,
    "showTags": True,
    "freezeDamage": True,
    "respawnBlocksExplode": True,
    "showBorderEffect": True,
    "showRecipeMessages": True,
    "playersSleepingPercentage": 100,
    "projectilesCanBreakBlocks": True,
    "tntExplosionDropDecay": False,
}
_RULE_TOKEN = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")
_SEED_MAX = 64
DIFFICULTIES = ("peaceful", "easy", "normal", "hard")


def slot_ids(count: int) -> list[str]:
    return [f"slot{i}" for i in range(1, count + 1)]


def folder_of(slot_id: str) -> str:
    return "world" if slot_id == "slot1" else slot_id


class SlotStore:
    def __init__(
        self, path: Path, worlds_dir: Path, count: int = DEFAULT_SLOTS
    ) -> None:
        self.path = path
        self.worlds_dir = worlds_dir
        self.count = count

    def _raw(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            raw = {}
        return {str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)}

    def all(self) -> list[dict[str, Any]]:
        raw = self._raw()
        out = []
        for sid in slot_ids(self.count):
            rec = {
                "id": sid,
                "folder": folder_of(sid),
                "name": None,
                "seed": None,
                "gamemode": "survival",
                "difficulty": "normal",
                "cheats": False,
                "origin": None,
                **raw.get(sid, {}),
            }
            rec["exists"] = self.exists(rec["folder"])
            if rec["exists"] and not rec["name"]:
                # A world that predates the slot records (the M0a rig's) gets a name.
                rec["name"] = "World" if sid == "slot1" else sid
            out.append(rec)
        return out

    def get(self, slot_id: str) -> dict[str, Any]:
        for rec in self.all():
            if rec["id"] == slot_id:
                return rec
        raise ValueError(f"no such slot: {slot_id}")

    def by_folder(self, folder: str) -> dict[str, Any] | None:
        return next((r for r in self.all() if r["folder"] == folder), None)

    def update(self, slot_id: str, **fields: Any) -> dict[str, Any]:
        self.get(slot_id)  # validates the id
        raw = self._raw()
        rec = raw.get(slot_id, {})
        for key, value in fields.items():
            if key == "gamemode" and value not in GAMEMODES:
                raise ValueError(f"game mode must be one of {', '.join(GAMEMODES)}")
            if key == "difficulty" and value not in DIFFICULTIES:
                raise ValueError(f"difficulty must be one of {', '.join(DIFFICULTIES)}")
            if key == "name" and value is not None:
                value = " ".join(str(value).split())[:40] or None
            rec[key] = value
        raw[slot_id] = rec
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(raw, indent=2, sort_keys=True))
        return self.get(slot_id)

    def clear(self, slot_id: str) -> None:
        raw = self._raw()
        raw.pop(slot_id, None)
        self.path.write_text(json.dumps(raw, indent=2, sort_keys=True))

    def exists(self, folder: str) -> bool:
        d = self.worlds_dir / folder
        return (d / "level.dat").is_file() or (d / "db").is_dir()


def new_seed() -> str:
    # A seed chosen here rather than by BDS, so "reset with the same seed" stays
    # possible for every world this box created.
    return str(secrets.randbelow(2**63))


def dir_size(path: Path) -> int:
    return (
        sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        if path.is_dir()
        else 0
    )


class SnapshotIndex:
    def __init__(self, path: Path, snapshot_dir: Path) -> None:
        self.path = path
        self.dir = snapshot_dir

    def _raw(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            raw = {}
        raw.setdefault("snapshots", {})
        raw.setdefault("downloads", {})
        return raw

    def _save(self, raw: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(raw, indent=2, sort_keys=True))

    def record(self, name: str, folder: str, label: str, auto: bool) -> None:
        raw = self._raw()
        raw["snapshots"][name] = {
            "folder": folder,
            "label": label,
            "auto": auto,
            "pinned": False,
        }
        self._save(raw)

    def entry(self, name: str) -> dict[str, Any]:
        """A snapshot's record. Files with no record predate the index: their folder
        is the filename prefix, and a `pre-` label marks the automatic ones."""
        rec = self._raw()["snapshots"].get(name)
        if rec:
            return dict(rec)
        folder = name.split("-2", 1)[0] if "-2" in name else "world"
        return {"folder": folder, "label": "", "auto": "-pre-" in name, "pinned": False}

    def set_pinned(self, name: str, pinned: bool) -> None:
        raw = self._raw()
        rec = raw["snapshots"].setdefault(name, self.entry(name))
        rec["pinned"] = pinned
        self._save(raw)

    def forget(self, name: str) -> None:
        raw = self._raw()
        raw["snapshots"].pop(name, None)
        self._save(raw)

    def note_download(self, folder: str) -> None:
        raw = self._raw()
        raw["downloads"][folder] = time.time()
        self._save(raw)

    def last_download(self, folder: str) -> float | None:
        return self._raw()["downloads"].get(folder)

    def listing(self, folder: str | None = None) -> list[dict[str, Any]]:
        if not self.dir.is_dir():
            return []
        out = []
        for p in sorted(
            self.dir.glob("*.mcworld"), key=lambda p: p.stat().st_mtime, reverse=True
        ):
            rec = self.entry(p.name)
            if folder is not None and rec["folder"] != folder:
                continue
            st = p.stat()
            out.append(
                {"name": p.name, "bytes": st.st_size, "created": st.st_mtime, **rec}
            )
        return out

    def prune(self, folder: str, keep: int = KEEP_PER_SLOT) -> list[str]:
        """Keep the newest `keep` unpinned backups of one world. Automatic ones are
        removed before the owner's; pinned ones are never counted or removed."""
        unpinned = [s for s in self.listing(folder) if not s["pinned"]]
        excess = len(unpinned) - keep
        removed: list[str] = []
        if excess <= 0:
            return removed
        oldest_first = sorted(unpinned, key=lambda s: s["created"])
        for pick_auto in (True, False):
            for s in oldest_first:
                if len(removed) == excess:
                    break
                if s["auto"] == pick_auto and s["name"] not in removed:
                    (self.dir / s["name"]).unlink(missing_ok=True)
                    self.forget(s["name"])
                    removed.append(s["name"])
        return removed


def check_world_zip(path: Path) -> str:
    """Validate an uploaded .mcworld; return the prefix its world sits under ("" when
    level.dat is at the root, "Name/" when the zip wraps one folder). Refuses anything
    that isn't a Bedrock world or that would write outside the slot."""
    if path.stat().st_size > MAX_IMPORT_BYTES:
        raise ValueError("that file is too big to be a Bedrock world (over 1 GB)")
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise ValueError("that isn't a .mcworld (not a zip file)") from exc
    with zf:
        names = zf.namelist()
        for n in names:
            parts = Path(n).parts
            if n.startswith("/") or ".." in parts:
                raise ValueError(f"the file has an unsafe path inside it: {n}")
        if "level.dat" in names:
            return ""
        tops = {n.split("/", 1)[0] for n in names if "/" in n}
        if len(tops) == 1:
            top = next(iter(tops))
            if f"{top}/level.dat" in names:
                return f"{top}/"
    raise ValueError("that isn't a Bedrock world (no level.dat inside)")


def extract_world(path: Path, prefix: str, dest: Path) -> None:
    dest.mkdir(parents=True)
    root = dest.resolve()
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if not info.filename.startswith(prefix) or info.filename == prefix:
                continue
            rel = info.filename[len(prefix) :]
            target = (root / rel).resolve()
            if root not in target.parents:
                raise ValueError(f"unsafe path inside the world: {info.filename}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                while chunk := src.read(1 << 20):
                    out.write(chunk)


def level_name(world: Path) -> str | None:
    try:
        return (world / "levelname.txt").read_text(encoding="utf-8").strip()[
            :40
        ] or None
    except (FileNotFoundError, UnicodeDecodeError):
        return None


def parse_gamerules(lines: list[str]) -> dict[str, bool | int | str]:
    """`gamerule` with no arguments prints every rule on one line:
    "doFireTick = true, randomTickSpeed = 1, playerWaypoints = everyone, …"."""
    out: dict[str, bool | int | str] = {}
    for line in lines:
        body = line.split("]", 1)[1] if line.startswith("[") else line
        for part in body.split(","):
            if "=" not in part:
                continue
            key, value = (x.strip() for x in part.split("=", 1))
            if not _RULE_TOKEN.match(key):
                continue
            out[key] = coerce_rule(value)
    return out


def coerce_rule(value: Any) -> bool | int | str:
    if isinstance(value, bool):
        return value
    text = str(value).strip()
    if text in ("true", "false"):
        return text == "true"
    try:
        return int(text)
    except ValueError:
        return text


def check_rules(
    changes: dict[str, Any], known: dict[str, bool | int | str]
) -> dict[str, bool | int | str]:
    """Validate a change set against the known rules: the name must exist and the value
    must have the rule's type (a choice rule takes a single lowercase word)."""
    out: dict[str, bool | int | str] = {}
    for key, raw in changes.items():
        if key not in known:
            raise ValueError(f"unknown world rule: {key}")
        current, value = known[key], coerce_rule(raw)
        if type(value) is not type(current):
            raise ValueError(f"{key} takes a {type(current).__name__}")
        if isinstance(value, int) and not isinstance(value, bool) and value < 0:
            raise ValueError(f"{key} can't be negative")
        if isinstance(value, str) and not re.fullmatch(r"[a-z_]{1,32}", value):
            raise ValueError(f"{key}: not a valid choice")
        out[key] = value
    return out


def rule_command(key: str, value: bool | int | str) -> str:
    shown = ("true" if value else "false") if isinstance(value, bool) else str(value)
    return f"gamerule {key} {shown}"


def check_seed(seed: str) -> str:
    """Bedrock's create-world box takes any text as a seed; BDS hashes non-numbers."""
    seed = seed.strip()
    if len(seed) > _SEED_MAX or any(c in seed for c in "\r\n"):
        raise ValueError(f"a seed is up to {_SEED_MAX} characters on one line")
    return seed
