"""The credential-exchange POSTs answer anyone, so their bodies are bounded twice: Caddy caps
the request body on exactly those paths (never all of /api, which would break uploads and
imports), and each token field has a max length. A real-format credential still fits both.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from jbrain.api.auth import LoginRequest
from jbrain.api.intake import RedeemRequest as IntakeRedeem
from jbrain.api.jcode_share import RedeemRequest as ShareRedeem
from jbrain.api.pairing import RedeemRequest as PairingRedeem
from jbrain.api.session_bridge import MintRequest
from jbrain.auth import keys
from tests.unit.test_route_auth_matrix import COOKIE_EXEMPT

_CADDYFILE = Path(__file__).resolve().parents[3] / "deploy" / "Caddyfile"


def _snippet() -> str:
    caddyfile = _CADDYFILE.read_text()
    return caddyfile[caddyfile.index("(app) {") : caddyfile.index("{$JBRAIN_SITE_ADDR} {")]


def _capped_paths(snippet: str) -> set[str]:
    block = re.search(r"@unauthed_post \{(.*?)\}", snippet, re.S)
    assert block is not None
    lines = [ln.split() for ln in block.group(1).strip().splitlines()]
    assert ["method", "POST"] in lines
    [paths] = [ln[1:] for ln in lines if ln[0] == "path"]
    return set(paths)


def test_caddy_caps_every_unauthenticated_post_and_nothing_else() -> None:
    snippet = _snippet()
    # Bearer-credentialed routes refuse an anonymous request before reading the body.
    unauthenticated = {
        path
        for (method, path) in COOKIE_EXEMPT
        if method == "POST" and not path.startswith("/api/ext/")
    }
    assert _capped_paths(snippet) == unauthenticated
    # The cap rides that matcher only: one request_body in the snippet, and it is matched,
    # so a large upload or import to any other /api path is untouched.
    assert snippet.count("request_body") == 1
    assert re.search(r"request_body @unauthed_post \{\s*max_size 64KB\s*\}", snippet)
    # Inside the /api handle, ahead of the proxy it guards; every app site imports it.
    api = snippet[snippet.index("handle /api/* {") :]
    assert api.index("request_body") < api.index("reverse_proxy api:8000")


def test_real_credentials_fit_the_bounds() -> None:
    LoginRequest(owner_key=keys.generate_owner_key(), device_label="Jeff's phone")
    LoginRequest(owner_key=keys.generate_owner_key(), device_label="d" * 200)
    # Device keys are minted in the owner-key format (devices/service.py, locations/pairing.py).
    MintRequest(device_key=keys.generate_owner_key())
    IntakeRedeem(secret=keys.generate_capability_key())
    ShareRedeem(token=keys.generate_capability_key())
    PairingRedeem(code="a" * 256)


@pytest.mark.parametrize(
    ("model", "field"),
    [
        (LoginRequest, "owner_key"),
        (MintRequest, "device_key"),
        (IntakeRedeem, "secret"),
        (ShareRedeem, "token"),
        (PairingRedeem, "code"),
        (LoginRequest, "device_label"),
    ],
)
def test_an_oversized_credential_is_refused(model: type, field: str) -> None:
    with pytest.raises(ValidationError):
        fields = {"owner_key": keys.generate_owner_key()} if model is LoginRequest else {}
        model(**{**fields, field: "a" * 100_000})
