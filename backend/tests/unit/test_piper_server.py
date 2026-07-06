"""The warm-model piper TTS server for the `tts-stt` speech service
(deploy/tts-stt/piper_server.py).

piper_server imports `piper` at module load (the real package lives only in the tts-stt
image), so these tests stub it with a fake PiperVoice before loading the module. They cover
the warm cache (a voice's model loads ONCE across many renders — the whole point of the
service), the curated multi-speaker resolution (libritts_r #3922 -> speaker 0), the
render-failure logging, and the tts_debug latch.
"""

import importlib.util
import io
import json
import sys
import types
import wave
from pathlib import Path

import pytest

_DEPLOY = Path(__file__).resolve().parents[3] / "deploy"
_SERVER_PATH = _DEPLOY / "tts-stt" / "piper_server.py"
_DOCKERFILE = _DEPLOY / "Dockerfile.tts-stt"
_INSTALL_SCRIPT = _DEPLOY / "tts-stt" / "install-tts.sh"


def _short_name(stem: str) -> str:
    """A voice stem's model name as the fetch loops key it — "en_US-amy-medium" -> "amy"."""
    return stem.removeprefix("en_US-").removesuffix("-medium")


class _FakeVoice:
    """Stand-in for piper.voice.PiperVoice: records loads + synth calls, writes a tiny WAV."""

    loads: list[str] = []

    def __init__(self, model_path: str) -> None:
        self.model_path = model_path

    @classmethod
    def load(cls, model_path: str, config_path: str | None = None) -> "_FakeVoice":
        cls.loads.append(str(model_path))
        return cls(str(model_path))

    def synthesize_wav(self, text: str, wav_file, syn_config=None) -> None:  # type: ignore[no-untyped-def]
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(22050)
        wav_file.writeframes(b"\x00\x00" * 100)


class _FakeSynthesisConfig:
    def __init__(self, speaker_id: int | None = None, **_: object) -> None:
        self.speaker_id = speaker_id


def _load_server() -> types.ModuleType:
    _FakeVoice.loads = []
    fake_piper = types.ModuleType("piper")
    fake_piper.PiperVoice = _FakeVoice  # type: ignore[attr-defined]
    fake_piper.SynthesisConfig = _FakeSynthesisConfig  # type: ignore[attr-defined]
    sys.modules["piper"] = fake_piper
    spec = importlib.util.spec_from_file_location("piper_server", _SERVER_PATH)
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
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    mod = _load_server()
    voices = tmp_path / "voices"
    voices.mkdir()
    _write_voice(voices, "en_US-amy-medium")
    _write_voice(voices, "en_US-libritts_r-medium", {"3922": 0, "1234": 1})
    monkeypatch.setattr(mod, "PIPER_VOICES_DIR", voices)
    monkeypatch.setattr(mod, "PIPER_BAKED_VOICES_DIR", tmp_path / "baked")  # absent
    return mod


def _wav_frames(data: bytes) -> int:
    with wave.open(io.BytesIO(data), "rb") as w:
        return w.getnframes()


def test_voices_expose_curated_speaker(server: types.ModuleType) -> None:
    assert server.piper_voices() == ["en_US-amy-medium", "en_US-libritts_r-medium#3922"]


def test_resolve_maps_curated_id_to_speaker_index(server: types.ModuleType) -> None:
    model, speaker = server._resolve_voice("en_US-libritts_r-medium#3922")
    assert model.stem == "en_US-libritts_r-medium"
    assert speaker == 0  # LibriTTS 3922 is piper index 0
    _, amy_speaker = server._resolve_voice("en_US-amy-medium")
    assert amy_speaker is None  # single-speaker -> no speaker id


def test_resolve_accepts_any_valid_speaker_not_just_curated(server: types.ModuleType) -> None:
    # The voice explorer auditions every speaker, so a NON-curated but valid speaker id must
    # resolve to its real index (1234 -> piper index 1 here), not fall back to a default.
    model, speaker = server._resolve_voice("en_US-libritts_r-medium#1234")
    assert model.stem == "en_US-libritts_r-medium"
    assert speaker == 1


def test_resolve_unknown_speaker_falls_back_no_traversal(server: types.ModuleType) -> None:
    # An id with a speaker the model doesn't have must NOT pass a bogus index to piper — it
    # falls back to the first installed voice. (The stem is still a real installed model.)
    model, speaker = server._resolve_voice("en_US-libritts_r-medium#nope")
    assert model.stem == "en_US-amy-medium"  # first installed voice, sorted
    assert speaker is None


def test_speakers_roster_ordered_by_index_multispeaker_only(server: types.ModuleType) -> None:
    # The explorer roster: names ordered by piper index, and single-speaker models excluded.
    assert server.piper_speakers() == {"en_US-libritts_r-medium": ["3922", "1234"]}


def test_warm_cache_loads_each_model_once(server: types.ModuleType) -> None:
    # The point of the service: repeated renders of a voice REUSE the resident model.
    for _ in range(3):
        assert server.tts_wav("hello", "en_US-libritts_r-medium#3922", lead_ms=0) is not None
    assert (
        _FakeVoice.loads.count(str(server.PIPER_VOICES_DIR / "en_US-libritts_r-medium.onnx")) == 1
    )
    # A different voice loads its own model, still once across repeats.
    for _ in range(2):
        server.tts_wav("hi", "en_US-amy-medium", lead_ms=0)
    assert len(_FakeVoice.loads) == 2  # libritts once + amy once


def test_tts_wav_renders_a_wav(server: types.ModuleType) -> None:
    out = server.tts_wav("hello there", "en_US-amy-medium", lead_ms=0)
    assert out is not None
    assert _wav_frames(out) == 100  # the fake voice writes 100 frames


def test_tts_wav_pads_lead_silence(server: types.ModuleType) -> None:
    plain = server.tts_wav("hi", "en_US-amy-medium", lead_ms=0)
    padded = server.tts_wav("hi", "en_US-amy-medium", lead_ms=500)
    assert plain and padded
    assert _wav_frames(padded) > _wav_frames(plain)  # 500ms of silence prepended


def test_render_failure_is_logged_not_silent(
    server: types.ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(_self, _text, _wav, syn_config=None):  # type: ignore[no-untyped-def]
        raise RuntimeError("onnx exploded")

    monkeypatch.setattr(_FakeVoice, "synthesize_wav", boom)
    server._voice_cache.clear()
    assert server.tts_wav("hello", "en_US-amy-medium", lead_ms=0) is None
    err = capsys.readouterr().err
    assert "render failed" in err
    assert "onnx exploded" in err


def test_tts_debug_trace_when_latched(
    server: types.ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    server._tts_debug[0] = True
    server.tts_wav("hello", "en_US-libritts_r-medium#3922", lead_ms=0)
    err = capsys.readouterr().err
    assert "rendering 'en_US-libritts_r-medium#3922'" in err
    assert "speaker=0" in err


def test_docker_image_bakes_every_curated_multispeaker_model(server: types.ModuleType) -> None:
    # A curated speaker only reaches the picker if its MODEL is installed — and production
    # installs are the BAKED tts-stt image. Guard that Dockerfile.tts-stt's baked tuple stays
    # in step with CURATED_SPEAKERS so a new curated model can't be exposed yet missing.
    dockerfile = _DOCKERFILE.read_text()
    for stem in server.CURATED_SPEAKERS:
        assert _short_name(stem) in dockerfile, (
            f"{stem} is curated in piper_server.py but not baked into Dockerfile.tts-stt"
        )
    assert "'joe'" in dockerfile and "'amy'" in dockerfile


def test_install_script_installs_every_curated_model(server: types.ModuleType) -> None:
    # The run-on-host path (install-tts.sh MODELS) must carry the curated models too.
    script = _INSTALL_SCRIPT.read_text()
    for stem in server.CURATED_SPEAKERS:
        assert stem in script, f"{stem} curated in piper_server.py but missing from install-tts.sh"
