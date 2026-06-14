"""The one-time install wipe: its guards, storage clearing, and orchestration.
The destructive DB SQL is deploy-only; here we prove it fires exactly when it
should and never otherwise, and that the storage clear + sentinel are correct."""

from pathlib import Path

import pytest

from jbrain import install_wipe
from jbrain.config import Settings


def test_should_wipe_truth_table() -> None:
    assert install_wipe.should_wipe(enabled=True, sentinel_exists=False) is True
    assert install_wipe.should_wipe(enabled=True, sentinel_exists=True) is False  # already done
    assert install_wipe.should_wipe(enabled=False, sentinel_exists=False) is False  # not opted in
    assert install_wipe.should_wipe(enabled=False, sentinel_exists=True) is False


def test_clear_dir_empties_contents_keeps_the_dir(tmp_path: Path) -> None:
    (tmp_path / "a.bin").write_text("x")
    nested = tmp_path / "sub"
    nested.mkdir()
    (nested / "b.bin").write_text("y")

    install_wipe.clear_dir(str(tmp_path))

    assert tmp_path.exists()  # the mounted volume stays
    assert list(tmp_path.iterdir()) == []  # contents gone, including nested dirs


def test_clear_dir_missing_dir_is_a_noop(tmp_path: Path) -> None:
    install_wipe.clear_dir(str(tmp_path / "nope"))  # must not raise


def _settings(tmp_path: Path, *, enabled: bool) -> Settings:
    return Settings(
        wipe_on_first_deploy=enabled,
        blob_dir=str(tmp_path / "blobs"),
        backups_dir=str(tmp_path / "backups"),
    )


def _stub_destructive(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    async def _drop(url: str) -> None:
        calls.append("drop")

    def _rebuild() -> None:
        calls.append("rebuild")

    monkeypatch.setattr(install_wipe, "_drop_schema", _drop)
    monkeypatch.setattr(install_wipe, "_rebuild_schema", _rebuild)


def test_main_noops_when_flag_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install_wipe, "get_settings", lambda: _settings(tmp_path, enabled=False))
    calls: list[str] = []
    _stub_destructive(monkeypatch, calls)
    monkeypatch.setenv(install_wipe.MIGRATION_URL_ENV, "postgresql+asyncpg://x")

    assert install_wipe.main() == 0
    assert calls == []  # nothing destructive ran
    assert not install_wipe._sentinel_path(_settings(tmp_path, enabled=False)).exists()


def test_main_noops_when_sentinel_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, enabled=True)
    sentinel = install_wipe._sentinel_path(settings)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("done earlier")
    monkeypatch.setattr(install_wipe, "get_settings", lambda: settings)
    calls: list[str] = []
    _stub_destructive(monkeypatch, calls)
    monkeypatch.setenv(install_wipe.MIGRATION_URL_ENV, "postgresql+asyncpg://x")

    assert install_wipe.main() == 0
    assert calls == []  # idempotent: never wipes twice


def test_main_refuses_without_migration_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(install_wipe, "get_settings", lambda: _settings(tmp_path, enabled=True))
    calls: list[str] = []
    _stub_destructive(monkeypatch, calls)
    monkeypatch.delenv(install_wipe.MIGRATION_URL_ENV, raising=False)

    assert install_wipe.main() == 1  # refuses rather than half-running
    assert calls == []


def test_main_wipes_rebuilds_and_writes_sentinel_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, enabled=True)
    # Pre-seed storage that must be cleared, and a sentinel that must NOT exist yet.
    Path(settings.blob_dir).mkdir(parents=True)
    (Path(settings.blob_dir) / "stale.bin").write_text("old")
    monkeypatch.setattr(install_wipe, "get_settings", lambda: settings)
    calls: list[str] = []
    _stub_destructive(monkeypatch, calls)
    monkeypatch.setenv(install_wipe.MIGRATION_URL_ENV, "postgresql+asyncpg://x")

    assert install_wipe.main() == 0
    # Order: drop schema → clear storage → rebuild (then sentinel).
    assert calls == ["drop", "rebuild"]
    assert (Path(settings.blob_dir) / "stale.bin").exists() is False  # storage cleared
    assert install_wipe._sentinel_path(settings).exists()  # written last


def test_sentinel_not_written_when_a_destructive_step_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Safety-critical: if the wipe blows up mid-way, it must NOT mark itself done
    # — a re-run has to finish the reset, not skip a half-wiped install.
    settings = _settings(tmp_path, enabled=True)
    monkeypatch.setattr(install_wipe, "get_settings", lambda: settings)
    monkeypatch.setenv(install_wipe.MIGRATION_URL_ENV, "postgresql+asyncpg://x")

    async def _boom(url: str) -> None:
        raise RuntimeError("schema drop failed")

    monkeypatch.setattr(install_wipe, "_drop_schema", _boom)

    with pytest.raises(RuntimeError):
        install_wipe.main()
    assert install_wipe._sentinel_path(settings).exists() is False  # re-runnable


def test_empty_string_flag_env_falls_back_to_off(monkeypatch: pytest.MonkeyPatch) -> None:
    # Compose passes ${WIPE_ON_FIRST_DEPLOY:-} (empty) when not opted in; that
    # must parse as off, not crash the one-shot (env_ignore_empty).
    monkeypatch.setenv("JBRAIN_WIPE_ON_FIRST_DEPLOY", "")
    assert Settings().wipe_on_first_deploy is False
