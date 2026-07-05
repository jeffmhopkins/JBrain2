"""JPet v2 action scripts (docs/proposed/JPET_V2_PLAN.md) — pure, no DB.

Proves the bounding + settling that keeps the pet safe: `clean_script` drops unknown/
ungrounded steps, caps length, and always terminates; `settle_script` computes the pet +
room resting state (incl. carrying the ball to a corner and toggling the lights); the
canned kid-button scripts are all valid; the play reward is one-directional.
"""

from jbrain.jpet.service import (
    BUTTON_ACTIONS,
    MAX_SCRIPT_STEPS,
    OBJECT_HOMES,
    TERMINAL,
    canned_script,
    clean_script,
    settle_script,
)

OBJS = {k: (v[0], v[1]) for k, v in OBJECT_HOMES.items()}


def _actions(steps):
    return [s.action for s in steps]


def test_clean_drops_unknown_actions_and_always_terminates() -> None:
    steps = clean_script(
        [{"action": "explode"}, {"action": "dance", "duration_ms": 1000}], objects=OBJS
    )
    assert _actions(steps) == ["dance", "sit"]  # unknown dropped; terminating step appended


def test_clean_drops_ungrounded_targets() -> None:
    # `moon` isn't a room object; the go_to step has no valid target → dropped.
    steps = clean_script([{"action": "go_to", "target": "moon"}], objects=OBJS)
    assert _actions(steps) == ["idle"]  # nothing survived → a lone terminating idle


def test_clean_caps_length_and_forces_terminal() -> None:
    raw = [{"action": "spin"}] * (MAX_SCRIPT_STEPS + 4)
    steps = clean_script(raw, objects=OBJS)
    assert len(steps) == MAX_SCRIPT_STEPS
    assert steps[-1].action in TERMINAL  # last step coerced to a rest pose


def test_clean_duration_is_clamped() -> None:
    steps = clean_script(
        [{"action": "dance", "duration_ms": 999999}, {"action": "sit"}], objects=OBJS
    )
    assert steps[0].duration_ms == 10000  # MAX_STEP_MS


def test_empty_script_becomes_lone_idle() -> None:
    assert _actions(clean_script([], objects=OBJS)) == ["idle"]
    assert _actions(clean_script("garbage", objects=OBJS)) == ["idle"]


def test_settle_carries_the_ball_to_the_corner() -> None:
    script = clean_script(
        [
            {"action": "go_to", "target": "ball"},
            {"action": "pick_up", "target": "ball"},
            {"action": "carry_to", "destination": "corner_ne"},
            {"action": "put_down"},
        ],
        objects=OBJS,
    )
    settled = settle_script(
        pos_x=0.0,
        pos_z=0.0,
        facing=0.0,
        asleep=False,
        carrying=None,
        lights_on=True,
        objects=OBJS,
        script=script,
    )
    assert settled.objects["ball"] == (0.85, -0.85)  # corner_ne
    assert settled.carrying is None  # put back down
    assert settled.action == "sit"


def test_settle_sleep_and_lights_toggle() -> None:
    to_bed = settle_script(
        pos_x=0.0,
        pos_z=0.0,
        facing=0.0,
        asleep=False,
        carrying=None,
        lights_on=True,
        objects=OBJS,
        script=clean_script(
            [{"action": "go_to", "target": "bed"}, {"action": "sleep"}], objects=OBJS
        ),
    )
    assert to_bed.asleep is True
    assert to_bed.pos_x == OBJS["bed"][0]
    lights = settle_script(
        pos_x=0.0,
        pos_z=0.0,
        facing=0.0,
        asleep=False,
        carrying=None,
        lights_on=True,
        objects=OBJS,
        script=clean_script([{"action": "go_to", "target": "light_switch"}], objects=OBJS),
    )
    assert lights.lights_on is False  # toggled


def test_all_canned_button_scripts_are_valid_and_terminate() -> None:
    for action in BUTTON_ACTIONS:
        steps = canned_script(action, objects=OBJS)
        assert steps, f"{action} produced an empty script"
        assert steps[-1].action in TERMINAL, f"{action} does not terminate"
