"""Token accounting: the fire-and-forget recorder contract and price math."""

from datetime import date

from jbrain.llm import FakeLlmClient, LlmRouter, LlmUsage
from jbrain.usage import UsageRow, cost_usd, summarize_usage

PRICES = {"xai:grok-4.3": {"input_per_m": 1.25, "output_per_m": 2.50}}

TODAY = date(2026, 6, 10)


class RecordingRecorder:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    async def record(self, *, task: str, provider: str, model: str, usage: LlmUsage) -> None:
        self.records.append({"task": task, "provider": provider, "model": model, "usage": usage})


class ExplodingRecorder:
    async def record(self, *, task: str, provider: str, model: str, usage: LlmUsage) -> None:
        raise RuntimeError("database is on fire")


def router(fake: FakeLlmClient, recorder: object) -> LlmRouter:
    return LlmRouter({"xai": fake}, {"note.extract": ("xai", "grok-4.3")}, recorder=recorder)  # type: ignore[arg-type]


async def test_recorder_receives_task_provider_model_usage() -> None:
    recorder = RecordingRecorder()
    await router(FakeLlmClient(["ok"]), recorder).complete(
        "note.extract", system="s", user_text="u"
    )
    assert recorder.records == [
        {
            "task": "note.extract",
            "provider": "xai",
            "model": "grok-4.3",
            "usage": LlmUsage(1, 1),
        }
    ]


async def test_raising_recorder_never_fails_the_call() -> None:
    result = await router(FakeLlmClient(["fine"]), ExplodingRecorder()).complete(
        "note.extract", system="s", user_text="u"
    )
    assert result.text == "fine"


async def test_reask_tokens_are_recorded_too() -> None:
    recorder = RecordingRecorder()
    fake = FakeLlmClient(["not json", '{"ok": true}'])
    await router(fake, recorder).complete(
        "note.extract", system="s", user_text="u", json_schema={"type": "object"}
    )
    assert len(recorder.records) == 2  # the ledger tracks what was billed


async def test_no_recorder_is_fine() -> None:
    result = await LlmRouter(
        {"xai": FakeLlmClient(["ok"])}, {"note.extract": ("xai", "grok-4.3")}
    ).complete("note.extract", system="s", user_text="u")
    assert result.text == "ok"


def test_spec_exposes_task_routing() -> None:
    r = LlmRouter({}, {"note.extract": ("xai", "grok-4.3")})
    assert r.spec("note.extract") == ("xai", "grok-4.3")


# --- price math --------------------------------------------------------------


def test_cost_usd_known_model() -> None:
    # 1M in + 1M out at grok-4.3 rates = $3.75
    assert cost_usd("xai", "grok-4.3", 1_000_000, 1_000_000, PRICES) == 3.75


def test_cost_usd_unknown_model_is_none_never_guessed() -> None:
    assert cost_usd("anthropic", "claude-sonnet-4-6", 1_000_000, 0, PRICES) is None
    assert cost_usd("xai", "grok-4.3", 1, 1, {"xai:grok-4.3": {"input_per_m": 1.0}}) is None


def row(
    day: date = TODAY,
    task: str = "note.extract",
    provider: str = "xai",
    model: str = "grok-4.3",
    inp: int = 1000,
    out: int = 500,
) -> UsageRow:
    return UsageRow(
        day=day, task=task, provider=provider, model=model, input_tokens=inp, output_tokens=out
    )


def test_summary_buckets_today_month_by_task_days() -> None:
    rows = [
        row(inp=4_000_000, out=2_000_000),
        row(day=date(2026, 6, 3), task="entity.disambiguate", inp=1_000_000, out=0),
        row(day=date(2026, 5, 20), inp=999, out=999),  # last month, within 30d
    ]
    summary = summarize_usage(rows, PRICES, TODAY)

    assert summary["today"] == {
        "input_tokens": 4_000_000,
        "output_tokens": 2_000_000,
        "cost_usd": 10.0,
    }
    assert summary["month"]["input_tokens"] == 5_000_000
    assert summary["month"]["cost_usd"] == 11.25

    assert [t["task"] for t in summary["by_task"]] == ["note.extract", "entity.disambiguate"]
    assert summary["by_task"][1]["cost_usd"] == 1.25

    assert [d["date"] for d in summary["days"]] == ["2026-05-20", "2026-06-03", "2026-06-10"]


def test_unknown_models_count_tokens_but_not_cost() -> None:
    rows = [
        row(inp=1_000_000, out=0),
        row(model="mystery-model", inp=2_000_000, out=0),
    ]
    today = summarize_usage(rows, PRICES, TODAY)["today"]
    assert today["input_tokens"] == 3_000_000  # tokens are the ground truth
    assert today["cost_usd"] == 1.25  # only the priceable model contributes


def test_cost_null_when_nothing_priceable() -> None:
    rows = [row(model="mystery-model")]
    assert summarize_usage(rows, PRICES, TODAY)["today"]["cost_usd"] is None


def test_empty_usage_is_zeros_with_null_cost() -> None:
    summary = summarize_usage([], PRICES, TODAY)
    assert summary["today"] == {"input_tokens": 0, "output_tokens": 0, "cost_usd": None}
    assert summary["days"] == [] and summary["by_task"] == []


def test_days_window_excludes_older_than_thirty_days() -> None:
    rows = [row(day=date(2026, 5, 1)), row(day=date(2026, 5, 12))]
    summary = summarize_usage(rows, PRICES, TODAY)
    assert [d["date"] for d in summary["days"]] == ["2026-05-12"]
