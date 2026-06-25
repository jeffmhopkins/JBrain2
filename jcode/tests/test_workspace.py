"""The repo/ref validators that keep caller input out of git's option/transport
surface (the ext::/file:// and leading-dash vectors)."""

from __future__ import annotations

import pytest

from jcode_ctl.workspace import WorkspaceError, validate_ref, validate_repo


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
