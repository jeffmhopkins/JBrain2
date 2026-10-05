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
    assert not spec.get("volumes"), "the browser must mount nothing"
    assert not spec.get("environment"), "the browser needs no env: no secrets, no data"
    assert not spec.get("ports"), "only the api, on `browser`, reaches the MCP endpoint"
    assert spec["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in spec["security_opt"]
    assert spec["shm_size"] and spec["mem_limit"] and spec["pids_limit"] > 0


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
