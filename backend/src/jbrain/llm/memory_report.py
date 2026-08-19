"""What llama.cpp says a model ACTUALLY cost, parsed from its own load output.

The catalog predicts a model's footprint as `size + kv + overhead` from hand-maintained
per-model numbers. On 2026-08-19 two of those numbers were found to be badly wrong —
`qwen3.5-0.8b` measured 3.82 GiB against a 2.45 prediction, and `qwen3.5-4b` at least
12.8 against 7.25 — which is what made a healthy llama.cpp build fail its smoke test and
get rolled back. Nothing detected the drift, and the fix took a live box, a debug token
and an afternoon.

It should not have. llama.cpp prints the true per-buffer sizes on every single load. They
went into llama-swap's upstream log buffer, which nothing read, because the log endpoint
fetched the proxy buffer instead (see `LocalGatewayClient.tail_logs`). This module reads
them, so a wrong catalog entry is a logged divergence rather than an afternoon.

Deliberately parses what the PINNED build already emits rather than requiring the newer
`--fit`/`no_alloc` estimator: measuring after a load works today and needs no upgrade.
`no_alloc` is strictly better when available — it answers BEFORE committing memory — and
the newer table form is parsed here too, so adopting it needs no change on this side.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_MIB = 1024 * 1024
_BYTES_PER_GIB = 1024**3

# The per-buffer figures, matched ANYWHERE in a line rather than anchored to its start.
#
# Anchoring was wrong, and the live box said so. Current llama.cpp prefixes every line with
# its structured logger — `<elapsed> <LEVEL> <subsystem> <component>: <message>` — so a real
# line reads:
#
#   0.01.234.567 I ggml  load_tensors: Vulkan0 model buffer size =  4402.34 MiB
#
# An anchored `^who:` pattern matches none of that, which is how a 7356-byte capture of a
# perfectly good load banner parsed to nothing. Two earlier guesses at the cause (a separate
# `/logs/upstream` buffer, then SSE `data:` framing) were both wrong; this one is built from
# `footprint_unparsed`'s sample of the actual bytes.
#
# Matching only the part that carries meaning also makes this robust to the prefix changing
# again — the emitting subsystem and device name were never needed.
_BUFFER = re.compile(
    r"(?P<kind>model|KV|compute)\s+buffer size\s*=\s*(?P<mib>[\d.]+)\s*MiB",
    re.I,
)

# The newer `llama_memory_breakdown_print` table, whose `unaccounted` column is the
# measured replacement for our hand-picked RUNTIME_OVERHEAD_GB:
#   | - Vulkan0 (Radeon) | 124000 = 900 + (68000 = 60000 + 6000 + 2000) + 3945 |
_BREAKDOWN = re.compile(
    r"^\s*\|\s*-?\s*(?P<dev>[\w\d]+)[^|]*\|\s*(?P<total>\d+)\s*=\s*(?P<free>\d+)\s*\+\s*\("
    r"(?P<self_>\d+)\s*=\s*(?P<model>\d+)\s*\+\s*(?P<context>\d+)\s*\+\s*(?P<compute>\d+)\)"
    r"\s*\+\s*(?P<unaccounted>\d+)",
    re.M,
)


@dataclass(frozen=True)
class MemoryReport:
    """One load's measured device memory, in GiB. `unaccounted` is only ever populated by
    the newer breakdown table; it is the engine's own figure for everything that is not
    weights, KV or compute — i.e. exactly what `RUNTIME_OVERHEAD_GB` guesses at."""

    model_gb: float
    kv_gb: float
    compute_gb: float
    unaccounted_gb: float | None = None

    @property
    def total_gb(self) -> float:
        return round(self.model_gb + self.kv_gb + self.compute_gb + (self.unaccounted_gb or 0.0), 2)


# llama-swap serves /logs/stream/* as Server-Sent Events, so each line arrives framed as
# `data: <original line>`. Unframed input passes through untouched, so this is safe against
# a build that streams raw — which matters, because the framing has not been confirmed and
# is the current best explanation for a 3772-byte capture parsing to nothing.
_SSE = re.compile(r"^(?:data|event|id|retry)\s*:\s?", re.I)


def strip_sse_framing(log_text: str) -> str:
    """Drop SSE field prefixes so the payload can be parsed as ordinary log lines."""
    return "\n".join(_SSE.sub("", line) for line in log_text.splitlines())


def parse_memory_report(log_text: str) -> MemoryReport | None:
    """The LAST complete report in `log_text`, or None if the build printed none.

    Last rather than first: the log is a ring buffer across many loads, and the reading
    that matters is the model that just loaded. Returns None rather than a zeroed report
    when nothing matched, so a caller cannot mistake "this build does not print it" for
    "this model cost nothing"."""
    log_text = strip_sse_framing(log_text)
    table = _parse_breakdown(log_text)
    if table is not None:
        return table
    return _parse_buffers(log_text)


def _parse_breakdown(log_text: str) -> MemoryReport | None:
    matches = list(_BREAKDOWN.finditer(log_text))
    if not matches:
        return None
    # Device rows only; the table also carries a `Host` row that is not device memory.
    last = matches[-1]
    return MemoryReport(
        model_gb=round(int(last["model"]) * _MIB / _BYTES_PER_GIB, 2),
        kv_gb=round(int(last["context"]) * _MIB / _BYTES_PER_GIB, 2),
        compute_gb=round(int(last["compute"]) * _MIB / _BYTES_PER_GIB, 2),
        unaccounted_gb=round(int(last["unaccounted"]) * _MIB / _BYTES_PER_GIB, 2),
    )


def _parse_buffers(log_text: str) -> MemoryReport | None:
    """Sum the per-buffer lines of the most recent load.

    A load prints its buffers once per device, and a multi-GPU box prints several — they
    are summed, because the budget this feeds is the whole device pool. Only the trailing
    run is used: everything after the last `model buffer size` line, which is the first
    thing a fresh load emits."""
    # Group by LOAD, not by line. One load prints a `model buffer size` line per device
    # (Vulkan0 and CPU on this box), so slicing from the last such line would drop the
    # first device's weights — silently, and in the direction that under-reports.
    # A new load starts at a `model` line whose predecessor was not also `model`.
    loads: list[list[re.Match[str]]] = []
    previous: str | None = None
    for match in _BUFFER.finditer(log_text):
        kind = match["kind"].lower()
        if kind == "model" and previous != "model":
            loads.append([])
        if loads:
            loads[-1].append(match)
        previous = kind
    if not loads:
        return None
    totals = {"model": 0.0, "kv": 0.0, "compute": 0.0}
    for match in loads[-1]:
        totals[match["kind"].lower()] += float(match["mib"])
    return MemoryReport(
        model_gb=round(totals["model"] * _MIB / _BYTES_PER_GIB, 2),
        kv_gb=round(totals["kv"] * _MIB / _BYTES_PER_GIB, 2),
        compute_gb=round(totals["compute"] * _MIB / _BYTES_PER_GIB, 2),
    )


def catalog_divergence(measured: MemoryReport, predicted_gb: float) -> float:
    """How far the catalog's prediction is from what the engine actually allocated, in GiB.

    Positive means the catalog is LIGHT — the direction that costs a freeze, because the
    admission check reserved less than the load takes."""
    return round(measured.total_gb - predicted_gb, 2)
