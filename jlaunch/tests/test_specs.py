"""The registered specs are well-formed; the Erdos-Straus spec matches RUN_1E12.md."""

from __future__ import annotations

import pytest

from jlaunch_ctl.specs import SPECS, JobSpec, Phase, SpecError, _validate, get_spec


def test_erdos_straus_spec_registered_and_shaped() -> None:
    spec = get_spec("erdos_straus_1e12")
    assert spec is not None
    assert spec.repo_url == "https://github.com/jeffmhopkins/Erd-s-Straus-attack"
    assert spec.artifact_path == "es_1e12_artifacts.tar.gz"
    assert spec.verify_log == "run_1e12_verify.log"
    assert spec.verify_must_end_with == "VERIFICATION OK"
    assert [p.name for p in spec.phases] == ["install", "smoke", "run"]
    assert "hard primes:" in spec.headline_markers


def test_public_omits_no_required_field() -> None:
    pub = SPECS["erdos_straus_1e12"].public()
    assert pub["name"] == "erdos_straus_1e12"
    assert pub["phases"] == ["install", "smoke", "run"]
    assert pub["disk_gb"] == 15


def test_validate_rejects_non_url_repo() -> None:
    bad = JobSpec(
        name="bad",
        title="bad",
        repo_url="ext::sh -c whoami",
        branch="main",
        phases=(Phase("x", "true"),),
        artifact_path="a",
        artifact_media_type="application/gzip",
        verify_log="v",
        verify_must_end_with="OK",
        headline_markers=(),
        est_hours="",
        disk_gb=0,
        all_cores=False,
        notes="",
    )
    with pytest.raises(SpecError):
        _validate(bad)


def test_get_spec_unknown_is_none() -> None:
    assert get_spec("nope") is None


def test_native_spec_registered_and_no_clone() -> None:
    spec = get_spec("erdos_straus_1e12_native")
    assert spec is not None
    assert spec.repo_url == ""  # baked-in es-census binary: nothing to clone
    assert spec.all_cores is True
    assert [p.name for p in spec.phases] == ["census", "package"]
    assert spec.artifact_path == "es_1e12_artifacts.tar.gz"
    assert spec.verify_must_end_with == "VERIFICATION OK"
    assert "hard primes:" in spec.headline_markers
    census = spec.phases[0].run
    assert "es-census" in census
    assert "--verify-log run_1e12_verify.log" in census


def test_native_1e13_spec_registered() -> None:
    spec = get_spec("erdos_straus_1e13_native")
    assert spec is not None
    assert spec.repo_url == ""  # baked-in binary, no clone
    assert spec.all_cores is True
    assert [p.name for p in spec.phases] == ["census", "package"]
    assert spec.artifact_path == "es_1e13_artifacts.tar.gz"
    assert spec.verify_log == "run_1e13_verify.log"
    census = spec.phases[0].run
    assert "--max 10000000000000" in census  # exactly 10^13
    assert "--verify-log run_1e13_verify.log" in census


def test_validate_allows_empty_repo() -> None:
    spec = JobSpec(
        name="baked",
        title="baked",
        repo_url="",  # no clone
        branch="",
        phases=(Phase("run", "true"),),
        artifact_path="a",
        artifact_media_type="application/gzip",
        verify_log="v",
        verify_must_end_with="OK",
        headline_markers=(),
        est_hours="",
        disk_gb=0,
        all_cores=True,
        notes="",
    )
    assert _validate(spec) is spec


def test_native_spec_build_script_skips_clone() -> None:
    from jlaunch_ctl.runner import build_script

    native = get_spec("erdos_straus_1e12_native")
    py = get_spec("erdos_straus_1e12")
    assert native is not None and py is not None
    native_script = build_script(native, "/tmp/state")
    assert "clone" not in native_script  # no repo -> the runner omits the clone phase
    assert "clone" in build_script(py, "/tmp/state")  # the Python spec still clones
    assert "run_phase census" in native_script
    assert "run_phase package" in native_script
