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
    texts: list[str] = []  # text as received by synth — proves the pre-dispatch normalization

    def __init__(self, model_path: str) -> None:
        self.model_path = model_path

    @classmethod
    def load(cls, model_path: str, config_path: str | None = None) -> "_FakeVoice":
        cls.loads.append(str(model_path))
        return cls(str(model_path))

    def synthesize_wav(self, text: str, wav_file, syn_config=None) -> None:  # type: ignore[no-untyped-def]
        type(self).texts.append(text)
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(22050)
        wav_file.writeframes(b"\x00\x00" * 100)


class _FakeSynthesisConfig:
    def __init__(self, speaker_id: int | None = None, **_: object) -> None:
        self.speaker_id = speaker_id


class _FakeStyle:
    """Minimal Kokoro voice-style-matrix stand-in: supports the weighted-average blending the
    server does (scalar `*` then `+`), so a blend's math is checkable without numpy."""

    def __init__(self, data: list[float]) -> None:
        self.data = list(data)

    def __mul__(self, w: float) -> "_FakeStyle":
        return _FakeStyle([x * w for x in self.data])

    __rmul__ = __mul__

    def __add__(self, other: "_FakeStyle") -> "_FakeStyle":
        return _FakeStyle([a + b for a, b in zip(self.data, other.data, strict=True)])


class _FakeKokoro:
    """Stand-in for kokoro_onnx.Kokoro: records loads + the last create() call, returns a
    numpy-free (list, rate) so the stdlib float->PCM path is exercised without pulling numpy into
    the backend venv."""

    loads: list[str] = []
    last_create: dict = {}
    # Deterministic per-voice style vectors so a blend's weighted average is exactly checkable.
    _STYLES = {"am_michael": [10.0, 20.0], "af_nicole": [30.0, 40.0]}

    def __init__(self, model_path: str, voices_path: str) -> None:
        type(self).loads.append(str(model_path))

    def get_voice_style(self, name: str) -> "_FakeStyle":
        return _FakeStyle(_FakeKokoro._STYLES.get(name, [0.0, 0.0]))

    def create(  # type: ignore[no-untyped-def]
        self, text, voice, speed=1.0, lang="en-us", is_phonemes=False, trim=True
    ):
        type(self).last_create = {
            "text": text,
            "voice": voice,
            "is_phonemes": is_phonemes,
            "speed": speed,
        }
        return [0.0, 0.5, -0.5, 1.0, -1.0] * 20, 24000  # 100 samples, the real engine's 24 kHz


class _FakeG2P:
    """Stand-in for misaki.en.G2P: records the text it phonemized, returns (phonemes, tokens)."""

    calls: list[str] = []

    def __init__(self, trf: bool = False, british: bool = False, fallback: object = None) -> None:
        pass

    def __call__(self, text: str):  # type: ignore[no-untyped-def]
        type(self).calls.append(text)
        return (f"PH[{text}]", [])


class _FakeEspeakFallback:
    def __init__(self, british: bool = False) -> None:
        pass


def _load_server() -> types.ModuleType:
    _FakeVoice.loads = []
    _FakeVoice.texts = []
    _FakeKokoro.loads = []
    _FakeKokoro.last_create = {}
    _FakeG2P.calls = []
    fake_piper = types.ModuleType("piper")
    fake_piper.PiperVoice = _FakeVoice  # type: ignore[attr-defined]
    fake_piper.SynthesisConfig = _FakeSynthesisConfig  # type: ignore[attr-defined]
    sys.modules["piper"] = fake_piper
    # Kokoro + misaki are imported lazily inside _load_kokoro / _load_g2p, so these fakes only
    # matter once a Kokoro voice actually renders — harmless for the piper-only tests.
    fake_kokoro = types.ModuleType("kokoro_onnx")
    fake_kokoro.Kokoro = _FakeKokoro  # type: ignore[attr-defined]
    sys.modules["kokoro_onnx"] = fake_kokoro
    fake_misaki = types.ModuleType("misaki")
    fake_en = types.ModuleType("misaki.en")
    fake_en.G2P = _FakeG2P  # type: ignore[attr-defined]
    fake_espeak = types.ModuleType("misaki.espeak")
    fake_espeak.EspeakFallback = _FakeEspeakFallback  # type: ignore[attr-defined]
    fake_misaki.en = fake_en  # type: ignore[attr-defined]
    fake_misaki.espeak = fake_espeak  # type: ignore[attr-defined]
    sys.modules["misaki"] = fake_misaki
    sys.modules["misaki.en"] = fake_en
    sys.modules["misaki.espeak"] = fake_espeak
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
    # Point Kokoro at an absent dir so these piper-only tests are hermetic regardless of a
    # real /opt/kokoro on the build host.
    monkeypatch.setattr(mod, "KOKORO_DIR", tmp_path / "kokoro_absent")
    return mod


@pytest.fixture
def kokoro_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """A server with the Kokoro weights present (fake files) alongside a single piper voice, so
    the Kokoro engine path lists + renders."""
    mod = _load_server()
    voices = tmp_path / "voices"
    voices.mkdir()
    _write_voice(voices, "en_US-amy-medium")
    monkeypatch.setattr(mod, "PIPER_VOICES_DIR", voices)
    monkeypatch.setattr(mod, "PIPER_BAKED_VOICES_DIR", tmp_path / "baked")
    kdir = tmp_path / "kokoro"
    kdir.mkdir()
    (kdir / mod.KOKORO_MODEL).write_bytes(b"onnx")
    (kdir / mod.KOKORO_VOICES_FILE).write_bytes(b"bin")
    monkeypatch.setattr(mod, "KOKORO_DIR", kdir)
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


# --- Kokoro engine -------------------------------------------------------------------------


def test_kokoro_voices_listed_after_piper_when_baked(kokoro_server: types.ModuleType) -> None:
    voices = kokoro_server.piper_voices()
    assert voices[0] == "en_US-amy-medium"  # piper voices first
    assert "kokoro-af_heart" in voices
    assert "kokoro-am_michael" in voices


def test_kokoro_voices_absent_without_weights(server: types.ModuleType) -> None:
    # The shared fixture points KOKORO_DIR at a missing dir — no Kokoro ids leak into the list.
    assert server.kokoro_voices() == []
    assert not any(v.startswith("kokoro-") for v in server.piper_voices())


def test_kokoro_renders_a_24khz_mono_wav(kokoro_server: types.ModuleType) -> None:
    out = kokoro_server.tts_wav("hello there", "kokoro-af_heart", lead_ms=0)
    assert out is not None
    with wave.open(io.BytesIO(out), "rb") as w:
        assert w.getframerate() == 24000  # Kokoro's native rate, not piper's 22050
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getnframes() == 100  # the fake engine returns 100 samples


def test_kokoro_warm_load_once(kokoro_server: types.ModuleType) -> None:
    for _ in range(3):
        assert kokoro_server.tts_wav("hi", "kokoro-am_michael", lead_ms=0) is not None
    assert len(_FakeKokoro.loads) == 1  # the ~310 MB model loads once across renders


def test_kokoro_id_degrades_to_none_not_a_piper_voice(
    server: types.ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    # Kokoro NOT baked (shared fixture). A kokoro-* id must return None (→ device native voice),
    # NEVER fall through to the first piper voice — that silent wrong-voice is the trap the
    # dispatch-before-resolve ordering exists to prevent.
    assert server.tts_wav("hi", "kokoro-af_heart", lead_ms=0) is None
    assert "kokoro not installed" in capsys.readouterr().err


def test_kokoro_render_failure_is_logged_not_silent(
    kokoro_server: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def boom(_self, _text, voice, speed=1.0, lang="en-us", is_phonemes=False, trim=True):  # type: ignore[no-untyped-def]
        raise RuntimeError("kokoro exploded")

    monkeypatch.setattr(_FakeKokoro, "create", boom)
    kokoro_server._kokoro_holder.clear()
    assert kokoro_server.tts_wav("hi", "kokoro-af_heart", lead_ms=0) is None
    err = capsys.readouterr().err
    assert "render failed" in err
    assert "kokoro exploded" in err


def test_dockerfile_bakes_kokoro_weights(server: types.ModuleType) -> None:
    # A curated Kokoro voice only reaches the picker if the weights are baked into the image —
    # guard that the Dockerfile bake block stays in step with the module's filenames + that the
    # curated list is non-empty (so the id scheme can't silently ship with nothing behind it).
    dockerfile = _DOCKERFILE.read_text()
    assert server.KOKORO_MODEL in dockerfile
    assert server.KOKORO_VOICES_FILE in dockerfile
    assert server.CURATED_KOKORO_VOICES


# --- W1: misaki G2P pronunciation ----------------------------------------------------------


def test_kokoro_phonemizes_with_misaki_when_available(kokoro_server: types.ModuleType) -> None:
    kokoro_server._g2p_holder.clear()
    assert kokoro_server.tts_wav("hello there", "kokoro-af_heart", lead_ms=0) is not None
    assert _FakeG2P.calls == ["hello there"]  # misaki phonemized the text...
    assert _FakeKokoro.last_create["is_phonemes"] is True  # ...and phonemes were fed as phonemes
    assert _FakeKokoro.last_create["text"] == "PH[hello there]"  # not the raw text


def test_kokoro_falls_back_to_espeak_when_misaki_absent(
    kokoro_server: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # misaki not importable → the Kokoro path lets kokoro-onnx phonemize with its own espeak.
    for name in ("misaki", "misaki.en", "misaki.espeak"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    kokoro_server._g2p_holder.clear()
    assert kokoro_server.tts_wav("hello", "kokoro-af_heart", lead_ms=0) is not None
    assert _FakeKokoro.last_create["is_phonemes"] is False  # raw text → kokoro's built-in espeak
    assert _FakeKokoro.last_create["text"] == "hello"
    assert _FakeG2P.calls == []  # misaki never ran


def test_kokoro_degrades_to_espeak_when_misaki_call_throws(
    kokoro_server: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # misaki loads but throws at phonemize time → render must fall back to espeak, not go silent.
    def boom(_self, _text):  # type: ignore[no-untyped-def]
        raise RuntimeError("g2p exploded")

    monkeypatch.setattr(_FakeG2P, "__call__", boom)
    kokoro_server._g2p_holder.clear()
    assert kokoro_server.tts_wav("hello", "kokoro-af_heart", lead_ms=0) is not None
    assert _FakeKokoro.last_create["is_phonemes"] is False  # espeak path used
    assert "misaki phonemize failed" in capsys.readouterr().err


def test_kokoro_lexicon_emits_misaki_override(
    kokoro_server: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A lexicon entry becomes a misaki inline override "[word](/phonemes/)" before phonemizing.
    monkeypatch.setitem(kokoro_server.KOKORO_LEXICON, "kokoro", "kˈOkəɹO")
    kokoro_server._g2p_holder.clear()
    kokoro_server.tts_wav("say Kokoro now", "kokoro-af_heart", lead_ms=0)
    assert _FakeG2P.calls[-1] == "say [Kokoro](/kˈOkəɹO/) now"


# --- Speakable-text normalization (°F / mph / compass / "City, ST") ------------------------


def test_speakable_text_expands_the_weather_sentence(server: types.ModuleType) -> None:
    # The reported sentence: state code, degree units, wind speed, and compass point all spoken.
    said = server._speakable_text(
        "Tomorrow in Cocoa, FL will be warm, with a high near 94 °F and a low around 75 °F. "
        "Expect light breezes (about 4 mph from the SSW)."
    )
    assert "Cocoa, Florida" in said
    assert "94 degrees Fahrenheit" in said and "75 degrees Fahrenheit" in said
    assert "4 miles per hour" in said
    assert "from the south southwest" in said
    assert "°" not in said and " FL " not in said and " mph" not in said


def test_speakable_text_degree_units_and_bare_degree(server: types.ModuleType) -> None:
    assert server._speakable_text("20 °C to 30°C") == "20 degrees Celsius to 30 degrees Celsius"
    assert server._speakable_text("0 °K is absolute zero") == "0 degrees Kelvin is absolute zero"
    assert server._speakable_text("tilt it 45° back") == "tilt it 45 degrees back"


def test_speakable_text_wind_speed_units(server: types.ModuleType) -> None:
    assert server._speakable_text("gusts to 30 mph") == "gusts to 30 miles per hour"
    assert server._speakable_text("60 kph or 60 km/h") == (
        "60 kilometers per hour or 60 kilometers per hour"
    )


def test_speakable_text_all_compass_points(server: types.ModuleType) -> None:
    # Every 2- and 3-letter code expands standalone; the 3-letter ones must not split ("SSW" is
    # NOT "S" + "SW").
    assert server._speakable_text("winds NNE then ESE then SW") == (
        "winds north northeast then east southeast then southwest"
    )
    # Bare cardinals expand only after from/the, and are left alone elsewhere.
    assert server._speakable_text("from the W and out of N") == "from the west and out of N"
    assert server._speakable_text("grade W on the exam") == "grade W on the exam"


def test_speakable_text_state_only_in_city_shape(server: types.ModuleType) -> None:
    # "City, ST" expands; a bare English-word code (IN/OR/ME) outside that shape does not.
    assert server._speakable_text("headed to Reno, NV now") == "headed to Reno, Nevada now"
    assert server._speakable_text("interested IN or ME") == "interested IN or ME"
    # A state code that is also a compass code ("NE") resolves as the state in the City shape,
    # because state expansion runs before the compass pass.
    assert server._speakable_text("out near Omaha, NE now") == "out near Omaha, Nebraska now"


def test_speakable_text_dates(server: types.ModuleType) -> None:
    # Day spoken as an ordinal, year in speech style — with or without the comma, and with a
    # trailing ordinal suffix already present.
    assert server._speakable_text("July 10, 2026") == "July tenth, twenty twenty six"
    assert server._speakable_text("July 10 2026") == "July tenth, twenty twenty six"
    assert server._speakable_text("due July 10th tonight") == "due July tenth tonight"
    assert server._speakable_text("April 21, 2010") == "April twenty first, twenty ten"


def test_speakable_text_year_reading_styles(server: types.ModuleType) -> None:
    # The year-reading conventions that differ by era.
    assert server._year_words(2026) == "twenty twenty six"
    assert server._year_words(1999) == "nineteen ninety nine"
    assert server._year_words(2000) == "two thousand"
    assert server._year_words(2005) == "two thousand five"
    assert server._year_words(1905) == "nineteen oh five"
    assert server._year_words(1900) == "nineteen hundred"
    assert server._year_words(2010) == "twenty ten"


def test_speakable_text_acronyms(server: types.ModuleType) -> None:
    # A 3-5 letter all-caps run is spelled as letters ("ORFS" -> "O R F S"); 2-letter runs and
    # stoplisted said-as-word acronyms / roman numerals / emphasis words are left alone.
    assert server._speakable_text("the ORFS crate, an AI core") == "the O R F S crate, an AI core"
    assert server._speakable_text("call the FBI now") == "call the F B I now"
    assert server._speakable_text("NASA and NATO agreed") == "NASA and NATO agreed"
    assert server._speakable_text("in World War VII") == "in World War VII"
    # A city/state code (2-letter) is untouched by the acronym rule.
    assert server._speakable_text("interested IN or ME") == "interested IN or ME"


def test_speakable_text_name_initial(server: types.ModuleType) -> None:
    # A single-letter initial before a capitalized word loses its period (so espeak doesn't pause);
    # a dotted abbreviation like "U.S." and a lowercase-led sentence end are left alone.
    assert server._speakable_text("by Dennis E. Taylor") == "by Dennis E Taylor"
    assert server._speakable_text("J. R. R. Tolkien") == "J R R Tolkien"
    assert server._speakable_text("the U.S. Grant memorial") == "the U.S. Grant memorial"
    assert server._speakable_text("Grade A. then rest") == "Grade A. then rest"


def test_speakable_text_distance_mi(server: types.ModuleType) -> None:
    # "mi" -> "miles", but only after a number (so a stray "mi" never invents the word). The wall
    # path reaches the box with digits intact; the PWA already expands this in speakable.js.
    assert server._speakable_text("40 mi south of here") == "40 miles south of here"
    assert server._speakable_text("about 3.5 mi") == "about 3.5 miles"
    assert server._speakable_text("40 min drive") == "40 min drive"  # "min" is not "mi"


def test_speakable_text_non_dates_untouched(server: types.ModuleType) -> None:
    # A day out of range is not a date; a following non-year number isn't swallowed.
    assert server._speakable_text("July 45 items") == "July 45 items"
    assert server._speakable_text("July 10 with 12 people") == "July tenth with 12 people"


def test_kokoro_lexicon_titusville_seeded(kokoro_server: types.ModuleType) -> None:
    # The seeded proper-noun override reaches the misaki path (case-insensitive, even after the
    # "City, ST" expansion turns "Titusville, FL" into "Titusville, Florida").
    kokoro_server._g2p_holder.clear()
    kokoro_server.tts_wav("visiting Titusville, FL", "kokoro-af_heart", lead_ms=0)
    assert _FakeG2P.calls[-1] == "visiting [Titusville](/tˈItəsvɪl/), Florida"


def test_tts_wav_normalizes_before_piper(server: types.ModuleType) -> None:
    # The piper path renders the SPOKEN text, not the raw symbols — normalization is pre-dispatch.
    assert server.tts_wav("high of 94 °F", "en_US-amy-medium", lead_ms=0) is not None
    assert _FakeVoice.texts[-1] == "high of 94 degrees Fahrenheit"


def test_tts_wav_normalizes_before_kokoro(kokoro_server: types.ModuleType) -> None:
    # The Kokoro/misaki path phonemizes the spoken text too (G2P records what it received).
    kokoro_server._g2p_holder.clear()
    kokoro_server.tts_wav("winds 4 mph from the SSW", "kokoro-af_heart", lead_ms=0)
    assert _FakeG2P.calls[-1] == "winds 4 miles per hour from the south southwest"


def test_dockerfile_builds_misaki_in_a_py312_venv(server: types.ModuleType) -> None:
    # The G2P upgrade rides the image build — keep the Dockerfile in step with _load_g2p's import.
    # spaCy (misaki's dep) has no Python-3.14 wheels and this base image's system Python is 3.14,
    # so misaki MUST build into a dedicated 3.12 venv that the entrypoint prefers — never back on
    # system python, where it silently degrades to espeak. Guard both halves so a refactor can't
    # quietly revert the fix.
    dockerfile = _DOCKERFILE.read_text()
    assert "misaki" in dockerfile
    assert "/opt/tts-venv" in dockerfile and "3.12" in dockerfile, (
        "misaki must install into a Python 3.12 venv (spaCy has no 3.14 wheels)"
    )
    entrypoint = (_DEPLOY / "tts-stt" / "entrypoint.sh").read_text()
    assert "/opt/tts-venv/bin/python" in entrypoint, (
        "the entrypoint must prefer the 3.12 TTS venv so misaki's G2P is actually used"
    )


# --- W2: audiobook pacing (speed + trailing silence) ---------------------------------------


def test_kokoro_speed_defaults_to_env(
    kokoro_server: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert kokoro_server.tts_wav("hi", "kokoro-af_heart", lead_ms=0) is not None
    assert _FakeKokoro.last_create["speed"] == 1.0  # default env is no-op
    monkeypatch.setattr(kokoro_server, "KOKORO_SPEED", 0.85)
    kokoro_server.tts_wav("hi", "kokoro-af_heart", lead_ms=0)
    assert _FakeKokoro.last_create["speed"] == 0.85  # env slows the read


def test_kokoro_speed_param_overrides_and_clamps(kokoro_server: types.ModuleType) -> None:
    kokoro_server.tts_wav("hi", "kokoro-af_heart", lead_ms=0, speed=0.8)
    assert _FakeKokoro.last_create["speed"] == 0.8  # explicit request wins over the env default
    kokoro_server.tts_wav("hi", "kokoro-af_heart", lead_ms=0, speed=5.0)
    assert _FakeKokoro.last_create["speed"] == 2.0  # clamped high
    kokoro_server.tts_wav("hi", "kokoro-af_heart", lead_ms=0, speed=0.1)
    assert _FakeKokoro.last_create["speed"] == 0.5  # clamped low


def test_kokoro_trail_appends_silence(kokoro_server: types.ModuleType) -> None:
    plain = kokoro_server.tts_wav("hi", "kokoro-af_heart", lead_ms=0)
    padded = kokoro_server.tts_wav("hi", "kokoro-af_heart", lead_ms=0, trail_ms=200)
    assert plain and padded
    # 100 base samples @ 24 kHz + 200 ms of trailing silence (0.2 * 24000 = 4800 frames).
    assert _wav_frames(plain) == 100
    assert _wav_frames(padded) == 100 + 4800


def test_kokoro_trail_defaults_to_env(
    kokoro_server: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(kokoro_server, "KOKORO_TRAIL_MS", 100)
    out = kokoro_server.tts_wav("hi", "kokoro-af_heart", lead_ms=0)
    assert out is not None
    assert _wav_frames(out) == 100 + 2400  # 100 ms env trail = 2400 frames @ 24 kHz


def test_piper_ignores_the_kokoro_trail_env_default(
    server: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The audiobook trail is a Kokoro control — the snappy piper fallback must not inherit it.
    monkeypatch.setattr(server, "KOKORO_TRAIL_MS", 500)
    out = server.tts_wav("hi", "en_US-amy-medium", lead_ms=0)
    assert out is not None
    assert _wav_frames(out) == 100  # piper's own 100 frames, no trailing silence


# --- W3: narrator voice blending -----------------------------------------------------------


def test_kokoro_blend_listed_after_the_plain_voices(kokoro_server: types.ModuleType) -> None:
    voices = kokoro_server.piper_voices()
    assert "kokoro-narrator" in voices
    assert voices.index("kokoro-narrator") > voices.index("kokoro-af_heart")  # blends come last


def test_kokoro_blend_weighted_averages_the_voice_styles(kokoro_server: types.ModuleType) -> None:
    kokoro_server._g2p_holder.clear()
    assert kokoro_server.tts_wav("hi", "kokoro-narrator", lead_ms=0) is not None
    voice = _FakeKokoro.last_create["voice"]
    # narrator = am_michael*0.6 + af_nicole*0.4 = [10,20]*0.6 + [30,40]*0.4 = [18, 28].
    assert isinstance(voice, _FakeStyle)
    assert voice.data == pytest.approx([18.0, 28.0])
    assert _FakeKokoro.last_create["is_phonemes"] is True  # blends still ride the misaki path


def test_kokoro_plain_voice_passes_its_name_not_a_blend(kokoro_server: types.ModuleType) -> None:
    kokoro_server._g2p_holder.clear()
    kokoro_server.tts_wav("hi", "kokoro-af_heart", lead_ms=0)
    assert _FakeKokoro.last_create["voice"] == "af_heart"  # a plain voice is a name string


def test_kokoro_blends_are_well_formed(server: types.ModuleType) -> None:
    # Guard the blend registry: non-empty (an empty blend renders None), keys don't collide with a
    # real voice name (which would shadow it), and every referenced voice is actually baked in the
    # bin (a typo would otherwise render None).
    for key, blend in server.KOKORO_BLENDS.items():
        assert blend, f"blend {key!r} is empty"
        assert key not in server.CURATED_KOKORO_VOICES, f"blend {key!r} collides with a real voice"
        for vname, _weight in blend:
            assert vname in server.CURATED_KOKORO_VOICES
