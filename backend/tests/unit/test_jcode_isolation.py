"""Wave P0 of per-session jcode isolation, asserted on the deploy config
(docs/JCODE_SESSION_ISOLATION_PLAN.md). The jcode service ships a tailored seccomp profile
— Docker's default plus a single rule allowing the namespace syscalls without
CAP_SYS_ADMIN — so the control server can later put each session's shell in its own
user+net namespace. The capability is dormant: JCODE_SESSION_ISOLATION (off by default)
gates whether it's used. This wave only lays the substrate; no behaviour changes."""

import json
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[3]
_DEPLOY = _ROOT / "deploy"
_PROFILE = _DEPLOY / "jcode-seccomp.json"
_COMPOSE = _DEPLOY / "docker-compose.yml"

# The four namespace syscalls the control server (or bwrap) needs to create a user+net
# namespace and for the proxy to enter it. Allowing them unconditionally lifts the
# default profile's CAP_SYS_ADMIN gate for JUST these — the rest of that block stays gated.
_NS_SYSCALLS = ["clone", "clone3", "unshare", "setns"]


def _profile() -> dict:
    return json.loads(_PROFILE.read_text())


def test_seccomp_profile_is_valid_and_keeps_the_deny_default() -> None:
    prof = _profile()
    # Still deny-by-default like Docker's stock profile — we only widened four syscalls.
    assert prof["defaultAction"] == "SCMP_ACT_ERRNO"
    # It's the real default profile (has the arch map + a substantial syscall set), not a
    # hand-rolled allow-list that silently drops protections.
    assert "archMap" in prof
    assert len(prof["syscalls"]) > 20


def test_seccomp_profile_allows_the_namespace_syscalls_without_cap() -> None:
    blocks = _profile()["syscalls"]
    allow = [
        b
        for b in blocks
        if b.get("names") == _NS_SYSCALLS
        and b["action"] == "SCMP_ACT_ALLOW"
        and not b.get("includes")  # unconditional — NOT gated on CAP_SYS_ADMIN
    ]
    assert len(allow) == 1, "expected exactly one unconditional namespace-allow rule"


def test_seccomp_profile_drops_the_conflicting_clone3_denial() -> None:
    # The stock profile force-ERRNOs clone3 (no cap); that must be gone or it conflicts
    # with the allow above and namespace creation stays blocked.
    blocks = _profile()["syscalls"]
    assert not [
        b for b in blocks if b.get("names") == ["clone3"] and b["action"] == "SCMP_ACT_ERRNO"
    ]


def test_only_the_stock_list_and_the_namespace_rule_are_unconditionally_allowed() -> None:
    # The complete invariant (stronger than spot-checking a few names): the ONLY blocks
    # that allow syscalls with no caps/args gate are the stock big allow-list and our
    # four-name namespace rule. Any new unconditional allow — e.g. someone ungating
    # mount/bpf/perf_event_open — makes a third block appear and fails here.
    uncond = [
        b
        for b in _profile()["syscalls"]
        if b["action"] == "SCMP_ACT_ALLOW" and not b.get("includes") and not b.get("args")
    ]
    assert len(uncond) == 2, "unexpected unconditional-allow block(s)"
    name_sets = sorted((sorted(b["names"]) for b in uncond), key=len)
    assert name_sets[0] == sorted(_NS_SYSCALLS)  # our rule
    assert len(name_sets[1]) > 50  # the stock allow-list, intact


def test_base_profile_protections_are_intact() -> None:
    # Canaries that we widened the real default and didn't regenerate against a wrong base:
    # the CAP_SYS_ADMIN gate and the arg-filtered socket rule must both survive.
    blocks = _profile()["syscalls"]
    assert any(b.get("includes", {}).get("caps") == ["CAP_SYS_ADMIN"] for b in blocks)
    assert any("socket" in (b.get("names") or []) and b.get("args") for b in blocks)


def _jcode() -> dict:
    return yaml.safe_load(_COMPOSE.read_text())["services"]["jcode"]


def test_jcode_service_applies_the_profile_and_gates_use_behind_a_flag() -> None:
    svc = _jcode()
    # Read straight from the source tree (./src/deploy/...), NOT a copied-out file. git
    # reset --hard refreshes src on every update, so the profile can never lag the compose
    # that references it — a copied-out file would, and a stale/missing one fails jcode
    # start (the bootstrap race that bit the first deploy).
    assert "seccomp=./src/deploy/jcode-seccomp.json" in svc["security_opt"]
    # Off by default; the flag gates whether namespaces are actually used (later waves).
    assert svc["environment"]["JCODE_SESSION_ISOLATION"] == "${JCODE_SESSION_ISOLATION:-false}"


def test_profile_is_read_from_src_so_no_copy_bootstrap_is_needed() -> None:
    # The profile lives only in the source tree; the deploy helpers must NOT try to copy it
    # to the project root (that copy is gated by the NEW helper, which the running update
    # is not — the race that failed jcode start). Referencing src sidesteps it entirely.
    seccomp = next(o for o in _jcode()["security_opt"] if o.startswith("seccomp="))
    assert seccomp.split("=", 1)[1].startswith("./src/deploy/")
