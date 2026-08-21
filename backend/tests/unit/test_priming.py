"""jerv_prime_spec: the (system, tools) a gateway warm-up sends to prime jerv's turn-one
prefix, and the openai_tools serialization it shares with a real turn's payload. The
invariant under test is that the primed shape tracks a real turn (empty read scope, jerv's
allowlist + extra grant, the same hidden set), so the gateway's --cache-reuse can reuse it.
"""

from collections.abc import Collection
from typing import Any, cast

from jbrain.agent.agents import AGENTS
from jbrain.agent.priming import (
    jerv_prime_fingerprint,
    jerv_prime_spec,
    template_renders_date,
)
from jbrain.agent.readtools import OPTIONAL_CANVAS_TOOLS, OPTIONAL_CROP_TOOLS
from jbrain.agent.toolregistry import ToolRegistry
from jbrain.llm.openai_compat import openai_tools
from jbrain.llm.types import LlmTool

# Both canvas tools and the crop tool ride the same model gate.
GATED = OPTIONAL_CANVAS_TOOLS | OPTIONAL_CROP_TOOLS


class _RecordingRegistry:
    """Records the schemas_for arguments and returns a fixed tool list, so a test can assert
    the prime asks for exactly what a real jerv turn does without building the real registry."""

    def __init__(self, tools: list[LlmTool]):
        self._tools = tools
        self.calls: list[tuple[Any, ...]] = []

    def schemas_for(
        self,
        scopes: Collection[str],
        allow: Collection[str] | None = None,
        extra: Collection[str] = (),
        hidden: Collection[str] = (),
    ) -> list[LlmTool]:
        self.calls.append((tuple(scopes), allow, tuple(extra), tuple(sorted(hidden))))
        return self._tools


class _Liveness:
    def __init__(self, hidden: Collection[str], *, boom: bool = False):
        self._hidden = hidden
        self._boom = boom

    async def hidden_tools(self) -> Collection[str]:
        if self._boom:
            raise RuntimeError("comfyui probe failed")
        return self._hidden


def test_openai_tools_serializes_to_the_openai_function_shape() -> None:
    tools = [
        LlmTool(name="web_search", description="Search the web.", input_schema={"type": "object"})
    ]
    assert openai_tools(tools) == [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the web.",
                "parameters": {"type": "object"},
            },
        }
    ]


async def test_jerv_prime_spec_uses_the_persona_and_a_real_turns_tool_query() -> None:
    tools = [LlmTool(name="web_search", description="d", input_schema={})]
    reg = _RecordingRegistry(tools)
    system, primed = await jerv_prime_spec(cast(ToolRegistry, reg), None)

    profile = AGENTS["jerv"]
    assert system == profile.prompt
    # Same query a real turn makes (api.agent): empty read scope, jerv's allowlist + extra
    # grant. No liveness → the image tools are not hidden; the canvas pair still is,
    # because it is model-gated and no served model was named for this prime.
    (scopes, allow, extra, hidden) = reg.calls[0]
    assert scopes == () and allow == profile.tools and extra == tuple(profile.extra_tools)
    assert set(hidden) == GATED
    assert primed == openai_tools(tools)


async def test_jerv_prime_spec_hides_image_tools_when_comfyui_is_down() -> None:
    # jerv holds the image-gen tools, so a down ComfyUI must hide them from the prime too —
    # matching a real turn, which hides them (else the primed prefix stops being a prefix).
    reg = _RecordingRegistry([])
    jerv_tools = AGENTS["jerv"].tools
    assert jerv_tools is not None
    hidden = next(iter(jerv_tools - GATED))  # any non-canvas tool jerv holds
    await jerv_prime_spec(cast(ToolRegistry, reg), _Liveness({hidden}))
    # The canvas pair rides alongside: it is model-gated and no served model was named,
    # so it is hidden here exactly as it would be on a turn routed to an unqualified model.
    assert set(reg.calls[0][3]) == {hidden} | GATED


async def test_jerv_prime_spec_is_best_effort_when_the_liveness_probe_fails() -> None:
    # A probe error must not break the prime (or a load): it hides nothing and primes the
    # persona + full tool set (a live ComfyUI is the steady state anyway).
    reg = _RecordingRegistry([])
    await jerv_prime_spec(cast(ToolRegistry, reg), _Liveness((), boom=True))
    # Only the model-gated canvas pair is hidden (no served model named); the probe
    # failure itself hides nothing.
    assert set(reg.calls[0][3]) == GATED


# The KV-slot state file's name is the fingerprint, so what enters the digest decides how often
# the box rewrites a ~2 GB file and how often it pays a cold prefill for a prefix that did not
# change. These pin the key at "per model, per prompt".

_FP = {
    "build_info": "b1234-abcdef",
    "n_ctx": 262144,
    "n_slots": 1,
    "server_args": ("-fa", "1"),
}

# A date-free template, like the Qwen3.8 hybrid's (checked against the live gateway's /props:
# 10,341 characters, zero date constructs).
_DATELESS = "{% for message in messages %}<|im_start|>{{ message.role }}{% endfor %}"
# Harmony's shape: minja's date builtin rendered into the system header.
_DATED = "System: Current date: {{ strftime_now('%Y-%m-%d') }}\n{% for m in messages %}{% endfor %}"


def test_a_dateless_template_keeps_the_same_fingerprint_across_days() -> None:
    """The whole point of the change: a prompt that has not moved must not rewrite its state
    file at UTC midnight. The hybrid was writing ~2 GB every night, orphaning the previous file
    on a volume nothing prunes, and paying a ~100 s cold prefill for an identical prefix."""
    monday = jerv_prime_fingerprint(
        "persona", [{"name": "t"}], chat_template=_DATELESS, today_utc="2026-08-18", **_FP
    )
    tuesday = jerv_prime_fingerprint(
        "persona", [{"name": "t"}], chat_template=_DATELESS, today_utc="2026-08-19", **_FP
    )
    assert monday == tuesday


def test_a_dated_template_still_rotates_daily() -> None:
    """gpt-oss's harmony template renders `Current date:` into the system header, so its prefix
    genuinely differs after the container's UTC midnight. Dropping the date wholesale would make
    it load a ~1 GB state file whose reusable prefix ends at the date token."""
    monday = jerv_prime_fingerprint(
        "persona", [{"name": "t"}], chat_template=_DATED, today_utc="2026-08-18", **_FP
    )
    tuesday = jerv_prime_fingerprint(
        "persona", [{"name": "t"}], chat_template=_DATED, today_utc="2026-08-19", **_FP
    )
    assert monday != tuesday


def test_everything_that_really_changes_the_prefix_still_moves_the_fingerprint() -> None:
    """Dropping the date must not have loosened the key. Each of these rewrites the bytes
    llama-server would prefill, so each must produce a different name and a clean miss."""
    base = dict(
        system="persona", tools=[{"name": "t"}], chat_template=_DATELESS, today_utc="2026-08-19"
    )

    def fp(**over: object) -> str:
        args = {**base, **_FP, **over}
        return jerv_prime_fingerprint(
            args.pop("system"),  # type: ignore[arg-type]
            args.pop("tools"),  # type: ignore[arg-type]
            **args,  # type: ignore[arg-type]
        )

    baseline = fp()
    assert fp(system="a different persona") != baseline  # the pinned prompt moved
    assert fp(tools=[{"name": "t"}, {"name": "u"}]) != baseline  # a tool appeared
    assert fp(build_info="b9999-fedcba") != baseline  # llama.cpp rebuilt
    assert fp(chat_template=_DATELESS + " ") != baseline  # template edit
    assert fp(n_ctx=32768) != baseline  # served -c changed
    assert fp(n_slots=2) != baseline  # served -np changed
    assert fp(server_args=("-fa", "1", "-ctk", "q8_0")) != baseline  # the KV cells changed shape


def test_quantising_the_kv_cache_moves_the_fingerprint() -> None:
    """The regression this argument was added for. MEASURED 2026-08-21: `-ctk/-ctv q8_0` landed
    on the Qwen3.8 27B family and NONE of the other inputs moved with it — same build, same
    persona, same template, same `-c`, same `-np`. So every state file kept a name the keeper
    still considered live, and llama-server answered the restore with `400 mismatched key type
    (8 != 1, layer 0)`: a cold ~100 s prefill on the one model the owner waits on, every restart.

    Asserted on the FLAGS alone, holding everything else fixed, because that is exactly the
    combination the real change presented and the only one the old digest was blind to."""
    fixed = dict(
        system="persona",
        tools=[{"name": "t"}],
        build_info="b1234-abcdef",
        chat_template=_DATELESS,
        n_ctx=262144,
        n_slots=1,
        today_utc="2026-08-19",
    )
    f16 = jerv_prime_fingerprint(server_args=("-fa", "1"), **fixed)  # type: ignore[arg-type]
    q8 = jerv_prime_fingerprint(
        server_args=("-fa", "1", "-ctk", "q8_0", "-ctv", "q8_0"),
        **fixed,  # type: ignore[arg-type]
    )
    assert f16 != q8


def test_the_flags_are_hashed_in_order_and_unresolved() -> None:
    """Hashed RAW: the same flags in a different order are a different launch line as far as
    this digest is concerned, and that is deliberate. `llama_swap_config` drops overridden and
    speculative flags before it stamps the config, and re-deriving that here would be a second
    implementation of the resolution rules, free to drift from the first. Over-rotating costs
    one prefill; under-rotating restores state the server will reject or, worse, accept."""
    fixed = dict(
        system="persona",
        tools=[{"name": "t"}],
        build_info="b1234-abcdef",
        chat_template=_DATELESS,
        n_ctx=262144,
        n_slots=1,
        today_utc="2026-08-19",
    )
    a = jerv_prime_fingerprint(server_args=("-fa", "1", "--swa-full"), **fixed)  # type: ignore[arg-type]
    b = jerv_prime_fingerprint(server_args=("--swa-full", "-fa", "1"), **fixed)  # type: ignore[arg-type]
    assert a != b
    # A list and a tuple of the same flags are the same launch line, though — the callers'
    # container type is an implementation detail and must not rotate a ~2 GB file.
    assert jerv_prime_fingerprint(server_args=["-fa", "1"], **fixed) == jerv_prime_fingerprint(  # type: ignore[arg-type]
        server_args=("-fa", "1"),
        **fixed,  # type: ignore[arg-type]
    )


def test_an_unreadable_template_keeps_rotating() -> None:
    """A /props hiccup gives an empty template. The conservative direction is to keep the date:
    a needless daily rotation costs one file, a missed one costs a prefix that stops matching."""
    assert template_renders_date("")
    assert not template_renders_date(_DATELESS)
    assert template_renders_date(_DATED)
    # Matched case-insensitively, so a template writing the label itself is caught.
    assert template_renders_date("System: CURRENT DATE: 2026-08-19")
