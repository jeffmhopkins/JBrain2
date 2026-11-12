"""A persona's soul: upstream's `pb2_soul` document (soul_format 3, `growbot/site/v3/soul.js`).

The box stores the document whole and the creature's own loader (`normalizeSoul`) fills in any
part it lacks, so a fresh creature is a near-empty soul and the engine's defaults speak for it.
What the box writes itself (an owner's edit, an import, a seed) is cleaned here the way the
creature cleans an import (`sanitizeSoulCfg`), because the phone loads a pulled soul as its own
and never re-sanitises it. The child-safety floor is not in the soul at all: `prompts.js`
appends it after any persona, so nothing here can remove it.
"""

from __future__ import annotations

import copy
import json
import math
import re
import time
from typing import Any

SOUL_FORMAT = 3
# Upstream's snapshot export of a grown creature is ~15 KB; a whole diary under the big caps
# stays well inside this. The same cap holds for a phone's push and an owner's import.
MAX_SOUL_BYTES = 1_000_000

# The config regions the owner may edit from the PWA (soul.js "persona/dream/harness/body/meta/
# expression — load-time config").
REGIONS = ("persona", "expression", "meta", "dream", "harness", "body")
# Plus the memory surfaces, which the creature writes and the owner may correct (owner,
# 2026-11-12). They are not config, so an import keeps a file's own rather than cleaning them.
MEMORIES = ("identity", "identityCore", "scratch", "goals", "log")
EDITABLE = (*REGIONS, *MEMORIES)
# soul.js: normalizeSoul reseeds an identity under 20 characters; SOUL_CAPS_BIG is the ceiling
# on the identity, the rules and the diary; soulReact, dreamCommit and soulLog cap each line.
IDENTITY_MIN = 20
IDENTITY_MAX = 1600
STATE_CAP = 90
RULE_CAP = 90
RULES_MAX = 24
WANT_CAP = 80
WANTS_MAX = 4
SELF_NAME_CAP = 32
LOG_LINE_CAP = 140
LOG_MAX = 1000
DATE_MAX_MS = 8.64e15
# Free-text regions, with the field that holds the text and its cap in sanitizeSoulCfg.
TEXT_REGIONS = {"persona": ("constitution", 6000), "dream": ("system_prompt", 8000)}
JSON_REGIONS = ("expression", "harness", "body")
# soul.js IDENTITY_CORE_CAP: the bond line's length.
IDENTITY_CORE_CAP = 240
# A JSON region's own cap, far past any real one (body.json is ~2 KB).
MAX_REGION_BYTES = 64_000


class SoulError(ValueError):
    """A soul or a region the box will not store; the message is the owner's to read."""


class SoulTooBig(SoulError):
    """Past `MAX_SOUL_BYTES`."""


def clean(value: Any, cap: int = 400) -> str:
    """soul.js `soulClean`: collapse whitespace, collapse `==+` (the prompt's section marker),
    trim, cap."""
    s = "" if value is None else str(value)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"==+", "=", s)
    return s.strip()[:cap]


def obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _size(doc: Any) -> int:
    return len(json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode())


def _dream_limits(raw: dict[str, Any]) -> dict[str, float | int]:
    """soul.js `cleanDreamLimits`: only the known numbers, each positive and finite, clamped;
    anything else is left out so the creature's default applies."""
    out: dict[str, float | int] = {}
    bounds = {
        "identity_cap_chars": (800, 1600, True),
        "identity_min_after_drop": (20, 1600, False),
        "longing_step": (0, 1, False),
        "wants_max": (1, 4, True),
        "add_words_max": (1, 160, True),
    }
    for key, (lo, hi, whole) in bounds.items():
        v = raw.get(key)
        if isinstance(v, bool) or not isinstance(v, int | float):
            continue
        try:
            if not math.isfinite(v) or v <= 0:
                continue
        except OverflowError:
            # An integer past a double: JSON.parse makes it Infinity, which upstream drops.
            continue
        clamped = min(hi, max(lo, v))
        out[key] = math.floor(clamped) if whole else clamped
    return out


def sanitize_cfg(raw: dict[str, Any]) -> dict[str, Any]:
    """soul.js `sanitizeSoulCfg`, the regions a soul file may carry, each cleaned and capped."""
    cfg: dict[str, Any] = {}
    persona = raw.get("persona")
    if isinstance(persona, dict):
        p: dict[str, Any] = {}
        if isinstance(persona.get("constitution"), str):
            p["constitution"] = clean(persona["constitution"], 6000)
        if isinstance(persona.get("prompt_order"), list):
            p["prompt_order"] = persona["prompt_order"]
        if isinstance(persona.get("output_contract"), dict):
            p["output_contract"] = persona["output_contract"]
        cfg["persona"] = p
    dream = raw.get("dream")
    if isinstance(dream, dict):
        d: dict[str, Any] = {}
        if isinstance(dream.get("system_prompt"), str):
            d["system_prompt"] = clean(dream["system_prompt"], 8000)
        if isinstance(dream.get("limits"), dict):
            d["limits"] = _dream_limits(dream["limits"])
        if isinstance(dream.get("scene_vocab"), list):
            words = [re.sub(r"[^a-z]", "", str(w).lower()) for w in dream["scene_vocab"]]
            d["scene_vocab"] = [w for w in words if w][:40]
        cfg["dream"] = d
    harness = raw.get("harness")
    if isinstance(harness, dict):
        h: dict[str, Any] = {}
        models = harness.get("models")
        if isinstance(models, dict):
            h["models"] = {
                k: clean(models[k], 80) for k in ("fast", "dream") if isinstance(models.get(k), str)
            }
        mt = harness.get("max_tokens")
        if isinstance(mt, int | float) and not isinstance(mt, bool):
            h["max_tokens"] = max(48, min(1024, int(mt)))
        if isinstance(harness.get("timing"), dict):
            h["timing"] = harness["timing"]
        cfg["harness"] = h
    if isinstance(raw.get("body"), dict):
        cfg["body"] = copy.deepcopy(raw["body"])
    if isinstance(raw.get("expression"), dict):
        cfg["expression"] = copy.deepcopy(raw["expression"])
    meta = raw.get("meta")
    if isinstance(meta, dict):
        cfg["meta"] = {
            "name": clean(meta.get("name"), 80),
            "author": clean(meta.get("author"), 60),
            "description": clean(meta.get("description"), 500),
            "based_on": clean(meta.get("based_on") or "", 80),
            "kind": "snapshot" if meta.get("kind") == "snapshot" else "template",
        }
    return cfg


def fresh(name: str) -> dict[str, Any]:
    """A fresh creature: upstream's own seed, which the phone's `normalizeSoul` fills in (seed
    identity, wants, first diary line); only its name is the box's."""
    return {
        "soul_format": SOUL_FORMAT,
        "bornAt": 0,
        "meta": {
            "name": clean(name, 80),
            "author": "",
            "description": "",
            "based_on": "growbot-default@soul3",
            "kind": "template",
        },
    }


def seeded(name: str, source: dict[str, Any], based_on: str) -> dict[str, Any]:
    """A new persona from another soul's personality: its persona, expression and dream regions,
    with fresh memories (a seed is a personality; a duplicate is a creature)."""
    soul = fresh(name)
    cfg = sanitize_cfg(source)
    for region in ("persona", "expression", "dream"):
        if region in cfg:
            soul[region] = cfg[region]
    soul["meta"]["based_on"] = clean(based_on, 80)
    return soul


def check_size(soul: dict[str, Any]) -> None:
    size = _size(soul)
    if size > MAX_SOUL_BYTES:
        raise SoulTooBig(
            f"That soul is {size / 1e6:.1f} MB, and a soul is at most "
            f"{MAX_SOUL_BYTES // 1_000_000} MB. Nothing changed."
        )


def from_phone(soul: Any) -> dict[str, Any]:
    """A soul the creature wrote itself (`saveSoul`): stored as it is, within the cap. The phone
    is the owner's, opened from a ticket, and its loader cleans what it reads."""
    if not isinstance(soul, dict) or soul.get("soul_format") != SOUL_FORMAT:
        raise SoulError("not a soul_format 3 document")
    check_size(soul)
    return soul


def from_file(raw: Any) -> dict[str, Any]:
    """An imported soul file: upstream's own export (or its soul gallery's), soul_format 3.

    The config regions are cleaned as the creature cleans an import. The memory surfaces are
    kept as the file has them (the phone's `normalizeSoul` repairs their shapes on load), and
    the whole is size-capped first, so a hostile file costs nothing to refuse."""
    if not isinstance(raw, dict):
        raise SoulError("That isn't a soul file — it has no soul format in it. Nothing changed.")
    check_size(raw)
    if raw.get("soul_format") != SOUL_FORMAT:
        if isinstance(raw.get("soul_format"), int) or "identity" in raw:
            raise SoulError(
                "That soul file is an older format. Open it in the creature's MIND tab and "
                "export it again. Nothing changed."
            )
        raise SoulError("That isn't a soul file — it has no soul format in it. Nothing changed.")
    soul = {k: copy.deepcopy(v) for k, v in raw.items() if k not in REGIONS}
    soul.update(sanitize_cfg(raw))
    meta = obj(raw.get("meta"))
    if meta.get("kind") == "snapshot":
        # A grown creature comes back with its own bond line, cleaned and capped.
        if isinstance(soul.get("identityCore"), str):
            soul["identityCore"] = clean(soul["identityCore"], IDENTITY_CORE_CAP)
        else:
            soul.pop("identityCore", None)
    else:
        _as_template(soul)
    # The box keeps its own revision; a file's would only confuse the phone's merge.
    for key in ("rev", "savedAt", "writeId", "readOnly", "merged"):
        soul.pop(key, None)
    return soul


def _as_template(soul: dict[str, Any]) -> None:
    """soul.js `soulFromFile`'s template branch: a character to be born fresh. Its bond has not
    happened yet (keeping the uploader's would shut the append-once door from birth), it has no
    diary, and the person's own choices (its name, their name, languages) belong to the
    creature whose person made them."""
    soul["identityCore"] = ""
    soul["log"] = []
    soul["bornAt"] = 0
    scratch = obj(soul.get("scratch"))
    soul["scratch"] = {
        "state": "awake and curious",
        "mood": scratch.get("mood") or {"v": 0.3, "e": 0.5},
        "rules": scratch["rules"] if isinstance(scratch.get("rules"), list) else [],
        "reflexes": scratch["reflexes"] if isinstance(scratch.get("reflexes"), list) else [],
        "traces": [],
        "pendingProposal": "",
    }
    goals = obj(soul.get("goals"))
    longing = goals.get("longing")
    soul["goals"] = {
        "wants": goals["wants"][:4] if isinstance(goals.get("wants"), list) else [],
        "longing": longing
        if isinstance(longing, int | float) and not isinstance(longing, bool)
        else 0.2,
        "call": goals["call"] if isinstance(goals.get("call"), list) else [],
        "nextTry": "",
        "funLog": [],
        "ladder": {"for": "", "rungs": []},
    }


def edit_region(soul: dict[str, Any], region: str, value: Any) -> dict[str, Any]:
    """`soul` with one config region replaced by the owner's `value`, cleaned as an import.

    `value` is the text for persona, dream and identity, `{name, description}` for meta, and an
    object for expression, harness and body; the other memory surfaces are `_edit_memory`'s.
    Returns a new document; `soul` is not touched."""
    if region not in EDITABLE:
        raise SoulError("That part of the soul can't be edited here.")
    out = copy.deepcopy(soul)
    if region in MEMORIES:
        _edit_memory(out, region, value)
    elif region in TEXT_REGIONS:
        field, cap = TEXT_REGIONS[region]
        if not isinstance(value, str) or not value.strip():
            raise SoulError(
                "It can't be empty — the creature would have no voice. Nothing was saved."
            )
        if len(value) > cap:
            raise SoulError(
                f"That's {len(value)} characters; the most it takes is {cap}. Nothing was saved."
            )
        current = obj(out.get(region))
        merged = {**current, field: value}
        out[region] = sanitize_cfg({region: merged})[region]
    elif region == "meta":
        if not isinstance(value, dict) or not clean(value.get("name"), 80):
            raise SoulError("Give it a name. Nothing was saved.")
        current = obj(out.get("meta"))
        merged = {**current, "name": value.get("name"), "description": value.get("description")}
        out["meta"] = sanitize_cfg({"meta": merged})["meta"]
    else:
        if not isinstance(value, dict):
            raise SoulError("It has to be one { … } block. Nothing was saved.")
        if _size(value) > MAX_REGION_BYTES:
            raise SoulError("That block is too big to be a soul's settings. Nothing was saved.")
        out[region] = sanitize_cfg({region: value})[region]
    check_size(out)
    return out


def _u16(s: str) -> int:
    """A string's length as the phone counts it (`slice` and `maxLength` count UTF-16 units)."""
    return len(s.encode("utf-16-le")) // 2


def _fit16(s: str, cap: int) -> str:
    """`s` cut to `cap` UTF-16 units, never through an emoji's surrogate pair."""
    while _u16(s) > cap:
        s = s[:-1]
    return s


def _storable(s: str) -> str:
    # A NUL or a lone surrogate cannot be stored as jsonb (memories.py `_text` refuses both).
    if "\x00" in s or any("\ud800" <= c <= "\udfff" for c in s):
        raise SoulError("That has a character the box can't keep. Nothing was saved.")
    return s


def _text(value: Any, cap: int, what: str) -> str:
    """The owner's text cleaned as `soulClean`, refused rather than cut past the creature's cap."""
    if not isinstance(value, str):
        raise SoulError(f"{what} has to be text. Nothing was saved.")
    text = _storable(clean(value, len(value)))
    if _u16(text) > cap:
        raise SoulError(
            f"{what} is {_u16(text)} characters; the most it keeps is {cap}. Nothing was saved."
        )
    return text


def _lines(value: Any, cap: int, most: int, what: str) -> list[str]:
    if not isinstance(value, list):
        raise SoulError(f"The {what} have to be a list of lines. Nothing was saved.")
    out = [line for line in (_text(x, cap, f"A line of the {what}") for x in value) if line]
    if len(out) > most:
        raise SoulError(
            f"That's {len(out)} {what}; the most it keeps is {most}. Nothing was saved."
        )
    return out


def _name_clean(value: Any) -> str:
    """next.html `nameClean`, the one cleaner for a creature's name."""
    s = re.sub(r"[\n\r{}\"'<>|]+", " ", "" if value is None else str(value))
    s = re.sub(r"==+", "=", s)
    return _fit16(re.sub(r"\s+", " ", s).strip(), SELF_NAME_CAP)


def _edit_memory(out: dict[str, Any], region: str, value: Any) -> None:
    """One memory surface replaced by the owner's correction, cleaned as the creature cleans
    its own writes. Only the fields the PWA shows are replaced; the rest (mood, longing, the
    ladder, a diary line's ambient flag) are kept as the creature left them."""
    if region == "identity":
        text = _text(value, IDENTITY_MAX, "Who it is")
        if len(text) < IDENTITY_MIN:
            raise SoulError(
                f"It needs at least {IDENTITY_MIN} characters, or the creature starts over "
                "from its seed. Nothing was saved."
            )
        out["identity"] = text
    elif region == "identityCore":
        # Empty is allowed: the bond line is then unwritten, and its dream may write it.
        out["identityCore"] = _text(value, IDENTITY_CORE_CAP, "The bond line")
    if region in ("identity", "identityCore"):
        # soul.js mergeSoulInto keeps the identity and bond line with the newer identityAt, so a
        # tab still holding the old ones does not win them back.
        out["identityAt"] = int(time.time() * 1000)
        return
    if not isinstance(out.get(region), list if region == "log" else dict):
        # A creature that has not woken yet gets upstream's seed for these on its first load
        # (normalizeSoul fills a missing or broken one whole); a partial one would replace it.
        raise SoulError(
            "It hasn't written this yet — it does when it first wakes up. Nothing was saved."
        )
    if not isinstance(value, dict) and region != "log":
        raise SoulError("It has to be one { … } block. Nothing was saved.")
    if region == "scratch":
        scratch = obj(out.get("scratch"))
        had = scratch.get("rules")
        # A merge never trims, so a creature may hold more than the cap; that is not the owner's
        # to prune before they may change anything else.
        most = max(RULES_MAX, len(had) if isinstance(had, list) else 0)
        scratch["state"] = _text(value.get("state"), STATE_CAP, "What it's doing right now")
        scratch["rules"] = _lines(value.get("rules"), RULE_CAP, most, "rules")
        out["scratch"] = scratch
    elif region == "goals":
        goals = obj(out.get("goals"))
        name = value.get("selfName")
        if not isinstance(name, str):
            raise SoulError("Its name has to be text. Nothing was saved.")
        goals["selfName"] = _storable(_name_clean(name))
        goals["wants"] = _lines(value.get("wants"), WANT_CAP, WANTS_MAX, "wants")
        out["goals"] = goals
    else:
        out["log"] = _diary(out.get("log"), value)


def _when(t: Any) -> bool:
    # A diary time the phone and the PWA can both read: a Date takes at most 8.64e15 ms.
    return isinstance(t, int | float) and not isinstance(t, bool) and 0 <= t <= DATE_MAX_MS


def _diary(had: Any, value: Any) -> list[dict[str, Any]]:
    """The diary as the owner left it: each line `{t, txt}`, matched to the line it was by `t`
    so its other fields survive; an emptied line is dropped, and the order is by time. A stored
    line the editor could not show (no readable time or words) is kept as it was."""
    most = max(LOG_MAX, len(had) if isinstance(had, list) else 0)
    if not isinstance(value, list) or len(value) > most:
        raise SoulError(f"The diary has to be a list of at most {most} lines. Nothing was saved.")
    stored = [e for e in had if isinstance(e, dict)] if isinstance(had, list) else []
    old = {e["t"]: e for e in stored if _when(e.get("t"))}
    out = [e for e in stored if not (_when(e.get("t")) and isinstance(e.get("txt"), str))]
    for e in value:
        t = e.get("t") if isinstance(e, dict) else None
        if not _when(t):
            raise SoulError("A diary line has to be a time and its words. Nothing was saved.")
        line = _text(e.get("txt"), LOG_LINE_CAP, "A diary line")
        if line:
            out.append({**obj(old.get(t)), "t": t, "txt": line})
    out.sort(key=lambda e: e["t"] if _when(e.get("t")) else 0)
    return out


def renamed(soul: dict[str, Any], name: str) -> dict[str, Any]:
    """The list's name is also the soul file's `meta.name`; the creature's own name
    (`goals.selfName`) is the person's to change, inside the creature."""
    out = copy.deepcopy(soul)
    meta = obj(out.get("meta"))
    out["meta"] = sanitize_cfg({"meta": {**meta, "name": name}})["meta"]
    return out


def for_export(soul: dict[str, Any], name: str) -> dict[str, Any]:
    """The soul file Export downloads: upstream's snapshot shape, so the creature's MIND tab and
    its gallery read it. Visual memories are not in it; they stay on the box."""
    out = {k: copy.deepcopy(v) for k, v in soul.items() if k not in ("readOnly", "writeId")}
    meta = obj(out.get("meta"))
    out["meta"] = {**meta, "name": clean(meta.get("name") or name, 80), "kind": "snapshot"}
    out["soul_format"] = SOUL_FORMAT
    return out
