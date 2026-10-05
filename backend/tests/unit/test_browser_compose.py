"""The fence around the headless browser, asserted on the compose file and the proxy config.

The browser reads attacker-controlled pages with a model that injection succeeds against
often, so its containment is the NETWORK (docs/plans/BROWSER_AGENT_PLAN.md §2), and a network
claim is a claim about membership: one extra service on `browser`, or the browser on one
extra network, silently hands it a route. None of this runs Docker; it reads the files the
box is built from, the way `test_pysandbox_server.py` guards the code sandbox. The live half
— the fence failing closed against 169.254.169.254, an RFC1918 address, `db:5432` and
`searxng:8080`, via a redirect and a rebinding name — is B0's on-box check, through
`debug-connect.sh browse`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_DEPLOY = Path(__file__).resolve().parents[3] / "deploy"
_SQUID = (_DEPLOY / "egress" / "squid.conf").read_text()


def _compose() -> dict[str, Any]:
    return yaml.safe_load((_DEPLOY / "docker-compose.yml").read_text())


def _members(network: str) -> set[str]:
    return {
        name
        for name, spec in _compose()["services"].items()
        if network in (spec.get("networks") or [])
    }


# --- Membership ------------------------------------------------------------------------


def test_the_browser_network_has_no_gateway() -> None:
    assert _compose()["networks"]["browser"]["internal"] is True


def test_only_the_api_and_the_egress_proxy_share_the_browsers_network() -> None:
    assert _members("browser") == {"browser", "api", "egress"}


def test_the_browser_is_on_its_own_network_and_nothing_else() -> None:
    assert _compose()["services"]["browser"]["networks"] == ["browser"]


def test_only_the_proxy_has_a_way_out_and_it_never_touches_internal() -> None:
    compose = _compose()
    # A plain bridge: the one network in this fence WITH a gateway, so the proxy can reach out.
    assert not (compose["networks"]["browser_out"] or {}).get("internal")
    assert _members("browser_out") == {"egress"}
    assert compose["services"]["egress"]["networks"] == ["browser", "browser_out"]


def test_nothing_that_holds_data_or_models_shares_a_network_with_the_browser() -> None:
    """db, the supervisor (which holds the Docker socket), the worker, the model servers and
    the blob-holding services are unreachable from the browser by construction."""
    services = _compose()["services"]
    browser_nets = set(services["browser"]["networks"])
    for name in ("db", "supervisor", "worker", "local-llm", "flash-next", "embed", "comfyui"):
        assert not browser_nets & set(services[name].get("networks") or []), name


# --- The browser container ----------------------------------------------------------------


def test_the_image_is_pinned_by_tag_and_digest() -> None:
    image = _compose()["services"]["browser"]["image"]
    default = image.split(":-", 1)[1].rstrip("}")
    assert default.startswith("mcr.microsoft.com/playwright/mcp:v")
    assert re.search(r"@sha256:[0-9a-f]{64}$", default)
    assert ":latest" not in default


def test_the_browser_is_launched_fenced_and_isolated() -> None:
    spec = _compose()["services"]["browser"]
    flags = spec["command"]
    assert "--isolated" in flags
    assert "--proxy-server=http://egress:3128" in flags
    assert "--no-webmcp" in flags
    assert "--image-responses=omit" in flags
    assert "--port=8931" in flags
    allowed = next(f for f in flags if f.startswith("--allowed-hosts="))
    # The MCP server's own Host check: never disabled, never open to loopback names.
    assert "*" not in allowed and "localhost" not in allowed and "127.0.0.1" not in allowed
    # No `--caps` (no devtools/pdf/vision surface), no user data dir, no saved session.
    assert not any(f.startswith(("--caps", "--user-data-dir", "--save-session")) for f in flags)
    # The image's entrypoint (`--headless --no-sandbox`) is kept, not replaced.
    assert "entrypoint" not in spec


def test_the_browser_holds_nothing_worth_reaching() -> None:
    spec = _compose()["services"]["browser"]
    # One read-only mount: its launch config, from the checkout. No data, no credential.
    assert spec["volumes"] == ["./src/deploy/browser:/etc/playwright-mcp:ro"]
    assert not spec.get("environment"), "the browser needs no env: no secrets, no data"
    assert not spec.get("ports"), "only the api, on `browser`, reaches the MCP endpoint"
    assert spec["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in spec["security_opt"]
    assert spec["shm_size"] and spec["mem_limit"] and spec["pids_limit"] > 0


def test_the_browser_cannot_fill_the_disk() -> None:
    """A page that downloads or writes without end fills a size-capped RAM disk, never the
    box's disk: the root is read-only, every writable path is a sized tmpfs, and the MCP
    server evicts its own old outputs."""
    spec = _compose()["services"]["browser"]
    assert spec["read_only"] is True
    mounts = {m.split(":", 1)[0]: m for m in spec["tmpfs"]}
    assert set(mounts) == {"/tmp", "/home/node"}
    assert all("size=" in m for m in mounts.values())
    flags = spec["command"]
    assert "--output-dir=/tmp/playwright-mcp" in flags  # inside the capped /tmp
    assert any(f.startswith("--output-max-size=") for f in flags)


def test_webrtc_cannot_send_udp_around_the_proxy() -> None:
    import json

    spec = _compose()["services"]["browser"]
    assert "--config=/etc/playwright-mcp/config.json" in spec["command"]
    config = json.loads((_DEPLOY / "browser" / "config.json").read_text())
    args = config["browser"]["launchOptions"]["args"]
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in args


def test_the_accepted_api_reachability_risk_is_written_down() -> None:
    """The api shares `browser`, so a compromised Chromium could reach api:8000 past Caddy.
    Accepted for now and owned by B2; the record of that lives beside the service."""
    text = (_DEPLOY / "docker-compose.yml").read_text()
    assert "ACCEPTED RISK" in text and "api:8000" in text


def test_the_browser_and_its_proxy_are_stock_stack() -> None:
    """CLAUDE.md #10: a profile would mean the tool exists only on a box where someone ran a
    CLI command."""
    services = _compose()["services"]
    assert "profiles" not in services["browser"] and "profiles" not in services["egress"]


def test_the_api_defaults_to_the_running_browser() -> None:
    config = (_DEPLOY.parent / "backend" / "src" / "jbrain" / "config.py").read_text()
    assert 'browser_mcp_url: str = "http://browser:8931/mcp"' in config


# --- The proxy ---------------------------------------------------------------------------


def test_the_proxy_is_built_from_a_digest_pinned_base_and_checks_its_config() -> None:
    spec = _compose()["services"]["egress"]
    assert spec["build"]["dockerfile"] == "deploy/Dockerfile.egress"
    assert spec["cap_drop"] == ["ALL"] and set(spec["cap_add"]) == {"SETUID", "SETGID"}
    assert not spec.get("ports") and not spec.get("volumes")
    dockerfile = (_DEPLOY / "Dockerfile.egress").read_text()
    assert re.search(r"^FROM \S+@sha256:[0-9a-f]{64}$", dockerfile, re.MULTILINE)
    assert "COPY deploy/egress/squid.conf /etc/squid/squid.conf" in dockerfile
    assert "squid -k parse" in dockerfile


def _denied_ranges() -> set[str]:
    return set(re.findall(r"^acl non_public dst (\S+)$", _SQUID, re.MULTILINE))


def test_the_proxy_denies_every_non_public_range() -> None:
    assert {
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "100.64.0.0/10",
        "0.0.0.0/8",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
        "::ffff:0:0/96",
        "2002::/16",
        "2001::/32",
    } <= _denied_ranges()


def test_the_proxy_denies_every_compose_service_by_name() -> None:
    """Kept in step with the compose file: a service added without an entry here fails."""
    named: set[str] = set()
    for line in re.findall(r"^acl compose_services dstdomain (.+)$", _SQUID, re.MULTILINE):
        named |= set(line.split())
    assert set(_compose()["services"]) <= named


def test_the_denies_come_before_the_allow() -> None:
    rules = re.findall(r"^http_access (\S+) (.+)$", _SQUID, re.MULTILINE)
    assert rules[-1] == ("allow", "all")
    denied = {target for verb, target in rules[:-1] if verb == "deny"}
    assert {"non_public", "compose_services", "single_label", "lan_suffix", "!web_ports"} <= denied
    assert all(verb == "deny" for verb, _ in rules[:-1])


def test_the_proxy_keeps_no_copies_and_names_no_one() -> None:
    assert "cache deny all" in _SQUID
    assert "forwarded_for delete" in _SQUID and "via off" in _SQUID


def test_the_proxy_bounds_a_response_and_refuses_localhost_names() -> None:
    assert re.search(r"^reply_body_max_size \d+ MB$", _SQUID, re.MULTILINE)
    lan = re.search(r"^acl lan_suffix dstdomain (.+)$", _SQUID, re.MULTILINE)
    assert lan is not None and ".localhost" in lan[1].split()


def test_ci_runs_the_built_proxy_against_its_deny_matrix() -> None:
    ci = (_DEPLOY.parent / ".github" / "workflows" / "ci.yml").read_text()
    assert "bash deploy/egress/fence-check.sh http://127.0.0.1:3128" in ci
    check = (_DEPLOY / "egress" / "fence-check.sh").read_text()
    for target in ("169.254.169.254", "db:5432", "searxng:8080", "100.64.0.1", "localtest.me"):
        assert target in check
