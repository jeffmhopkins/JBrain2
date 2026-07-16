"""The grok login-shell hook renders ~/.grok/config.toml from the api proxy's list.

Runs the real `grok-config.sh` with a FAKE `grok` (so the hook's guard fires) and a
FAKE `curl` standing in for the api's /models endpoint — no network. Asserts the
multi-model `/model` config (one quoted block per served name, all pointed at the
proxy) and the single-model fallback when the list can't be fetched.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_HOOK = Path(__file__).resolve().parents[1] / "grok-config.sh"

# alias|served|label|window (the proxy's ?format=lines output).
_LINES = (
    "oss|gpt-oss-120b|GPT-OSS 120B · reasoning|131072\n"
    "qwen|qwen3-coder-next|Qwen3-Coder-Next 80B · coding agent (Q4)|262144\n"
    "glm|glm-4.5-air|GLM-4.5 Air · reasoning (alt)|131072\n"
)


def _fakebin(tmp_path: Path, *, curl_body: str) -> Path:
    """A PATH dir with a stub `grok` (so the hook's guard fires) and a stub `curl`."""
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    grok = fakebin / "grok"
    grok.write_text("#!/bin/sh\necho grok 1.0.0\n")
    grok.chmod(0o755)
    curl = fakebin / "curl"
    curl.write_text(curl_body)
    curl.chmod(0o755)
    return fakebin


def _run(tmp_path: Path, fakebin: Path, *, model: str = "qwen3-coder-next") -> str:
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": f"{fakebin}:/usr/bin:/bin",
        "HOME": str(home),
        "GROK_MODELS_BASE_URL": "http://api:8000/api/jcode/llm/v1",
        "GROK_API_KEY": "sk-jcode",
        "GROK_MODEL": model,
    }
    res = subprocess.run(["bash", str(_HOOK)], capture_output=True, text=True, env=env)
    assert res.returncode == 0, res.stderr
    return (home / ".grok" / "config.toml").read_text()


def test_renders_every_installed_model_as_a_switchable_block(tmp_path: Path) -> None:
    # A curl that echoes the proxy's `served|label|window` lines regardless of args.
    curl = "#!/usr/bin/env bash\ncat <<'EOF'\n" + _LINES + "EOF\n"
    toml = _run(tmp_path, _fakebin(tmp_path, curl_body=curl))

    # The default maps to the session model's alias (qwen3-coder-next → qwen).
    assert 'default = "qwen"' in toml
    # Block keys are short aliases (`/model oss`); the served name is `model =`.
    assert '[model."oss"]' in toml and 'model = "gpt-oss-120b"' in toml
    assert '[model."qwen"]' in toml and 'model = "qwen3-coder-next"' in toml
    assert '[model."glm"]' in toml and 'model = "glm-4.5-air"' in toml
    # Each block points at the proxy, with the model's own window + the shared key.
    assert 'base_url = "http://api:8000/api/jcode/llm/v1"' in toml
    assert "context_window = 131072" in toml
    assert "context_window = 262144" in toml
    assert 'env_key = "GROK_API_KEY"' in toml
    # Subagents on (the proxy's swap lock serializes them, never parallel), with the
    # built-in plan subagent bound to the reasoner by its alias.
    assert "[subagents]" in toml and "enabled = true" in toml
    assert "[subagents.models]" in toml and 'plan = "oss"' in toml


def test_falls_back_to_the_single_pinned_model_when_the_list_is_unavailable(
    tmp_path: Path,
) -> None:
    # curl fails (api unreachable) → the hook writes just the session's pinned model.
    curl = "#!/usr/bin/env bash\nexit 1\n"
    toml = _run(tmp_path, _fakebin(tmp_path, curl_body=curl), model="qwen3-coder-next")
    assert 'default = "qwen3-coder-next"' in toml
    assert '[model."qwen3-coder-next"]' in toml
    # No other model blocks were invented from a failed fetch.
    assert toml.count('[model."') == 1
    # Subagents still enabled, but no plan pin — the reasoner isn't in the (empty) list.
    assert "enabled = true" in toml
    assert "[subagents.models]" not in toml
