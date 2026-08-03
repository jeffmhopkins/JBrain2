"""The assembled script carries the clone, every phase, and the two gates."""

from __future__ import annotations

from jlaunch_ctl.runner import RC_ARTIFACT_MISSING, RC_VERIFY_FAILED, build_script
from jlaunch_ctl.specs import get_spec


def test_script_includes_clone_phases_and_gates() -> None:
    spec = get_spec("erdos_straus_1e12")
    assert spec is not None
    script = build_script(spec, "/work/.state/abcd")

    # Clone is phase 0, with the ext/file transports denied and the URL after `--`.
    assert "protocol.ext.allow=never" in script
    assert "-- https://github.com/jeffmhopkins/Erd-s-Straus-attack ." in script
    # Every declared phase is run.
    for name in ("clone", "install", "smoke", "run"):
        assert f"run_phase {name} " in script or name in script
    # Both spec-independent gates, keyed by their sentinel return codes.
    assert f"exit {RC_VERIFY_FAILED}" in script
    assert f"exit {RC_ARTIFACT_MISSING}" in script
    assert "VERIFICATION OK" in script
    assert "headline.txt" in script
    assert 'echo "JLAUNCH_DONE ok"' in script


def test_script_without_repo_skips_clone() -> None:
    spec = get_spec("erdos_straus_1e12")
    assert spec is not None
    no_repo = spec.__class__(**{**spec.__dict__, "repo_url": ""})
    script = build_script(no_repo, "/tmp/s")
    assert "git -c protocol" not in script
