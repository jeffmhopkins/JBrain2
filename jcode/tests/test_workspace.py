"""The repo/ref validators that keep caller input out of git's option/transport
surface (the ext::/file:// and leading-dash vectors)."""

from __future__ import annotations

import pytest

from jcode_ctl.workspace import (
    GitWorkspace,
    WorkspaceError,
    validate_ref,
    validate_repo,
)


def test_prepare_home_creates_the_per_session_bin_and_npm_dirs(tmp_path) -> None:
    # prepare_home lays down the per-session HOME skeleton (no git/network): the bin dir
    # that leads PATH and the npm prefix. Idempotent so a restart re-provisions safely.
    home = tmp_path / "home" / "s1"
    ws = GitWorkspace(allowlist=[])
    ws.prepare_home(home)
    ws.prepare_home(home)  # second call must not raise
    assert (home / ".local" / "bin").is_dir()
    assert (home / ".npm-global").is_dir()


def test_empty_repo_is_a_scratch_workspace() -> None:
    validate_repo("")  # no raise


@pytest.mark.parametrize("repo", ["https://github.com/me/r", "git://host/r"])
def test_allowed_schemes(repo: str) -> None:
    validate_repo(repo)  # no raise


@pytest.mark.parametrize(
    "repo",
    [
        "ext::sh -c 'touch /tmp/pwned'",
        "file:///etc/passwd",
        "git@github.com:me/r",  # scp-like ssh
        "/home/user/secret",  # local path
        "-oProxyCommand=evil",  # leading dash
        "https://host/r::weird",  # embedded ::
    ],
)
def test_rejected_repos(repo: str) -> None:
    with pytest.raises(WorkspaceError):
        validate_repo(repo)


@pytest.mark.parametrize("ref", ["-x", "--upload-pack=evil"])
def test_option_like_refs_rejected(ref: str) -> None:
    with pytest.raises(WorkspaceError):
        validate_ref(ref, field="branch")


def test_normal_ref_ok() -> None:
    validate_ref("main", field="branch")
    validate_ref("jcode/spike", field="work_branch")
