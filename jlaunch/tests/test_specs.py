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
