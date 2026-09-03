"""The /api/settings surface — the first server-synced user settings — with
the store faked; the real store's SQL semantics are covered in
test_settings_pg."""

import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from jbrain.auth import service as auth_service
from jbrain.config import Settings
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo, FakeSettingsStore


@pytest.fixture
def client() -> Iterator[tuple[TestClient, FakeSettingsStore]]:
    app = create_app(
        Settings(secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none")
    )
    auth_repo = FakeAuthRepo()
    store = FakeSettingsStore()
    with TestClient(app) as test_client:
        app.state.auth_repo = auth_repo
        app.state.settings_store = store
        key = asyncio.run(auth_service.rotate_owner_key(auth_repo))
        assert (
            test_client.post(
                "/api/auth/session", json={"owner_key": key, "device_label": "t"}
            ).status_code
            == 204
        )
        yield test_client, store


def test_settings_require_auth() -> None:
    app = create_app(
        Settings(secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none")
    )
    with TestClient(app) as anon:
        app.state.auth_repo = FakeAuthRepo()
        assert anon.get("/api/settings").status_code == 401
        assert anon.put("/api/settings", json={"image_analysis_mode": "ocr"}).status_code == 401


def test_get_settings_defaults_to_full_analysis(
    client: tuple[TestClient, FakeSettingsStore],
) -> None:
    c, _ = client
    # No row yet: the default is full analysis [decided]; timezone is unset (UTC);
    # LLM wall-display streaming is off (owner text stays off the display by default).
    assert c.get("/api/settings").json() == {
        "image_analysis_mode": "full",
        "owner_timezone": None,
        "owner_callsign": None,
        "brain_llm_stream": False,
        "brain_read_aloud": False,
        "brain_answer_voice": "kokoro-af_heart",
        "brain_read_aloud_engine": "piper",
        "brain_answer_speed": 1.0,
        "brain_answer_pitch": 0.0,
        "brain_answer_chorus": False,
        "brain_answer_robot": False,
        "local_llm_auto_update": True,
        "local_llm_patch_restore_checkpoint": False,
        "pronunciation_lexicon": {},
    }


def test_put_settings_round_trips_the_mode(client: tuple[TestClient, FakeSettingsStore]) -> None:
    c, store = client
    resp = c.put("/api/settings", json={"image_analysis_mode": "ocr"})
    assert resp.status_code == 200
    assert resp.json() == {
        "image_analysis_mode": "ocr",
        "owner_timezone": None,
        "owner_callsign": None,
        "brain_llm_stream": False,
        "brain_read_aloud": False,
        "brain_answer_voice": "kokoro-af_heart",
        "brain_read_aloud_engine": "piper",
        "brain_answer_speed": 1.0,
        "brain_answer_pitch": 0.0,
        "brain_answer_chorus": False,
        "brain_answer_robot": False,
        "local_llm_auto_update": True,
        "local_llm_patch_restore_checkpoint": False,
        "pronunciation_lexicon": {},
    }
    assert store.values["image_analysis_mode"] == "ocr"
    assert c.get("/api/settings").json() == {
        "image_analysis_mode": "ocr",
        "owner_timezone": None,
        "owner_callsign": None,
        "brain_llm_stream": False,
        "brain_read_aloud": False,
        "brain_answer_voice": "kokoro-af_heart",
        "brain_read_aloud_engine": "piper",
        "brain_answer_speed": 1.0,
        "brain_answer_pitch": 0.0,
        "brain_answer_chorus": False,
        "brain_answer_robot": False,
        "local_llm_auto_update": True,
        "local_llm_patch_restore_checkpoint": False,
        "pronunciation_lexicon": {},
    }

    assert c.put("/api/settings", json={"image_analysis_mode": "full"}).json() == {
        "image_analysis_mode": "full",
        "owner_timezone": None,
        "owner_callsign": None,
        "brain_llm_stream": False,
        "brain_read_aloud": False,
        "brain_answer_voice": "kokoro-af_heart",
        "brain_read_aloud_engine": "piper",
        "brain_answer_speed": 1.0,
        "brain_answer_pitch": 0.0,
        "brain_answer_chorus": False,
        "brain_answer_robot": False,
        "local_llm_auto_update": True,
        "local_llm_patch_restore_checkpoint": False,
        "pronunciation_lexicon": {},
    }


def test_put_settings_round_trips_the_timezone(
    client: tuple[TestClient, FakeSettingsStore],
) -> None:
    c, store = client
    resp = c.put("/api/settings", json={"owner_timezone": "America/New_York"})
    assert resp.status_code == 200
    assert resp.json() == {
        "image_analysis_mode": "full",
        "owner_timezone": "America/New_York",
        "owner_callsign": None,
        "brain_llm_stream": False,
        "brain_read_aloud": False,
        "brain_answer_voice": "kokoro-af_heart",
        "brain_read_aloud_engine": "piper",
        "brain_answer_speed": 1.0,
        "brain_answer_pitch": 0.0,
        "brain_answer_chorus": False,
        "brain_answer_robot": False,
        "local_llm_auto_update": True,
        "local_llm_patch_restore_checkpoint": False,
        "pronunciation_lexicon": {},
    }
    assert store.values["owner_timezone"] == "America/New_York"


def test_put_settings_round_trips_brain_llm_stream(
    client: tuple[TestClient, FakeSettingsStore],
) -> None:
    c, store = client
    # Off by default; an explicit enable persists and round-trips true, then off again.
    resp = c.put("/api/settings", json={"brain_llm_stream": True})
    assert resp.status_code == 200
    assert resp.json()["brain_llm_stream"] is True
    assert store.values["brain_llm_stream"] is True
    assert c.get("/api/settings").json()["brain_llm_stream"] is True
    off = c.put("/api/settings", json={"brain_llm_stream": False})
    assert off.json()["brain_llm_stream"] is False


def test_put_settings_rejects_non_bool_brain_llm_stream(
    client: tuple[TestClient, FakeSettingsStore],
) -> None:
    c, store = client
    # A non-boolean that pydantic can't coerce to a bool is a 422 — a junk value must
    # never land as an enable (the display carries owner text only on a real `true`).
    assert c.put("/api/settings", json={"brain_llm_stream": "maybe"}).status_code == 422
    assert c.put("/api/settings", json={"brain_llm_stream": [1]}).status_code == 422
    assert "brain_llm_stream" not in store.values


def test_put_settings_round_trips_brain_read_aloud_and_pushes_flag(
    client: tuple[TestClient, FakeSettingsStore],
) -> None:
    c, store = client
    # The PUT also fire-and-forget pushes the flag to the wall so the voice panel shows/
    # hides on the toggle without a chat turn; capture those pushes.
    pushes: list[tuple[str, bool]] = []
    c.app.state.brain_flag_emit = lambda kind, on: pushes.append((kind, on))  # type: ignore[attr-defined]

    resp = c.put("/api/settings", json={"brain_read_aloud": True})
    assert resp.status_code == 200
    assert resp.json()["brain_read_aloud"] is True
    assert store.values["brain_read_aloud"] is True
    assert c.get("/api/settings").json()["brain_read_aloud"] is True
    assert pushes == [("read_aloud", True)]

    off = c.put("/api/settings", json={"brain_read_aloud": False})
    assert off.json()["brain_read_aloud"] is False
    assert pushes == [("read_aloud", True), ("read_aloud", False)]


def test_put_settings_rejects_non_bool_brain_read_aloud(
    client: tuple[TestClient, FakeSettingsStore],
) -> None:
    c, store = client
    assert c.put("/api/settings", json={"brain_read_aloud": "maybe"}).status_code == 422
    assert c.put("/api/settings", json={"brain_read_aloud": [1]}).status_code == 422
    assert "brain_read_aloud" not in store.values


def test_put_settings_round_trips_brain_answer_voice(
    client: tuple[TestClient, FakeSettingsStore],
) -> None:
    c, store = client
    # Defaults to Amy; a chosen voice id (incl. a multi-speaker "#speaker" entry) persists
    # and round-trips — this is the voice the PWA read-aloud renders answers in.
    assert c.get("/api/settings").json()["brain_answer_voice"] == "kokoro-af_heart"
    resp = c.put("/api/settings", json={"brain_answer_voice": "kokoro-am_michael"})
    assert resp.status_code == 200
    assert resp.json()["brain_answer_voice"] == "kokoro-am_michael"
    assert store.values["brain_answer_voice"] == "kokoro-am_michael"
    assert c.get("/api/settings").json()["brain_answer_voice"] == "kokoro-am_michael"


def test_put_settings_rejects_blank_brain_answer_voice(
    client: tuple[TestClient, FakeSettingsStore],
) -> None:
    c, store = client
    # A blank id would read back as the default (i.e. "unset"), so it's rejected rather
    # than stored; an over-long id is a 422 at validation.
    assert c.put("/api/settings", json={"brain_answer_voice": "   "}).status_code == 422
    assert c.put("/api/settings", json={"brain_answer_voice": "x" * 200}).status_code == 422
    assert "brain_answer_voice" not in store.values


def test_put_settings_round_trips_read_aloud_engine(
    client: tuple[TestClient, FakeSettingsStore],
) -> None:
    c, store = client
    # Defaults to piper (on-box, native fallback); the owner can switch to the device's
    # native voice and back.
    assert c.get("/api/settings").json()["brain_read_aloud_engine"] == "piper"
    resp = c.put("/api/settings", json={"brain_read_aloud_engine": "native"})
    assert resp.status_code == 200
    assert resp.json()["brain_read_aloud_engine"] == "native"
    assert store.values["brain_read_aloud_engine"] == "native"
    back = c.put("/api/settings", json={"brain_read_aloud_engine": "piper"})
    assert back.json()["brain_read_aloud_engine"] == "piper"


def test_put_settings_round_trips_pronunciation_lexicon(
    client: tuple[TestClient, FakeSettingsStore],
) -> None:
    c, store = client
    # Empty by default; a respelling map round-trips, and a blank word/say is dropped on store.
    assert c.get("/api/settings").json()["pronunciation_lexicon"] == {}
    resp = c.put(
        "/api/settings",
        json={"pronunciation_lexicon": {"Titusville": "Tight us ville", "  ": "x", "y": "  "}},
    )
    assert resp.status_code == 200
    assert resp.json()["pronunciation_lexicon"] == {"Titusville": "Tight us ville"}
    assert store.values["pronunciation_lexicon"] == {"Titusville": "Tight us ville"}
    # Replace semantics: an empty map clears it.
    assert (
        c.put("/api/settings", json={"pronunciation_lexicon": {}}).json()["pronunciation_lexicon"]
        == {}
    )


def test_put_settings_round_trips_voice_effects_and_pushes_them_to_the_wall(
    client: tuple[TestClient, FakeSettingsStore],
) -> None:
    c, store = client
    # Defaults are no-ops so an untouched box is unchanged.
    got = c.get("/api/settings").json()
    assert got["brain_answer_speed"] == 1.0
    assert got["brain_answer_pitch"] == 0.0
    assert got["brain_answer_chorus"] is False
    assert got["brain_answer_robot"] is False
    # The PUT also fire-and-forget pushes each effect to the wall so the display reads at the
    # chosen speed/pitch/chorus/robot live (no redeploy); capture those value pushes.
    pushes: list[tuple[str, float | bool]] = []
    c.app.state.brain_value_emit = lambda kind, value: pushes.append((kind, value))  # type: ignore[attr-defined]

    resp = c.put(
        "/api/settings",
        json={
            "brain_answer_speed": 1.25,
            "brain_answer_pitch": -3.0,
            "brain_answer_chorus": True,
            "brain_answer_robot": True,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["brain_answer_speed"] == 1.25
    assert body["brain_answer_pitch"] == -3.0
    assert body["brain_answer_chorus"] is True
    assert body["brain_answer_robot"] is True
    assert store.values["brain_answer_speed"] == 1.25
    assert pushes == [
        ("answer_speed", 1.25),
        ("answer_pitch", -3.0),
        ("answer_chorus", True),
        ("answer_robot", True),
    ]


def test_put_settings_rejects_out_of_range_speed_and_pitch(
    client: tuple[TestClient, FakeSettingsStore],
) -> None:
    c, store = client
    # Bounds are enforced at the edge (422), not silently clamped, so a junk value never lands.
    assert c.put("/api/settings", json={"brain_answer_speed": 3.0}).status_code == 422
    assert c.put("/api/settings", json={"brain_answer_speed": 0.1}).status_code == 422
    assert c.put("/api/settings", json={"brain_answer_pitch": 20.0}).status_code == 422
    assert c.put("/api/settings", json={"brain_answer_pitch": -99.0}).status_code == 422
    assert "brain_answer_speed" not in store.values
    assert "brain_answer_pitch" not in store.values


def test_put_settings_rejects_unknown_read_aloud_engine(
    client: tuple[TestClient, FakeSettingsStore],
) -> None:
    c, store = client
    assert c.put("/api/settings", json={"brain_read_aloud_engine": "robot"}).status_code == 422
    assert "brain_read_aloud_engine" not in store.values


def test_put_settings_rejects_an_unknown_timezone(
    client: tuple[TestClient, FakeSettingsStore],
) -> None:
    c, store = client
    assert c.put("/api/settings", json={"owner_timezone": "Mars/Olympus"}).status_code == 422
    assert "owner_timezone" not in store.values  # a bad zone never lands


def test_put_settings_rejects_unknown_keys_and_values(
    client: tuple[TestClient, FakeSettingsStore],
) -> None:
    c, store = client
    assert c.put("/api/settings", json={"image_analysis_mode": "everything"}).status_code == 422
    assert c.put("/api/settings", json={"theme": "dark"}).status_code == 422
    assert store.values == {}  # nothing leaked into the store


def test_put_settings_with_empty_patch_changes_nothing(
    client: tuple[TestClient, FakeSettingsStore],
) -> None:
    c, store = client
    store.values["image_analysis_mode"] = "ocr"
    assert c.put("/api/settings", json={}).json() == {
        "image_analysis_mode": "ocr",
        "owner_timezone": None,
        "owner_callsign": None,
        "brain_llm_stream": False,
        "brain_read_aloud": False,
        "brain_answer_voice": "kokoro-af_heart",
        "brain_read_aloud_engine": "piper",
        "brain_answer_speed": 1.0,
        "brain_answer_pitch": 0.0,
        "brain_answer_chorus": False,
        "brain_answer_robot": False,
        "local_llm_auto_update": True,
        "local_llm_patch_restore_checkpoint": False,
        "pronunciation_lexicon": {},
    }


def test_gateway_auto_update_defaults_on_and_can_be_turned_off(
    client: tuple[TestClient, FakeSettingsStore],
) -> None:
    """The switch governing whether an update loads a model into the GPU at all lived only
    in the box's `.env` — unreachable for an owner running it remotely with no terminal
    (CLAUDE.md #10). Default ON, because tracking llama.cpp master is what makes a
    freshly-released model work with no manual step."""
    c, _store = client

    assert c.get("/api/settings").json()["local_llm_auto_update"] is True

    patched = c.put("/api/settings", json={"local_llm_auto_update": False})

    assert patched.status_code == 200
    assert patched.json()["local_llm_auto_update"] is False
    assert c.get("/api/settings").json()["local_llm_auto_update"] is False


def test_gateway_patch_restore_defaults_off_and_can_be_turned_on(
    client: tuple[TestClient, FakeSettingsStore],
) -> None:
    """The Fast-Qwen-loads patch (patched llama-server → fast qwen MTP-hybrid disk restores)
    lived only in the box's `.env`, unreachable for a no-terminal owner (CLAUDE.md #10).
    Default OFF, because the patch is a ~20-30 min from-source rebuild the owner opts into."""
    c, _store = client

    assert c.get("/api/settings").json()["local_llm_patch_restore_checkpoint"] is False

    patched = c.put("/api/settings", json={"local_llm_patch_restore_checkpoint": True})

    assert patched.status_code == 200
    assert patched.json()["local_llm_patch_restore_checkpoint"] is True
    assert c.get("/api/settings").json()["local_llm_patch_restore_checkpoint"] is True


def test_a_callsign_is_stored_upper_cased(client: tuple[TestClient, FakeSettingsStore]) -> None:
    c, store = client

    out = c.put("/api/settings", json={"owner_callsign": " ke8xyz-9 "})

    # Upper case is how it travels on the air; storing it as typed would make every
    # later comparison a case-folding question. It lives here rather than on the Radio
    # screen because it is the owner's identity, not a property of the radio.
    assert out.status_code == 200
    assert out.json()["owner_callsign"] == "KE8XYZ-9"


def test_an_empty_callsign_clears_it(client: tuple[TestClient, FakeSettingsStore]) -> None:
    c, _ = client
    c.put("/api/settings", json={"owner_callsign": "KE8XYZ"})

    out = c.put("/api/settings", json={"owner_callsign": ""})

    # Unset is a real state with a real consequence: "my traffic" becomes uncomputable
    # and the radio screen has to say so rather than quietly matching nothing.
    assert out.json()["owner_callsign"] is None


@pytest.mark.parametrize("bad", ["KE8 XYZ", "KE8XYZ!", "KE8XYZ\u00e9", "X" * 17])
def test_a_mangled_callsign_is_refused_not_cleaned(
    client: tuple[TestClient, FakeSettingsStore], bad: str
) -> None:
    c, _ = client

    # Refused rather than stripped: a callsign that quietly lost a character filters for
    # a station that does not exist, and an empty heard log reads as a deaf radio.
    assert c.put("/api/settings", json={"owner_callsign": bad}).status_code == 422
