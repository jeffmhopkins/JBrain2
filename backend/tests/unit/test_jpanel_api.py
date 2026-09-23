"""jpanel's addressing, and the coupling that decides whether a panel is reachable at all.

The routes themselves are exercised against real Postgres in
`tests/integration/test_jpanel_message_rls.py`, where the policies live. What is worth testing
without a database is the one piece of pure logic the whole feature hangs off: turning a
principal's LABEL into the name a four-year-old hears, because panels are ordinary `device_key`
principals and that label is the only thing marking one.
"""

import re
from pathlib import Path

from fastapi.routing import APIRoute

from jbrain.api import jpanel


class TestDisplayName:
    def test_a_named_panel_keeps_its_name(self) -> None:
        assert jpanel._display_name("panel Ellie") == "Ellie"
        assert jpanel._display_name("panel  Rae ") == "Rae"

    def test_an_unnamed_panel_is_still_sayable(self) -> None:
        """A four-year-old told "a message from the other one" at least knows a message
        arrived. An empty name would draw a pop-up from nobody."""
        assert jpanel._display_name("room endpoint panel") == "the other one"
        assert jpanel._display_name("panel") == "the other one"

    def test_the_label_the_flash_route_actually_writes_is_one_this_understands(self) -> None:
        """THE COUPLING, PINNED. A panel is reachable only if its label matches what this
        module looks for, and the label is written somewhere else entirely — `/flash`, in
        `endpoint.py`. Nothing connects the two but this test.

        The first cut matched `label LIKE 'panel%'` and therefore silently lost every unit
        flashed WITHOUT a name, because that branch writes "room endpoint panel". The panel
        would have enrolled, polled, and simply never been addressable, with nothing anywhere
        saying why. So both branches of that expression are read out of the source and checked,
        rather than remembered."""
        src = (
            Path(__file__).resolve().parents[2] / "src" / "jbrain" / "api" / "endpoint.py"
        ).read_text()
        match = re.search(r'label = (f"[^"]+"[^\n]*?if name else "([^"]+)")', src)
        assert match is not None, "the /flash label expression moved; re-pin this test"
        named, unnamed = match.group(1), match.group(2)

        # The named branch: whatever prefix it uses must be one `_display_name` strips.
        assert "panel" in named, f"/flash no longer labels a named panel with 'panel': {named}"
        assert jpanel._display_name("panel Ellie") == "Ellie"

        # The unnamed branch, verbatim from the source rather than typed again here.
        assert unnamed == jpanel._UNNAMED_LABEL, (
            f"/flash labels an unnamed panel {unnamed!r} and jpanel looks for "
            f"{jpanel._UNNAMED_LABEL!r} — such a panel would never be addressable"
        )
        assert jpanel._display_name(unnamed) == "the other one"


class TestNameOf:
    def test_the_owner_is_dad(self) -> None:
        """Not "the owner", not a principal id: the word a child hears."""
        assert jpanel._name_of({}, "owner", None) == "Dad"

    def test_an_unknown_panel_still_gets_a_word(self) -> None:
        """A revoked or re-flashed panel leaves rows behind that still have to render. A name
        that resolves to nothing would put an empty string in a pop-up."""
        assert jpanel._name_of({}, "panel", "gone") == "the other one"

    def test_a_known_panel_is_named(self) -> None:
        assert jpanel._name_of({"p1": "Ellie"}, "panel", "p1") == "Ellie"


class TestMessageShape:
    def _row(self, sender_kind: str, sender_device: str | None, deliveries: int = 0):
        from datetime import UTC, datetime

        return (
            "11111111-1111-1111-1111-111111111111",
            sender_kind,
            sender_device,
            "owner" if sender_kind == "panel" else "panel",
            None if sender_kind == "panel" else "p1",
            "hello",
            "voice",
            1500,
            datetime(2026, 9, 22, 12, 0, tzinfo=UTC),
            None,
            deliveries,
        )

    def test_direction_is_relative_to_the_owner(self) -> None:
        """ "in" and "out" are the PWA's axis, and the owner is the other end of every
        conversation he can see — so the direction is decided by whether a panel sent it."""
        names = {"p1": "Ellie"}
        assert jpanel._row_to_message(self._row("panel", "p1"), names).direction == "in"
        assert jpanel._row_to_message(self._row("owner", None), names).direction == "out"

    def test_an_unplayed_message_reports_no_played_at(self) -> None:
        """`played_at is None` is the entire inbox query and the PWA's unplayed badge; a
        serialisation that turned it into a string would make every message look heard."""
        msg = jpanel._row_to_message(self._row("panel", "p1"), {"p1": "Ellie"})
        assert msg.played_at is None
        assert msg.from_name == "Ellie"
        assert msg.to_name == "Dad"

    def test_a_message_the_box_gave_up_on_is_not_merely_waiting(self) -> None:
        """THE TWO LOOK IDENTICAL IN `played_at`, AND ONLY ONE MEANS SOMETHING IS WRONG.

        A message nobody has come to yet and a message the box has stopped trying to deliver are
        both `played_at IS NULL`. Without a way to tell them apart, a panel that cannot
        acknowledge — the §10.4cw failure — presents to the owner as a child who simply has not
        pressed the pop-up, which is the wrong thing to believe about your own children.

        `played_at` is deliberately NOT written when the box gives up: the row is unplayed
        because it IS unplayed, and stamping it would be the box telling the owner a lie."""
        waiting = jpanel._row_to_message(self._row("owner", None, deliveries=1), {"p1": "Ellie"})
        assert waiting.undelivered is False, "one try is a message waiting, not a failure"

        gave_up = jpanel._row_to_message(
            self._row("owner", None, deliveries=jpanel.JPANEL_MAX_DELIVERIES),
            {"p1": "Ellie"},
        )
        assert gave_up.undelivered is True
        assert gave_up.played_at is None, (
            "the box must never stamp played_at on a message nobody heard — that is a lie "
            "about his children, told to make a number tidy"
        )


class TestThePanelFacingRoutesAreWhereTheFirmwareLooks:
    """THE FIRMWARE HARDCODES THESE FOUR PATHS AND NOTHING ELSE COULD CATCH A MOVE.

    `firmware/main/jpanel.c` builds `<api>/jpanel/<path>` with `snprintf`. Nothing on the panel
    can be tested on a host (the file needs the ESP HTTP client), and the box cannot tell a
    panel it is knocking on the wrong door — a 404 from `GET /waiting` is indistinguishable
    from "nobody sent me anything", so the whole feature reads as merely quiet.

    That is not hypothetical. W3 was written against `JPANEL_PLAN.md` §3b, which put these
    under the device surface at `/api/endpoint/jpanel/*` beside `/endpoint/converse`; W2 had
    mounted them at `/api/jpanel/*` instead, and the mismatch survived a clean build, a green
    host suite and a byte-compared image. It was found by asking the live box.

    So the paths are pinned from this end, where a test can actually run, and the firmware
    comment names this test by name.
    """

    def test_the_four_panel_routes_keep_their_paths(self) -> None:
        # `routes` is typed as `BaseRoute`, which has neither `path` nor `methods` — only the
        # `APIRoute` subclass does. Narrowed rather than ignored, so a route type that really
        # has no path (a mount, a websocket) is skipped instead of crashing the pin.
        paths = {
            (route.path, method)
            for route in jpanel.router.routes
            if isinstance(route, APIRoute)
            for method in route.methods
            if method != "HEAD"
        }
        assert ("/jpanel/send", "POST") in paths
        assert ("/jpanel/waiting", "GET") in paths
        assert ("/jpanel/next", "GET") in paths
        assert ("/jpanel/played", "POST") in paths

    def test_the_firmware_builds_exactly_that_base(self) -> None:
        """Read out of the firmware, so the two cannot drift without one of them failing."""
        # parents[3] is the repo root — one further out than the backend-local reads above.
        src = (Path(__file__).resolve().parents[3] / "firmware" / "main" / "jpanel.c").read_text(
            encoding="utf-8"
        )
        assert '"%s/jpanel%s", s_cfg->api, path' in src, (
            "firmware/main/jpanel.c no longer builds <api>/jpanel/<path>; "
            "the routes above moved with it or the panel is about to 404"
        )
        # And the four suffixes it passes to that helper.
        for suffix in ('"/send?to=%s"', '"/waiting"', '"/next"', '"/played"'):
            assert suffix in src, f"the firmware stopped asking for {suffix}"


class TestTheOwnerFacingRoutesAreWhereThePwaLooks:
    """THE SAME PIN, FOR THE OTHER CLIENT, AND FOR THE SAME REASON.

    The firmware spent a whole release calling `/api/endpoint/jpanel/*` — a path that does not
    exist — because it was written from a plan the backend had not followed. The PWA is the
    other client of these routes and hardcodes its URLs in exactly the same way, in a package
    with its own test runner that never imports this one.

    So the owner-facing paths are pinned from here too, against the TypeScript that calls
    them. The voice route is the one that matters most: it is newest, it takes a query
    parameter rather than a body field, and a 404 on it looks to a parent like a recording
    that simply did not send."""

    def test_the_owner_routes_keep_their_paths(self) -> None:
        paths = {
            (route.path, method)
            for route in jpanel.router.routes
            if isinstance(route, APIRoute)
            for method in route.methods
            if method != "HEAD"
        }
        assert ("/jpanel/messages", "GET") in paths
        assert ("/jpanel/messages", "POST") in paths
        assert ("/jpanel/messages/audio", "POST") in paths
        assert ("/jpanel/messages/{message_id}/played", "POST") in paths
        assert ("/jpanel/messages/{message_id}/audio", "GET") in paths

    def test_the_pwa_calls_exactly_those(self) -> None:
        src = (
            Path(__file__).resolve().parents[3] / "frontend" / "src" / "api" / "client.ts"
        ).read_text(encoding="utf-8")
        assert '"/api/jpanel/messages"' in src, "the PWA stopped calling the list/send route"
        assert "/api/jpanel/messages/audio?to_device=" in src, (
            "the PWA no longer POSTs Dad's recording to /api/jpanel/messages/audio with a "
            "to_device query parameter — a parent would see a send that silently 404s"
        )
        assert "/api/jpanel/messages/${encodeURIComponent(id)}/played" in src
        assert "/api/jpanel/messages/${encodeURIComponent(id)}/audio" in src

    def test_the_pwa_sends_the_audio_format_the_panels_speak(self) -> None:
        """One audio format crosses this boundary, and the browser is what converts to it.

        `voiceMessage.ts` resamples to 16 kHz mono s16 before upload, because decoding a
        `MediaRecorder` blob (webm/opus in Chrome, mp4/aac in Safari) would mean a codec
        dependency in the api container for a job the recording browser can already do. If
        that constant drifts from `PANEL_RATE`, the panel plays Dad at the wrong speed."""
        src = (
            Path(__file__).resolve().parents[3] / "frontend" / "src" / "voiceMessage.ts"
        ).read_text(encoding="utf-8")
        assert f"export const PANEL_RATE = {jpanel.PANEL_RATE};" in src, (
            f"the PWA resamples to something other than {jpanel.PANEL_RATE} Hz — Dad would "
            "arrive on the panel at the wrong pitch and speed"
        )
        assert f"export const MAX_MESSAGE_MS = {jpanel.MAX_MESSAGE_MS:_};" in src, (
            "the PWA's recording cap no longer matches MAX_MESSAGE_MS, so a long message "
            "would be truncated on arrival with nothing said about it"
        )


class TestDadsVoice:
    """THE PREFIX THAT WAS MISSING, AND WHY NOTHING CAUGHT IT.

    `DAD_VOICE` was "am_michael" and every message the owner sent arrived on the panel in the
    PET'S voice — the one thing a separate voice exists to prevent, since a message from Dad in
    the robot's voice teaches a four-year-old that the robot and their father are the same
    thing.

    `_resolve_kokoro_voice` returns the default for any id that does not start with `kokoro-`,
    and the default is `CURATED_KOKORO_VOICES[0]`. That fallback is right on its side — a stale
    id from an old client should render rather than error — and it is precisely why this was
    invisible: the box logged a successful render, the panel played perfectly good speech, and
    nothing said the voice had been swapped. The owner heard it.

    So the rule and the roster are read out of the service rather than restated here. The two
    live in different packages with different test runners and nothing else connects them."""

    def _tts_source(self) -> str:
        return (
            Path(__file__).resolve().parents[3] / "deploy" / "tts-stt" / "tts_server.py"
        ).read_text(encoding="utf-8")

    def test_dads_voice_is_one_the_engine_will_actually_use(self) -> None:
        src = self._tts_source()
        prefix_match = re.search(r'KOKORO_ID_PREFIX = "([^"]+)"', src)
        assert prefix_match is not None, "the voice id prefix moved; re-pin this test"
        prefix = prefix_match.group(1)
        assert jpanel.DAD_VOICE.startswith(prefix), (
            f"{jpanel.DAD_VOICE!r} does not start with {prefix!r}, so _resolve_kokoro_voice "
            "silently renders it in the DEFAULT voice — which is the pet's"
        )

        roster = re.search(r"CURATED_KOKORO_VOICES: tuple\[str, \.\.\.\] = \((.*?)\n\)", src, re.S)
        assert roster is not None, "the curated voice roster moved; re-pin this test"
        names = re.findall(r'"([a-z]{2}_[a-z]+)"', roster.group(1))
        assert names, "no voice names parsed out of the roster"
        assert jpanel.DAD_VOICE[len(prefix) :] in names, (
            f"{jpanel.DAD_VOICE!r} is not in the engine's curated roster, so it falls back "
            "to the default voice"
        )

    def test_dads_voice_is_not_the_one_the_pet_uses(self) -> None:
        """The whole point. `CURATED_KOKORO_VOICES[0]` is both the pet's voice and the fallback,
        so landing on it means either a deliberate mistake or the bug above returning."""
        src = self._tts_source()
        roster = re.search(r"CURATED_KOKORO_VOICES: tuple\[str, \.\.\.\] = \((.*?)\n\)", src, re.S)
        assert roster is not None
        default = re.findall(r'"([a-z]{2}_[a-z]+)"', roster.group(1))[0]
        assert not jpanel.DAD_VOICE.endswith(default), (
            f"Dad would speak in {default!r}, which is the pet's own voice"
        )

    def test_dads_voice_is_male(self) -> None:
        """`am_`/`bm_` are Kokoro's male American and British prefixes. Not a style preference:
        the cheapest possible signal to a four-year-old that this is a person and not the toy."""
        name = jpanel.DAD_VOICE.split("-", 1)[-1]
        assert name.startswith(("am_", "bm_")), f"{name!r} is not one of Kokoro's male voices"


class TestTheHeardRingCrossesThePackageBoundary:
    """THE FIRMWARE FILLS IT AND THE BOX VALIDATES IT, AND NOTHING ELSE CONNECTS THEM.

    `speech.c` builds the ring and `main.c` serialises it into the telemetry body; `TelemetryIn`
    decides whether that body is accepted at all. A shape mismatch is not a soft failure: the
    body 422s, and a 422 telemetry is a FAILED report — the panel keeps its crash ring and the
    reading never arrives. From the box it looks exactly like a panel that had nothing to say.

    This is the same class of coupling as the `/api/jpanel` route paths, which shipped broken
    for a release because two packages disagreed and no test read both.
    """

    def _speech_source(self) -> str:
        return (Path(__file__).resolve().parents[3] / "firmware" / "main" / "speech.c").read_text(
            encoding="utf-8"
        )

    def test_the_box_accepts_both_the_old_and_the_new_entry_shape(self) -> None:
        """A fleet upgrades one panel at a time, so both arities are live at once."""
        from jbrain.api.endpoint import TelemetryIn

        old = TelemetryIn(version="0.2.90", uptime_ms=1, heard=[("burp", 21, 1)])
        assert len(old.heard) == 1

        new = TelemetryIn(version="0.2.91", uptime_ms=1, heard=[("burp", 21, 1, 4)])
        assert len(new.heard) == 1
        # Through `list()` because the field is a union of both arities, and indexing position
        # 3 is only valid on one of them — which is the point of the union.
        assert list(new.heard[0])[3] == 4, "the repeat count must survive validation"

        both = TelemetryIn(
            version="0.2.91", uptime_ms=1, heard=[("burp", 21, 1), ("dance", 9, 0, 3)]
        )
        assert len(both.heard) == 2

    def test_the_ring_is_deep_enough_to_outlive_a_poll(self) -> None:
        """THE DEPTH IS THE WHOLE POINT OF THE CHANGE. Three entries were sized for a bench,
        where the question is asked seconds later; the owner asks his from another room off a
        poll that runs every fifteen minutes. `dance` and `burp` went undiagnosed because every
        attempt to look found an empty ring (docs/reference/PANEL_COMMANDS.md).

        Read out of the firmware so shrinking it back fails here rather than quietly costing
        another evening of data."""
        src = self._speech_source()
        match = re.search(r"#define DECODE_MAX (\d+)", src)
        assert match is not None, "DECODE_MAX moved; re-pin this test"
        assert int(match.group(1)) >= 8, (
            f"the ring holds {match.group(1)} decodes — too few to survive a fifteen-minute "
            "poll with children shouting at the panel, which is the case it exists for"
        )

    def test_the_firmware_sends_the_repeat_count(self) -> None:
        """Consecutive identical decodes collapse into one entry with a count, so a television
        repeating one word cannot flush the ring. The count only helps if it is on the wire."""
        src = (Path(__file__).resolve().parents[3] / "firmware" / "main" / "main.c").read_text(
            encoding="utf-8"
        )
        assert '[\\"%s\\",%d,%d,%d]' in src, (
            "the telemetry body no longer carries four fields per decode; the box accepts them "
            "and nothing is sending them"
        )
