"""Parsing llama.cpp's own account of what a load cost.

The catalog's per-model KV figures were wrong for two entries by 1.4 and >5.5 GiB, which
is what aborted a healthy load and rolled back a llama.cpp upgrade. llama.cpp had been
printing the correct numbers at every load the whole time — into a log buffer nothing
read. These pin the parse so the divergence is a logged number, not an afternoon on a
live box.
"""

from jbrain.llm.memory_report import catalog_divergence, parse_memory_report

# The form the CURRENT pinned build prints. Realistic spacing and interleaving, because a
# parser tested only against tidy input is a parser tested against nothing.
_BUFFERS = """
llama_model_loader: loaded meta data with 30 key-value pairs
load_tensors: offloading 36 repeating layers to GPU
load_tensors:      Vulkan0 model buffer size =  4402.34 MiB
load_tensors:          CPU model buffer size =   315.30 MiB
llama_context: n_ctx = 262144
llama_kv_cache_unified:    Vulkan0 KV buffer size =  8144.00 MiB
llama_context:      Vulkan0 compute buffer size =   560.02 MiB
llama_context:          CPU compute buffer size =    24.01 MiB
main: server is listening on 127.0.0.1:9110
"""

# The newer `llama_memory_breakdown_print` table, whose `unaccounted` column is the
# measured replacement for our hand-picked RUNTIME_OVERHEAD_GB.
_TABLE = """
| memory breakdown [MiB] | total   free     self   model   context   compute    unaccounted |
|   - Vulkan0 (Radeon)   | 126976 = 12000 + (110976 = 100000 +   8144 +     560) +      3945 |
|   - Host               |                            2048 =   2048 +      0 +       0       |
"""


def test_the_per_buffer_form_is_summed_across_devices() -> None:
    report = parse_memory_report(_BUFFERS)
    assert report is not None
    # Vulkan0 + CPU, because the budget this feeds is the whole pool.
    assert report.model_gb == round((4402.34 + 315.30) / 1024, 2)
    assert report.kv_gb == round(8144.00 / 1024, 2)
    assert report.compute_gb == round((560.02 + 24.01) / 1024, 2)
    assert report.unaccounted_gb is None, "the old form has no unaccounted column"


def test_the_breakdown_table_wins_when_both_are_present() -> None:
    """A build that prints both is the newer one, and its table is authoritative — it
    carries `unaccounted`, which the per-buffer lines cannot give us."""
    report = parse_memory_report(_BUFFERS + _TABLE)
    assert report is not None
    assert report.unaccounted_gb == round(3945 / 1024, 2)
    assert report.model_gb == round(100000 / 1024, 2)


def test_only_the_most_recent_load_is_counted() -> None:
    """The gateway log is a ring buffer across many loads. Summing all of them would
    report the box's whole history as one model's footprint."""
    first = _BUFFERS.replace("4402.34", "1000.00").replace("8144.00", "2000.00")
    report = parse_memory_report(first + _BUFFERS)
    assert report is not None
    assert report.kv_gb == round(8144.00 / 1024, 2), "an earlier load leaked into the total"


def test_a_build_that_prints_nothing_reports_none() -> None:
    """None, not a zeroed report: a caller must not be able to mistake "this build does
    not print it" for "this model cost nothing" — that reads as infinite headroom."""
    assert parse_memory_report("[INFO] GET /running 200\n[INFO] GET /health 200\n") is None


def test_divergence_is_positive_when_the_catalog_is_light() -> None:
    """The sign matters. Light is the direction that costs a freeze, because admission
    reserved less than the load takes — which is exactly what happened to qwen3.5-4b."""
    report = parse_memory_report(_BUFFERS)
    assert report is not None
    assert catalog_divergence(report, predicted_gb=7.25) > 0
    assert catalog_divergence(report, predicted_gb=report.total_gb) == 0.0


def test_the_real_qwen35_4b_numbers_reproduce_the_bug() -> None:
    """The measurement that started this: the catalog projected 7.25 GiB and the load was
    aborted at >=12.8. A report of that shape must show the catalog light by >5 GiB."""
    report = parse_memory_report(_BUFFERS)
    assert report is not None
    assert report.total_gb > 12.0, report
    assert catalog_divergence(report, predicted_gb=7.25) > 5.0


def test_sse_framed_lines_still_parse() -> None:
    """llama-swap serves `/logs/stream/*` as Server-Sent Events, so each line arrives as
    `data: <original>`.

    Unframed, the regex reads `data` as the emitting subsystem and then fails to find the
    buffer kind — which is the leading explanation for a 3772-byte capture from the live
    box parsing to nothing on 2026-08-19. Unconfirmed at the time of writing, hence
    `footprint_unparsed` logging a sample rather than this being assumed correct."""
    framed = "\n".join(f"data: {line}" for line in _BUFFERS.strip().splitlines())
    report = parse_memory_report(framed)
    assert report is not None, "SSE framing defeated the parse"
    assert report.kv_gb == round(8144.00 / 1024, 2)


def test_unframed_lines_are_untouched_by_the_stripper() -> None:
    """A build that streams raw must not be broken by the SSE handling."""
    assert parse_memory_report(_BUFFERS) == parse_memory_report(
        "\n".join(f"data: {line}" for line in _BUFFERS.strip().splitlines())
    )


def test_a_colon_in_the_payload_is_not_mistaken_for_framing() -> None:
    """Only the SSE field names are stripped, and only at line start — a device or
    subsystem name containing a colon must survive."""
    assert parse_memory_report("data: load_tensors: Vulkan0 model buffer size = 1024.00 MiB")


# The ACTUAL format the box emits, captured from `footprint_unparsed` on 2026-08-19.
# Every line carries llama.cpp's structured-logger prefix: <elapsed> <LEVEL> <subsystem>.
_REAL = """
0.00.184.502 W DEPRECATED: --mmap and --no-mmap are deprecated. use --load-mode mmap instead
0.00.184.891 I cmn  common_param: common_params_print_info: verbosity = 3
0.00.186.453 W srv  llama_server: -----------------
0.01.203.118 I ggml load_tensors: Vulkan0 model buffer size =  4402.34 MiB
0.01.203.402 I ggml load_tensors:     CPU model buffer size =   315.30 MiB
0.02.881.007 I ctx  llama_kv_cache_unified: Vulkan0 KV buffer size =  8144.00 MiB
0.02.999.240 I ctx  llama_context: Vulkan0 compute buffer size =   560.02 MiB
0.03.001.115 I srv  main: server is listening on 127.0.0.1:9110
"""


def test_the_format_the_box_actually_emits_parses() -> None:
    """Built from `footprint_unparsed`'s sample rather than from a guess.

    Two earlier hypotheses were wrong: a separate `/logs/upstream` buffer (that path 404s)
    and SSE `data:` framing (the stream is not SSE). The truth is llama.cpp's structured
    logger prefixing every line with `<elapsed> <LEVEL> <subsystem>`, which defeated a
    pattern anchored at line start — so a 7356-byte capture of a real load banner parsed
    to nothing."""
    report = parse_memory_report(_REAL)
    assert report is not None, "the real format still does not parse"
    assert report.model_gb == round((4402.34 + 315.30) / 1024, 2)
    assert report.kv_gb == round(8144.00 / 1024, 2)
    assert report.compute_gb == round((560.02 + 24.01 - 24.01) / 1024, 2)


def test_prose_mentioning_a_buffer_is_not_mistaken_for_a_measurement() -> None:
    """Unanchoring the pattern widens what it can match, so it must not fire on a log line
    that merely talks about buffers."""
    assert parse_memory_report("0.00.1 W srv llama_server: compute buffer size unknown") is None


def test_a_deprecation_warning_alone_is_not_a_report() -> None:
    """The box's capture opens with several warning lines before any real figure; a report
    built from those would be worse than none."""
    assert parse_memory_report(_REAL.split("load_tensors")[0]) is None
