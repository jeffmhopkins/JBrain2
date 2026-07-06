"""The server-brain's multi-speaker piper voice resolution (deploy/server-brain/serve.py).

serve.py is a stdlib-only script (not a package), so it's loaded from its path. These
cover the curated multi-speaker exposure (libritts_r 3922) and that the resolved speaker
index is passed to piper as --speaker, with single-speaker voices unaffected.
"""

import importlib.util
import json
import types
from pathlib import Path

import pytest

_DEPLOY = Path(__file__).resolve().parents[3] / "deploy"
_SERVE_PATH = _DEPLOY / "server-brain" / "serve.py"
_DOCKERFILE = _DEPLOY / "Dockerfile.server-brain"
_INSTALL_SCRIPT = _DEPLOY / "server-brain" / "install-tts.sh"


def _short_name(stem: str) -> str:
    """A voice stem's model name as the fetch loops key it — "en_US-amy-medium" -> "amy"."""
    return stem.removeprefix("en_US-").removesuffix("-medium")


def _load_serve() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("server_brain_serve", _SERVE_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_voice(dir_: Path, stem: str, speaker_id_map: dict | None = None) -> None:
    (dir_ / f"{stem}.onnx").write_bytes(b"onnx")
    meta: dict = {"audio": {"sample_rate": 22050}}
    if speaker_id_map is not None:
        meta["num_speakers"] = len(speaker_id_map)
        meta["speaker_id_map"] = speaker_id_map
    (dir_ / f"{stem}.onnx.json").write_text(json.dumps(meta))


@pytest.fixture
def serve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    mod = _load_serve()
    voices = tmp_path / "voices"
    voices.mkdir()
    # A single-speaker default, the curated multi-speaker model, and an uncurated
    # multi-speaker model (to exercise the default-speaker fallback).
    _write_voice(voices, "en_US-amy-medium")
    _write_voice(voices, "en_US-libritts_r-medium", {"3922": 0, "1234": 1})
    _write_voice(voices, "en_US-other-medium", {"spk": 0})
    monkeypatch.setattr(mod, "PIPER_VOICES_DIR", voices)
    monkeypatch.setattr(mod, "PIPER_BAKED_VOICES_DIR", tmp_path / "baked")  # absent
    return mod


def test_speaker_map_reads_only_multi_speaker_models(serve: types.ModuleType) -> None:
    models = serve._voice_models()
    assert serve._speaker_map(models["en_US-amy-medium"]) == {}
    assert serve._speaker_map(models["en_US-libritts_r-medium"]) == {"3922": 0, "1234": 1}


def test_voices_exposes_curated_speaker_and_single_speakers(serve: types.ModuleType) -> None:
    ids = serve.piper_voices()
    # Single-speaker model -> its stem; curated multi-speaker -> only speaker 3922 (not
    # the uncurated 1234); uncurated multi-speaker -> its stem (default speaker).
    assert ids == [
        "en_US-amy-medium",
        "en_US-libritts_r-medium#3922",
        "en_US-other-medium",
    ]


def test_resolve_voice_maps_id_to_model_and_speaker_index(serve: types.ModuleType) -> None:
    _, amy_speaker = serve._resolve_voice("en_US-amy-medium")
    assert amy_speaker is None  # single-speaker -> no --speaker
    model, speaker = serve._resolve_voice("en_US-libritts_r-medium#3922")
    assert model.stem == "en_US-libritts_r-medium"
    assert speaker == 0  # LibriTTS 3922 is piper index 0
    _, other_speaker = serve._resolve_voice("en_US-other-medium")
    assert other_speaker == 0  # uncurated multi-speaker falls back to its default


def test_resolve_voice_falls_back_to_first_for_unknown_id(serve: types.ModuleType) -> None:
    model, speaker = serve._resolve_voice("does-not-exist")
    assert model.stem == "en_US-amy-medium"  # first voice, sorted
    assert speaker is None


def test_tts_wav_passes_speaker_for_a_multi_speaker_voice(
    serve: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object):  # type: ignore[no-untyped-def]
        calls.append(cmd)
        Path(cmd[cmd.index("--output_file") + 1]).write_bytes(b"RIFFfakewav")
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(serve.shutil, "which", lambda _bin: "/usr/bin/piper")
    monkeypatch.setattr(serve.subprocess, "run", fake_run)

    out = serve.tts_wav("hello", "en_US-libritts_r-medium#3922", lead_ms=0)
    assert out == b"RIFFfakewav"
    assert "--speaker" in calls[0]
    assert calls[0][calls[0].index("--speaker") + 1] == "0"

    # A single-speaker voice carries no --speaker.
    serve.tts_wav("hi", "en_US-amy-medium", lead_ms=0)
    assert "--speaker" not in calls[1]


def test_tts_wav_none_without_piper(
    serve: types.ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(serve.shutil, "which", lambda _bin: None)
    assert serve.tts_wav("hello", "en_US-amy-medium") is None
    # Even the "no piper" path is logged, so a missing binary isn't a silent native fall back.
    assert "render failed" in capsys.readouterr().err


def test_tts_wav_verbose_trace_when_debug(
    serve: types.ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # With BRAIN_TTS_DEBUG on, a SUCCESSFUL render traces the resolved voice + speaker, so
    # an operator can confirm the box actually rendered the requested voice (not a fallback).
    def fake_run(cmd: list[str], **kwargs: object):  # type: ignore[no-untyped-def]
        Path(cmd[cmd.index("--output_file") + 1]).write_bytes(b"RIFFfakewav")
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(serve, "TTS_DEBUG", True)
    monkeypatch.setattr(serve.shutil, "which", lambda _bin: "/usr/bin/piper")
    monkeypatch.setattr(serve.subprocess, "run", fake_run)

    assert serve.tts_wav("hello", "en_US-libritts_r-medium#3922", lead_ms=0) == b"RIFFfakewav"
    err = capsys.readouterr().err
    assert "rendering 'en_US-libritts_r-medium#3922'" in err  # start trace: voice as received
    assert "speaker=0" in err  # resolved --speaker
    assert "rendered" in err  # completion trace with byte count


def test_tts_wav_render_failure_is_logged_not_silent(
    serve: types.ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A failed render must return None *and* name the voice + cause on stderr — otherwise the
    # reply silently degrades to the device's native voice and looks like the wrong speaker.
    def fake_run(cmd: list[str], **kwargs: object):  # type: ignore[no-untyped-def]
        raise serve.subprocess.CalledProcessError(1, cmd, stderr=b"onnx load failed")

    monkeypatch.setattr(serve.shutil, "which", lambda _bin: "/usr/bin/piper")
    monkeypatch.setattr(serve.subprocess, "run", fake_run)

    assert serve.tts_wav("hello", "en_US-libritts_r-medium#3922", lead_ms=0) is None
    err = capsys.readouterr().err
    assert "en_US-libritts_r-medium#3922" in err
    assert "onnx load failed" in err


def test_tts_wav_uses_configurable_timeout(
    serve: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The per-render cap is tunable so a heavy voice on a busy box can be given more time
    # instead of timing out into the native-voice fallback.
    monkeypatch.setattr(serve, "PIPER_TIMEOUT_S", 123.0)
    seen: dict[str, object] = {}

    def fake_run(cmd: list[str], **kwargs: object):  # type: ignore[no-untyped-def]
        seen["timeout"] = kwargs.get("timeout")
        Path(cmd[cmd.index("--output_file") + 1]).write_bytes(b"RIFFfakewav")
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(serve.shutil, "which", lambda _bin: "/usr/bin/piper")
    monkeypatch.setattr(serve.subprocess, "run", fake_run)

    serve.tts_wav("hello", "en_US-amy-medium", lead_ms=0)
    assert seen["timeout"] == 123.0


def test_docker_image_bakes_every_curated_multispeaker_model(serve: types.ModuleType) -> None:
    # A curated speaker (e.g. libritts_r 3922) only reaches the picker if its MODEL is
    # actually installed — and production installs are the BAKED image, not install-tts.sh.
    # Guard that the Dockerfile's baked-voices tuple stays in step with CURATED_SPEAKERS so a
    # new curated model can't be exposed by serve.py yet missing from the box.
    dockerfile = _DOCKERFILE.read_text()
    for stem in serve.CURATED_SPEAKERS:
        assert _short_name(stem) in dockerfile, (
            f"{stem} is curated in serve.py but not baked into Dockerfile.server-brain"
        )
    # The single-speaker defaults stay baked too.
    assert "'joe'" in dockerfile and "'amy'" in dockerfile


def test_install_script_installs_every_curated_model(serve: types.ModuleType) -> None:
    # The run-on-host path (install-tts.sh MODELS) must carry the curated models too, so a
    # dev box matches the baked image.
    script = _INSTALL_SCRIPT.read_text()
    for stem in serve.CURATED_SPEAKERS:
        assert stem in script, f"{stem} is curated in serve.py but missing from install-tts.sh"
