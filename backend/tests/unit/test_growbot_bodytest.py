"""growbot-bodytest.js: the guided body test (owner, 2026-11-15: "place this phone on body, power
on body, stand 1 m away and say '1 meter', and then it starts the test ... and log everything so
you can review after"). The whole script runs here against a fake robot, person and clock
(growbot_bodytest_harness.mjs): setup by voice, the eight checks and their verdicts, a fall, the
two ways to stop it, and the log it posts."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[3] / "growbot"
_PATCH = _ROOT / "patches" / "growbot-bodytest.js"
_SITE = _ROOT / "site" / "growbot-bodytest.js"
_HARNESS = Path(__file__).with_name("growbot_bodytest_harness.mjs")
_CHECKS = ["leftright", "tilt", "hold", "turn", "creep", "forward", "back", "estop"]


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    return node


def _run(**scenario: Any) -> dict[str, Any]:
    out = subprocess.run(
        [_node(), str(_HARNESS), _PATCH.as_uri(), json.dumps(scenario)],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def _eval(body: str) -> Any:
    script = (
        f"const m = await import({json.dumps(_PATCH.as_uri())});"
        f"console.log(JSON.stringify(await (async () => {{ {body} }})()));"
    )
    out = subprocess.run(
        [_node(), "--input-type=module", "-e", script], capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


def test_the_site_serves_the_patch_file() -> None:
    assert _SITE.read_bytes() == _PATCH.read_bytes()


def test_the_words_it_listens_for() -> None:
    lines = [
        "One meter.",
        "1 m",
        "a metre",
        "Stand up!",
        "I'm ready",
        "cancel the test",
        "stop stop",
        "what's for dinner",
    ]
    got = _eval(f"return {json.dumps(lines)}.map(m.heardWord);")
    assert got == ["metre", "metre", "metre", "stand", "ready", "cancel", "stop", ""]


def test_a_full_run_sets_up_by_voice_and_passes_every_check() -> None:
    got = _run(stills=True)
    assert got["status"] == "done"
    assert list(got["results"]) == [*_CHECKS, "falls"]
    assert all(r["v"] == "ok" for r in got["results"].values()), got["results"]
    assert got["steps"] == ["place", "power", "stand", "measure", *_CHECKS]
    # The setup's words, each heard and taken.
    assert [h["word"] for h in got["heard"]] == ["ready", "stand", "metre"]
    assert got["out"]["standUps"] == 1 and got["out"]["calibrations"] == 1
    # One walk per check that moves, each ended before the next.
    assert got["out"]["walks"] == 7
    turn = got["results"]["turn"]
    assert turn["left"]["reached"] == pytest.approx(90, abs=2)
    assert turn["right"]["reached"] == pytest.approx(-90, abs=2)
    assert got["results"]["forward"]["moved"] > 0 > got["results"]["back"]["moved"]
    # Once over, a heard line goes back to the creature.
    assert got["out"]["heardAfter"] is False


def test_the_log_reaches_the_box_in_order_at_twenty_samples_a_second() -> None:
    got = _run(stills=True)
    assert got["seqs"] == list(range(len(got["seqs"])))
    assert set(got["statuses"][:-1]) == {"running"} and got["statuses"][-1] == "done"
    # The skill set rides the first chunk only.
    assert got["skills"][0] == {"look": "pol-look", "forward": "pol-fwd"}
    assert not any(got["skills"][1:])
    assert 18 <= got["sampleHz"] <= 21
    assert set(got["kinds"]) == {"s", "e", "ev", "still"}
    assert got["stills"] == got["steps"]
    # The last chunk's summary holds the verdicts, which the Body tab shows.
    assert got["last"]["phase"] == "result" and got["last"]["results"]["turn"]["v"] == "ok"
    events = got["events"]
    assert events[0] == "bt_start" and events[-1] == "bt_end" and "bt_walk" in events


def test_reversed_frames_are_flipped_and_a_reversed_tilt_is_flagged() -> None:
    got = _run(stepSign=-1, tiltSign=-1)
    lr, tilt = got["results"]["leftright"], got["results"]["tilt"]
    assert lr["v"] == "warn" and lr["flip"] and lr["flipped"] is True
    assert got["out"]["flips"] == 1
    assert tilt["v"] == "warn" and tilt["reversed"] is True


def test_a_fall_fails_its_check_and_the_test_carries_on() -> None:
    got = _run(fallIn="forward")
    assert got["status"] == "done"
    assert got["results"]["forward"] == {"v": "fail", "why": "fell: tipped"}
    assert got["results"]["back"]["v"] == "ok"
    assert got["results"]["falls"] == {
        "v": "fail",
        "why": "1 fall(s) or grab(s); see the log",
        "n": 1,
    }
    assert "gait_abort" in got["events"]


def test_stop_stop_outside_its_own_check_ends_the_test() -> None:
    got = _run(stopStopIn="hold")
    assert got["status"] == "stopped"
    assert list(got["results"]) == ["leftright", "tilt"]
    assert "bt_stopped" in got["events"] and got["statuses"][-1] == "stopped"


def test_the_screens_stop_ends_it_during_setup_with_no_legs_moved() -> None:
    got = _run(cancelIn="place")
    assert got["status"] == "stopped" and got["results"] == {}
    assert got["out"]["walks"] == 0 and got["out"]["standUps"] == 0


def test_a_stop_stop_never_heard_says_why() -> None:
    got = _run(estop=False, drops=2)
    res = got["results"]["estop"]
    assert res["v"] == "fail" and res["drops"] == 2
    assert "dropped as its own noise" in res["why"]


def test_a_check_without_a_face_says_what_it_could_measure() -> None:
    got = _run(noFace="creep")
    creep = got["results"]["creep"]
    assert creep["cm"] is None and creep["deg"] == 0


@pytest.mark.parametrize(
    ("before", "after", "verdict"),
    [(0, 12, "ok"), (0, -12, "warn"), (0, 3, "warn"), (None, 3, "warn")],
)
def test_the_left_right_verdict(before: float | None, after: float, verdict: str) -> None:
    got = _eval(f"return m.judgeLeftRight({json.dumps(before)}, {after});")
    assert got["v"] == verdict
    assert bool(got.get("flip")) == (before == 0 and after == -12)


def test_the_other_verdicts_thresholds() -> None:
    got = _eval(
        "return ["
        "m.judgeTilt(10, 6, 80, 85).v, m.judgeTilt(10, 14, 80, 85).v,"
        "m.judgeTilt(10, 11, 80, 80).v,"
        "m.judgeCreep(1, 1.04, 0, 3, 15).v, m.judgeCreep(1, 1.08, 0, 3, 15).v,"
        "m.judgeCreep(1, 1, 0, 12, 15).v,"
        "m.judgeWalk(1, 0.9, 1).v, m.judgeWalk(1, 1.1, 1).v, m.judgeWalk(1, 0.99, 1).v,"
        "m.judgeWalk(1, 1.1, -1).v, m.judgeEstop(1200, 1, 0).v, m.judgeEstop(null, 0, 0).why,"
        "m.judgeFalls(0).v, m.judgeFalls(2).v,"
        "m.judgeTurn({reached: 88, secs: 3}, {reached: -40, secs: null}).v];"
    )
    assert got == [
        "ok",
        "warn",
        "warn",
        "ok",
        "warn",
        "warn",
        "ok",
        "fail",
        "warn",
        "ok",
        "ok",
        "nothing was heard; stopped by the time limit",
        "ok",
        "fail",
        "warn",
    ]


def test_stop_stop_with_the_legs_still_ends_it_too() -> None:
    # gbEStop logs no estop with nothing moving, so the test hears it itself.
    got = _run(sayIn={"stand": "Stop, stop!"})
    assert got["status"] == "stopped" and got["stopped"] == ["stop stop"]
    assert got["out"]["walks"] == 0


def test_whispers_silence_lines_are_never_taken_as_words() -> None:
    quiet = _run(sayIn={"power": "Thank you."})
    plain = _run()
    assert quiet["status"] == "done" and quiet["heardN"] == plain["heardN"]


def test_a_test_left_in_setup_ends_rather_than_log_forever() -> None:
    got = _run(neverReady=True)
    assert got["status"] == "stopped" and got["stopped"] == ["setup_timeout"]
    assert got["secs"] == pytest.approx(600, abs=1)


def test_after_a_fall_it_waits_out_the_cool_off_before_walking_again() -> None:
    got = _run(fallIn="forward")
    # The page refuses a walk for 4 s after a tip or a grab, so the next check waits that out.
    after = [t for t in got["walkAt"] if t > got["abortAt"][0]]
    assert after[0] - got["abortAt"][0] >= 4000
    assert got["results"]["back"]["v"] == "ok"


def test_a_tap_never_skips_power_before_the_legs_answer() -> None:
    """Run #1 (2026-11-16): a tap passed "power on" with the body off, so nothing could stand."""
    got = _run(bodyLate=5000)
    assert got["status"] == "done"
    assert got["stepAt"]["stand"] >= 5000


def test_lying_with_no_get_up_it_asks_to_be_stood_up_by_hand() -> None:
    """Runs #1, #26 and #36: the get-up was refused, so "stand up" moved nothing and said
    nothing. Now it says so, and carries on once stood up by hand."""
    got = _run(upright="hand", standUp="")
    assert got["status"] == "done", got["stopped"]
    assert got["stands"][0] == [False, "none"]
    assert got["stands"][-1] == [True, "by_hand"]
    # The refused try, then the tall stance once it is up.
    assert got["out"]["standUps"] == 2


def test_lying_it_gets_up_and_goes_on_once_upright() -> None:
    got = _run(upright="lying", standUp="getup")
    assert got["status"] == "done"
    assert got["stands"] == [[True, "getup"]]
    assert got["out"]["standUps"] == 1
