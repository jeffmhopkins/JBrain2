"""Dave's fast path (docs/plans/MINECRAFT_BEDROCK_PLAN.md §M18): which questions it takes,
which it leaves to the model, and the signed-off lines it answers with.

The searches, the waypoint, the scan count and "where am I" run through the REAL tool
handlers against a faked sidecar and an in-memory scan ledger, so the deep-scan limit
applies exactly as on a model turn. Saved places and other players need Postgres, so here
their handlers are stand-ins that answer the way the real ones do; the real ones are
driven end to end in `tests/integration/test_minecraft_places_pg.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from jbrain.agent import minecrafttools as mt
from jbrain.agent.agents import MINECRAFT_DAVE_INGAME_TOOLS
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.db.session import SessionContext
from jbrain.minecraft import client as mc
from jbrain.minecraft import fastpath as fp
from jbrain.minecraft import scans
from jbrain.minecraft.asker import Asker, Spot, asking
from tests.unit.test_minecraft_scans import MemoryBook, MemoryTargets

SID = "door-session"
CTX = ToolContext(session=SessionContext(principal_kind="owner"), scopes=(), agent_session_id=SID)
STEVE = Asker(key="111", gamertag="Steve 42", world="slot2", at=Spot(0, 64, 0, "overworld"))
DAY = "time query day"
LOCATE = "execute in overworld positioned 0 64 0 run locate structure village"


# --- matching -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("said", "want"),
    [
        ("point me home", fp.Ask("waypoint", name="home")),
        ("Navigate home!", fp.Ask("waypoint", name="home")),
        ("please point me home", fp.Ask("waypoint", name="home")),
        ("navigate to Tree Farm", fp.Ask("waypoint", name="Tree Farm")),
        ("point me to the barn?", fp.Ask("waypoint", name="the barn")),
        ("guide me to mine entrance please", fp.Ask("waypoint", name="mine entrance")),
        ("clear my waypoint", fp.Ask("clear")),
        ("clear the arrow.", fp.Ask("clear")),
        ("save this place as Tree Farm", fp.Ask("save", name="Tree Farm")),
        ("mark this location as base 2", fp.Ask("save", name="base 2")),
        ("forget tree farm", fp.Ask("forget", name="tree farm")),
        ("forget my place tree farm", fp.Ask("forget", name="tree farm")),
        ("delete saved place old base", fp.Ask("forget", name="old base")),
        ("list my places", fp.Ask("places")),
        ("what are my saved places?", fp.Ask("places")),
        ("where am I?", fp.Ask("where_am_i")),
        ("where am i right now", fp.Ask("where_am_i")),
        ("where is Mira_P", None),  # an underscore can't be in a gamertag
        ("where is Mira P", fp.Ask("where_is", name="Mira P")),
        ("where's Steve 42?", fp.Ask("where_is", name="Steve 42")),
        ("how many scans do I have left?", fp.Ask("scans")),
        ("how many deep scans left today", fp.Ask("scans")),
        (
            "where's the nearest village?",
            fp.Ask("nearest", kind="structure", what="village", label="village"),
        ),
        (
            "find the closest ancient city",
            fp.Ask("nearest", kind="structure", what="ancient_city", label="ancient city"),
        ),
        (
            "nearest diamonds",
            fp.Ask("nearest", kind="block", what="diamond_ore", label="diamonds"),
        ),
        (
            "where is the nearest spawner",
            fp.Ask("nearest", kind="block", what="mob_spawner", label="spawner"),
        ),
        ("nearest cow", fp.Ask("nearest", kind="mob", what="cow", label="cow")),
        ("find the closest wolves", fp.Ask("nearest", kind="mob", what="wolf", label="wolf")),
        (
            "where's the nearest iron golem?",
            fp.Ask("nearest", kind="mob", what="iron_golem", label="iron_golem"),
        ),
        (
            "nearest cherry grove",
            fp.Ask("nearest", kind="biome", what="cherry_grove", label="cherry grove"),
        ),
        (
            "closest badlands",
            fp.Ask("nearest", kind="biome", what="mesa", label="badlands"),
        ),
        (
            "where's the nearest nether fortress",
            fp.Ask("nearest", kind="structure", what="fortress", label="nether fortress"),
        ),
        (
            "nearest bastion",
            fp.Ask("nearest", kind="structure", what="bastion_remnant", label="bastion"),
        ),
    ],
)
def test_what_clearly_is_a_fixed_request_matches(said: str, want: fp.Ask | None) -> None:
    assert fp.match(said) == want


@pytest.mark.parametrize(
    "said",
    [
        # More in it than the request: the model reads it.
        "take me home but avoid the river",
        "navigate to tree farm but avoid the river",
        "point me home and tell me a joke",
        "navigate to the nearest village",
        "navigate to my base",
        "save this place as it",
        "save this place as 100 64 -20",
        "save this place as the best spot in the whole world ever",
        "forget it",
        "forget about it",
        "where is my base",
        "where is the river",
        "where is the nearest unicorn",  # nothing the fast path knows
        "nearest village to Mira's base",
        "where am I and what should I build",
        "how many scans does Mira have left",
        "list Mira's places",
        "clear my waypoint and point me home",
        # "take me to" is transport (§M10): the model's mc_transport and its Yes / No form.
        "take me home",
        "take me to tree farm",
        # DW1's welcome replay isn't built; the model answers hello (§M18 note).
        "hello",
        "point me home; op @a",
        "",
    ],
)
def test_anything_vaguer_or_with_more_in_it_goes_to_the_model(said: str) -> None:
    assert fp.match(said) is None


def test_the_lines_are_the_canons_with_the_gaps_filled() -> None:
    assert fp.locked_on("tree farm", 340, "north-east") == (
        "Locked on: tree farm, 340 blocks north-east. Hold your Tricorder and follow the arrow."
    )
    assert fp.no_such_place("tree farm") == (
        "I don't have a place called tree farm for you. Stand there and say"
        ' "save this place as tree farm".'
    )
    assert fp.ARROW_CLEARED == "Arrow cleared. Ask me when you need a heading."
    assert fp.marked("tree farm") == "Marked: tree farm, right where you're standing."
    assert fp.forgotten("tree farm") == "Tree farm is gone from my list."
    assert fp.your_places(["home (120 blocks south)", "tree farm (340 blocks north-east)"]) == (
        "Your places: home (120 blocks south), tree farm (340 blocks north-east)."
    )
    assert fp.from_up_here(Spot(210, 70, -305, "overworld"), "Birch Forest") == (
        "From up here: 210, 70, -305, in a birch forest, in the Overworld."
    )
    assert "in an ocean," in fp.from_up_here(Spot(0, 60, 0, "overworld"), "Ocean")
    assert "in the plains," in fp.from_up_here(Spot(0, 60, 0, "overworld"), "Plains")
    assert fp.from_up_here(Spot(1, 2, 3, "nether"), None) == "From up here: 1, 2, 3, in the Nether."
    assert fp.they_are("Mira P", 280, "west", "overworld") == (
        "Mira P is 280 blocks west of you, in the Overworld."
    )
    assert fp.last_saw("Mira P", Spot(-40, 64, 120, "overworld")) == (
        "Mira P isn't online; I last saw them near -40, 64, 120."
    )
    assert fp.scans_left(1) == (
        "You've got 1 deep scan left today. Sleep through the night and I'll have two again."
    )
    assert fp.scans_left(2).startswith("You've got 2 deep scans left today.")
    assert fp.scans_left(0).startswith("You've got no deep scans left today.")


def test_no_line_names_a_locked_item() -> None:
    """§M19: the fast path's words name no Old Maker item but the Tricorder."""
    import inspect

    source = inspect.getsource(fp).lower()
    for locked in ("power pack", "power_pack", "replicator", "wormhole", "no-spawn", "beacon"):
        assert locked not in source, locked


# --- carrying it out ------------------------------------------------------------------


@pytest.fixture
def sidecar(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {
        "sent": [],
        "replies": {
            DAY: ["[x INFO] Day is 7"],
            "/worlds": {"active": "slot2", "slots": [{"id": "slot2", "active": True}]},
            LOCATE: ["[x INFO] The nearest village is at block 300, ~, -400 (500 blocks away)"],
            "/map/point": {
                "source": "explored",
                "biome": "Birch Forest",
                "biome_id": "minecraft:birch_forest",
                "y": 70,
            },
            "/map/find": {
                "searched": ["minecraft:diamond_ore"],
                "radius": 2048,
                "found": [
                    {"block": "minecraft:diamond_ore", "x": 12, "y": -58, "z": 18, "distance": 22}
                ],
            },
        },
        "down": set(),
    }

    async def call(_cfg: Any, method: str, path: str, **kw: Any) -> dict[str, Any]:
        what = kw["json"]["command"] if path == "/command" else path
        if what in state["down"]:
            raise HTTPException(status_code=503, detail="the server is stopped")
        state["sent"].append(what)
        if path == "/command":
            if what.startswith("scriptevent jb:waypoint"):
                return {"lines": ["[x] [Scripting] [jbrain] waypoint set"]}
            return {"lines": state["replies"].get(what, ["[x INFO] ok"])}
        return state["replies"].get(path, {})

    monkeypatch.setattr(mc, "call", call)
    return state


class Stand:
    """Stand-ins for the handlers that need Postgres, answering as the real ones do."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.places = [
            ("home", Spot(0, 70, 120, "overworld")),
            ("tree farm", Spot(240, 64, -240, "overworld")),
        ]
        self.player: Any = mt.Found(
            "Mira P is online now",
            "online",
            gamertag="Mira P",
            online=True,
            spot=Spot(-280, 64, 0, "overworld"),
        )

    def handlers(self) -> dict[str, Any]:
        async def mc_places(args: dict, _ctx: Any) -> str:
            self.calls.append(("mc_places", args))
            if not self.places:
                return ToolOutput("none", result_brief="none")
            return mt.Found("places", f"{len(self.places)} place(s)", places=self.places)

        async def mc_place_save(args: dict, _ctx: Any) -> str:
            self.calls.append(("mc_place_save", args))
            return ToolOutput("saved", result_brief=args["name"])

        async def mc_place_remove(args: dict, _ctx: Any) -> str:
            self.calls.append(("mc_place_remove", args))
            if args["name"].lower() != "tree farm":
                return ToolOutput("no such", result_brief="not found")
            return ToolOutput("removed", result_brief="removed tree farm")

        async def mc_players(args: dict, _ctx: Any) -> str:
            self.calls.append(("mc_players", args))
            return self.player

        return {
            "mc_places": mc_places,
            "mc_place_save": mc_place_save,
            "mc_place_remove": mc_place_remove,
            "mc_players": mc_players,
        }


@pytest.fixture
def stand() -> Stand:
    return Stand()


@pytest.fixture
def book() -> MemoryBook:
    return MemoryBook()


def _fast(book: MemoryBook, stand: Stand) -> fp.FastPath:
    real = mt.build_minecraft_handlers(
        object(),  # type: ignore[arg-type]
        object(),
        None,
        scan_book=lambda _ctx: book,
        target_book=lambda _ctx: MemoryTargets(),
    )
    return fp.FastPath({**real, **stand.handlers()})


async def _answer(fast: fp.FastPath, said: str, who: Asker = STEVE) -> fp.Answer | None:
    ask = fp.match(said)
    assert ask is not None, said
    with asking(SID, who):
        return await fast.answer(ask, who, CTX)


def _waypoints(sidecar: dict[str, Any]) -> list[str]:
    return [s for s in sidecar["sent"] if s.startswith("scriptevent jb:waypoint")]


async def test_where_am_i_reads_the_biome_where_they_stand(book, stand, sidecar) -> None:
    got = await _answer(_fast(book, stand), "where am I?", STEVE)
    assert got is not None
    assert got.reply == "From up here: 0, 64, 0, in a birch forest, in the Overworld."
    (step,) = got.steps
    assert step["name"] == "mc_what_is_at" and step["ok"] and "Birch Forest" in step["summary"]
    # The map can't say: the coordinates alone, still the canon's line.
    sidecar["down"].add("/worlds")
    got = await _answer(_fast(book, stand), "where am I", STEVE)
    assert got is not None and got.reply == "From up here: 0, 64, 0, in the Overworld."


async def test_nearest_is_a_deep_scan_then_the_arrow(book, stand, sidecar) -> None:
    fast = _fast(book, stand)
    got = await _answer(fast, "where's the nearest village")
    assert got is not None
    assert got.reply == (
        "From up here I see one 500 blocks north-east, at 300, -400. Hold your Tricorder"
        " and follow the arrow, crew. You've got 1 deep scan left today."
    )
    assert [s["name"] for s in got.steps] == ["mc_locate", "mc_set_waypoint"]
    assert '"x": 300' in _waypoints(sidecar)[-1] and '"label": "Village"' in _waypoints(sidecar)[-1]
    assert book.used(STEVE, 7) == 1
    # The same ask again is the free repeat, from the stored answer.
    again = await _answer(fast, "where's the nearest village")
    assert again is not None and again.reply.endswith("You've got 1 deep scan left today.")
    assert book.used(STEVE, 7) == 1
    diamonds = await _answer(fast, "nearest diamonds")
    assert diamonds is not None
    assert "at 12, -58, 18" in diamonds.reply and diamonds.reply.endswith(
        "no deep scans left today."
    )
    assert '"y": -58' in _waypoints(sidecar)[-1]
    # Out of scans: the canon's recharge line, nothing searched, no arrow.
    sent = len(sidecar["sent"])
    out = await _answer(fast, "nearest mansion")
    assert out is not None and out.reply == scans.RECHARGE
    assert not any("locate" in s for s in sidecar["sent"][sent:])
    assert not _waypoints({"sent": sidecar["sent"][sent:]})


async def test_a_search_that_finds_nothing_or_cant_run_goes_to_the_model(
    book, stand, sidecar
) -> None:
    fast = _fast(book, stand)
    sidecar["replies"]["/map/find"] = {"searched": [], "suggestions": []}
    assert await _answer(fast, "nearest diamonds") is None
    assert book.used(STEVE, 7) == 0  # refunded: nothing was searched
    sidecar["down"].add(DAY)
    assert await _answer(fast, "where's the nearest village") is None
    # An Overworld structure asked for from the Nether: the model explains.
    nether = Asker(key="111", gamertag="Steve 42", world="slot2", at=Spot(0, 64, 0, "nether"))
    assert await _answer(fast, "where's the nearest village", nether) is None


async def test_scans_left_and_the_waypoint_tools(book, stand, sidecar) -> None:
    fast = _fast(book, stand)
    got = await _answer(fast, "how many scans do I have left?")
    assert got is not None and got.reply == fp.scans_left(2)
    cleared = await _answer(fast, "clear my waypoint")
    assert cleared is not None and cleared.reply == fp.ARROW_CLEARED
    assert '"clear": true' in _waypoints(sidecar)[-1]
    # Nothing the fast path can say: the server is down, so the model says so.
    sidecar["down"].add('scriptevent jb:waypoint {"player": "Steve 42", "clear": true}')
    assert await _answer(fast, "clear my waypoint") is None
    sidecar["down"].add(DAY)
    assert await _answer(fast, "how many scans do I have left") is None


async def test_places_through_the_place_tools(book, stand, sidecar) -> None:
    fast = _fast(book, stand)
    saved = await _answer(fast, "save this place as Tree Farm")
    assert saved is not None and saved.reply == fp.marked("Tree Farm")
    assert stand.calls[-1] == ("mc_place_save", {"name": "Tree Farm"})
    gone = await _answer(fast, "forget Tree Farm")
    assert gone is not None and gone.reply == "Tree farm is gone from my list."
    assert await _answer(fast, "forget the moon base") is None  # nothing changed: the model's
    listed = await _answer(fast, "list my places")
    assert listed is not None
    assert (
        listed.reply == "Your places: home (120 blocks south), tree farm (339 blocks north-east)."
    )
    stand.places = [("portal", Spot(10, 70, 10, "nether"))]
    listed = await _answer(fast, "list my places")
    assert listed is not None and listed.reply == "Your places: portal (in the Nether)."
    stand.places = []
    assert await _answer(fast, "list my places") is None


async def test_navigate_uses_the_saved_place_or_says_there_is_none(book, stand, sidecar) -> None:
    async def fake_waypoint(args: dict, ctx: Any) -> str:
        if args["place"] == "nowhere":
            return ToolOutput("no such", result_brief="no such place")
        if args["place"] == "portal":
            return mt.Found("set", "portal", target=Spot(10, 70, 10, "nether"))
        return mt.Found("set", "Home (your bed)", target=Spot(0, 70, 120, "overworld"))

    async def fake_players(args: dict, ctx: Any) -> str:
        return ToolOutput("Nobody called that", result_brief="unknown")

    fast = fp.FastPath({"mc_set_waypoint": fake_waypoint, "mc_players": fake_players})
    got = await _answer(fast, "point me home")
    assert got is not None and got.reply == fp.locked_on("home", 120, "south")
    nowhere = await _answer(fast, "navigate to nowhere")
    assert nowhere is not None and nowhere.reply == fp.no_such_place("nowhere")
    # Another dimension has no line with a distance: the model explains.
    assert await _answer(fast, "navigate to portal") is None


async def test_point_me_to_a_player_when_no_place_has_their_name(book, stand, sidecar) -> None:
    pointed: list[dict] = []

    async def fake_waypoint(args: dict, ctx: Any) -> str:
        if "place" in args:
            return ToolOutput("no such", result_brief="no such place")
        pointed.append(args)
        return mt.Found("set", args["label"], target=Spot(args["x"], None, args["z"], "overworld"))

    seen: dict[str, Any] = {"online": True, "spot": Spot(0, 64, -300, "overworld")}

    async def fake_players(args: dict, ctx: Any) -> str:
        return mt.Found("x", "online", gamertag="Mira P", **seen)

    fast = fp.FastPath({"mc_set_waypoint": fake_waypoint, "mc_players": fake_players})
    got = await _answer(fast, "point me to mira p")
    assert got is not None and got.reply == fp.locked_on("Mira P", 300, "north")
    assert pointed == [{"x": 0, "z": -300, "dimension": "overworld", "label": "Mira P"}]
    # Offline, or across a portal: nowhere to point now, so the model says where they were.
    seen["online"] = False
    assert await _answer(fast, "point me to mira p") is None
    seen.update(online=True, spot=Spot(0, 64, -300, "nether"))
    assert await _answer(fast, "point me to mira p") is None
    assert len(pointed) == 1


async def test_where_is_another_player(book, stand, sidecar) -> None:
    fast = _fast(book, stand)
    got = await _answer(fast, "where is Mira P")
    assert got is not None and got.reply == fp.they_are("Mira P", 280, "west", "overworld")
    assert stand.calls[-1] == ("mc_players", {"gamertag": "Mira P"})
    stand.player = mt.Found(
        "x", "last seen", gamertag="Mira P", online=False, spot=Spot(-40, 64, 120, "overworld")
    )
    got = await _answer(fast, "where's mira p")
    assert got is not None and got.reply == fp.last_saw("Mira P", Spot(-40, 64, 120, "overworld"))
    stand.player = ToolOutput("Nobody called that", result_brief="unknown")
    assert await _answer(fast, "where is Nobody Here") is None
    stand.player = mt.Found(
        "x", "online", gamertag="Mira P", online=True, spot=Spot(5, 64, 5, "nether")
    )
    assert await _answer(fast, "where is Mira P") is None  # another dimension: the model
    # Asking where you yourself are is "where am I".
    me = await _answer(fast, "where is steve 42")
    assert me is not None and me.reply.startswith("From up here: 0, 64, 0")


async def test_only_the_tools_a_player_turn_holds(book, stand, sidecar) -> None:
    """A handler outside the in-game allowlist is never called, even if it is wired."""
    called: list[str] = []

    async def mc_command(args: dict, ctx: Any) -> str:
        called.append("mc_command")
        return "ran"

    assert "mc_command" not in MINECRAFT_DAVE_INGAME_TOOLS
    fast = fp.FastPath({"mc_command": mc_command})
    assert await _answer(fast, "point me home") is None  # mc_set_waypoint isn't wired
    assert called == []
    for tool in ("mc_set_waypoint", "mc_place_save", "mc_place_remove", "mc_places"):
        assert tool in MINECRAFT_DAVE_INGAME_TOOLS
    for tool in ("mc_what_is_at", "mc_players", "mc_world_info", "mc_locate", "mc_find_block"):
        assert tool in MINECRAFT_DAVE_INGAME_TOOLS


def test_first_hit_reads_both_search_shapes() -> None:
    assert mt.first_hit("- diamond ore at x=12 y=-58 z=18, 22 blocks away", "overworld") == Spot(
        12, -58, 18, "overworld"
    )
    assert mt.first_hit("The nearest village is at block 300, ~, -400", "overworld") == Spot(
        300, None, -400, "overworld"
    )
    assert mt.first_hit("nearest minecraft:fortress is at block 30, (y?), 40", "nether") == Spot(
        30, None, 40, "nether"
    )
    assert mt.first_hit("is at block 1, 70, 2", "overworld") == Spot(1, 70, 2, "overworld")
    assert mt.first_hit("none within 2048 blocks", "overworld") is None


NETHER = Asker(key="111", gamertag="Steve 42", world="slot2", at=Spot(0, 64, 0, "nether"))


async def test_nearest_nether_structure_and_biome_search_where_they_stand(
    book, stand, sidecar
) -> None:
    fortress = "execute in nether positioned 0 64 0 run locate structure fortress"
    sidecar["replies"][fortress] = [
        "[x INFO] The nearest fortress is at block -96, ~, 160 (186 blocks away)"
    ]
    fast = _fast(book, stand)
    got = await _answer(fast, "nearest fortress", NETHER)
    assert got is not None and got.reply.startswith("From up here I see one 187 blocks south-west")
    assert fortress in sidecar["sent"]
    assert '"dim": "nether"' in _waypoints(sidecar)[-1]
    assert '"label": "Fortress"' in _waypoints(sidecar)[-1]
    # A Nether structure from the Overworld (or an End city from the Nether): the model's.
    assert await _answer(fast, "nearest fortress") is None
    assert await _answer(fast, "nearest end city", NETHER) is None

    grove = "execute in overworld positioned 0 64 0 run locate biome minecraft:cherry_grove"
    sidecar["replies"][grove] = [
        "[x INFO] The nearest minecraft:cherry_grove is at block 0, ~, -640 (640 blocks away)"
    ]
    got = await _answer(_fast(MemoryBook(), stand), "nearest cherry grove")
    assert got is not None and got.reply.startswith("From up here I see one 640 blocks north")
    assert '"label": "Cherry grove"' in _waypoints(sidecar)[-1]


async def test_nearest_mob_asks_the_game_and_costs_no_scan(book, stand, sidecar) -> None:
    find = 'scriptevent jb:find {"player": "Steve 42", "type": "iron_golem", "max": 256}'
    sidecar["replies"][find] = [
        "[x] [Scripting] [jbrain] find found minecraft:iron_golem at x=30 y=70 z=-40,"
        " 50 blocks from Steve 42 (overworld)"
    ]
    fast = _fast(book, stand)
    got = await _answer(fast, "where's the nearest iron golem?")
    assert got is not None
    assert got.reply == (
        "From up here I see one 50 blocks north-east, at 30, 70, -40. Hold your Tricorder"
        " and follow the arrow, crew."
    )
    assert [s["name"] for s in got.steps] == ["mc_find_mob", "mc_set_waypoint"]
    assert '"label": "Iron golem"' in _waypoints(sidecar)[-1]
    assert book.used(STEVE, 7) == 0
    # None about: nothing to point at, so the model says so in its own words.
    sidecar["replies"][find] = [
        "[x] [Scripting] [jbrain] find none: no minecraft:iron_golem within 256 blocks"
    ]
    assert await _answer(fast, "nearest iron golem") is None
