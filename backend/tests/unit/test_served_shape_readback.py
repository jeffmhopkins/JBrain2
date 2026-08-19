"""Reading the served `(window, slots)` back out of the config we wrote.

This exists because the llama.cpp upgrade smoketest had no way to know what the gateway
actually serves. It runs under `docker compose run --rm --no-deps` during an update, so it
cannot read the operator's `-c` override from the settings table, and it therefore sized
its pre-flight off the CATALOG window while llama-swap served the operator's:

    window used to project     projected   ceiling   observed   verdict
    32768  (catalog default)        1.57      3.57       3.78   ABORT
    262144 (actually served)        2.45      4.45       3.78   pass

Same model, same build, same memory — and that abort rolled llama.cpp back to its pinned
base. Reading the config closes the gap without a database, and cannot drift from the
served command because it IS the served command.
"""

import pathlib

from jbrain.llm import llama_swap_config

_TINY, _MID = "qwen3.5-0.8b", "qwen3.5-4b"
_TINY_SERVED, _MID_SERVED = "qwen3.5-0.8b", "qwen3.5-4b"
_TINY_WINDOW = 32768


def _manifest() -> list[dict[str, object]]:
    return [
        {
            "id": _TINY,
            "served_model": _TINY_SERVED,
            "gguf_include": "*Q8_0*.gguf",
            "mmproj_include": None,
            "context_window": _TINY_WINDOW,
            "recommended": False,
        },
        {
            "id": _MID,
            "served_model": _MID_SERVED,
            "gguf_include": "*Q8_0*.gguf",
            "mmproj_include": None,
            "context_window": 32768,
            "recommended": False,
        },
    ]


def _write(tmp_path: pathlib.Path, *, windows=None, slots=None) -> None:
    for entry in _manifest():
        d = tmp_path / str(entry["id"])
        d.mkdir(parents=True, exist_ok=True)
        (d / "model-Q8_0.gguf").write_bytes(b"\0")
    llama_swap_config.write(str(tmp_path), _manifest(), windows=windows or {}, slots=slots or {})


def test_the_catalog_default_round_trips(tmp_path: pathlib.Path) -> None:
    _write(tmp_path)
    shapes = llama_swap_config.served_shape_from_config(str(tmp_path))
    assert shapes[_TINY_SERVED] == (_TINY_WINDOW, 1)


def test_an_operator_window_override_is_what_comes_back(tmp_path: pathlib.Path) -> None:
    """The whole point: the smoketest must see 262144, not the catalog's 32768."""
    _write(tmp_path, windows={_TINY: 262144})
    shapes = llama_swap_config.served_shape_from_config(str(tmp_path))
    assert shapes[_TINY_SERVED] == (262144, 1)


def test_slots_divide_the_total_kv_cells(tmp_path: pathlib.Path) -> None:
    """`-c` is TOTAL cells across slots — llama-server divides it evenly by `-np` — so a
    reader that returned `-c` verbatim would double the per-slot window at 2 slots and
    make every projection wrong in the safe-looking direction."""
    _write(tmp_path, windows={_MID: 65536}, slots={_MID: 2})
    shapes = llama_swap_config.served_shape_from_config(str(tmp_path))
    assert shapes[_MID_SERVED] == (65536, 2)


def test_a_missing_config_falls_back_rather_than_raising(tmp_path: pathlib.Path) -> None:
    """Best-effort by design: a box mid-provision has no config yet, and failing the
    smoketest on that would roll back a llama.cpp build for the wrong reason."""
    assert llama_swap_config.served_shape_from_config(str(tmp_path)) == {}


def test_a_corrupt_config_falls_back_rather_than_raising(tmp_path: pathlib.Path) -> None:
    (tmp_path / "llama-swap.yaml").write_text("models: [this is not: valid: yaml")
    assert llama_swap_config.served_shape_from_config(str(tmp_path)) == {}
