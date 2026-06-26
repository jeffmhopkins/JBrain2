"""The shared `ImageRenderService` (Wave L2) directly: dims/seed/steps/speed gating, the
unload calls, and the typed error mapping — with `FakeImageGen` and an in-memory blob store,
no Postgres. The agent path's behavior against real PG is covered unchanged in
tests/integration/test_imagegentools_pg.py; here we exercise the extracted core in isolation."""

import hashlib

import pytest

from jbrain.db.session import SessionContext
from jbrain.image_gen.comfyui import ImageGenError, ImageGenInterrupted, OnProgress
from jbrain.image_gen.fake import FakeImageGen
from jbrain.image_gen.render import (
    ImageRenderService,
    ModelNotInstalledError,
    RenderValidationError,
)
from jbrain.models.images import GeneratedImage
from tests.unit.fakes import FakeComfyUiGateway, FakeLocalGateway

_OWNER = SessionContext(principal_id="owner-1", principal_kind="owner")


class MemBlobStore:
    """In-memory content-addressed blob store (the render service only put()s)."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    async def put(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        self.blobs[digest] = data
        return digest


class _FakeRepo:
    """Records the inserted row without a DB. The service opens a scoped_session and passes the
    yielded session to insert(); it is patched to a noop below, so the repo ignores it."""

    def __init__(self) -> None:
        self.inserted: dict | None = None

    async def insert(self, session: object, **kw: object) -> GeneratedImage:
        self.inserted = dict(kw)
        return GeneratedImage(**kw)  # type: ignore[arg-type]


class _NoopSession:
    pass


@pytest.fixture(autouse=True)
def _patch_scoped_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace scoped_session with a noop async context manager so the unit test needs no
    Postgres — the FakeRepo never touches the yielded session."""
    import jbrain.image_gen.render as render_mod

    class _Scoped:
        def __init__(self, _maker: object, _ctx: object) -> None: ...

        async def __aenter__(self) -> _NoopSession:
            return _NoopSession()

        async def __aexit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(render_mod, "scoped_session", _Scoped)


def _service(
    fake: FakeImageGen,
    *,
    provisioned: tuple[str, ...] = (
        "qwen-image",
        "qwen-image-lightning",
        "qwen-image-edit",
        "qwen-image-edit-lightning",
        "dreamshaper",
    ),
) -> tuple[ImageRenderService, _FakeRepo, MemBlobStore, FakeLocalGateway, FakeComfyUiGateway]:
    repo = _FakeRepo()
    blobs = MemBlobStore()
    lg = FakeLocalGateway(running={"gpt-oss-120b"})
    cg = FakeComfyUiGateway()
    svc = ImageRenderService(
        fake,
        blobs,  # type: ignore[arg-type]
        repo,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]  # maker — unused once scoped_session is patched
        lg,
        cg,
        provisioned,
    )
    return svc, repo, blobs, lg, cg


async def test_generate_resolves_dims_seed_steps_and_stores() -> None:
    fake = FakeImageGen()
    svc, repo, blobs, _, _ = _service(fake)

    row = await svc.generate(
        _OWNER, prompt="a fox", aspect="portrait", resolution="medium", steps=35, seed=99
    )

    assert (row.width, row.height) == (768, 1024)  # portrait preset
    assert row.kind == "generate" and row.model == "qwen-image-2512"
    assert row.seed == 99 and row.steps == 35  # explicit seed + in-band steps recorded
    assert fake.last_gen is not None and fake.last_gen.seed == 99 and fake.last_gen.steps == 35
    assert repo.inserted is not None and repo.inserted["blob_sha256"] in blobs.blobs


async def test_generate_random_seed_is_recorded() -> None:
    fake = FakeImageGen()
    svc, _, _, _, _ = _service(fake)
    row = await svc.generate(_OWNER, prompt="x", aspect="square", resolution="medium")
    assert isinstance(row.seed, int)
    assert fake.last_gen is not None and fake.last_gen.seed == row.seed  # the used seed is recorded


async def test_generate_unloads_llm_before_and_comfyui_after() -> None:
    fake = FakeImageGen()
    svc, _, _, lg, cg = _service(fake)
    await svc.generate(_OWNER, prompt="x", aspect="square", resolution="medium")
    assert lg.unloaded == ["gpt-oss-120b"]  # the resident LLM was freed before the render
    assert cg.frees == [(True, True)]  # ComfyUI's model freed after


async def test_generate_bad_aspect_raises_validation() -> None:
    fake = FakeImageGen()
    svc, _, _, _, _ = _service(fake)
    with pytest.raises(RenderValidationError, match="aspect"):
        await svc.generate(_OWNER, prompt="x", aspect="hexagon", resolution="medium")
    assert fake.last_gen is None  # no spend


async def test_generate_bad_resolution_raises_validation() -> None:
    fake = FakeImageGen()
    svc, _, _, _, _ = _service(fake)
    with pytest.raises(RenderValidationError, match="resolution"):
        await svc.generate(_OWNER, prompt="x", aspect="square", resolution="gigantic")


async def test_generate_fast_not_installed_raises_with_setup_id() -> None:
    fake = FakeImageGen()
    svc, _, _, _, _ = _service(fake, provisioned=("qwen-image",))
    with pytest.raises(ModelNotInstalledError, match="comfyui-setup.sh qwen-image-lightning"):
        await svc.generate(_OWNER, prompt="x", aspect="square", resolution="medium", speed="fast")
    assert fake.last_gen is None  # bailed before any spend


async def test_generate_fast_records_lightning_at_four_steps() -> None:
    fake = FakeImageGen()
    svc, _, _, _, _ = _service(fake)
    row = await svc.generate(
        _OWNER, prompt="x", aspect="square", resolution="medium", speed="fast", steps=30
    )
    assert row.model == "qwen-image-lightning" and row.steps == 4  # fast ignores the steps knob


async def test_generate_interrupt_propagates_and_skips_comfyui_free() -> None:
    """An interrupt propagates untouched (the caller maps it) and the ComfyUI free is skipped —
    behavior-identical to the original handler's bail before _free_comfyui_model."""

    class _Interrupting(FakeImageGen):
        async def generate(self, spec: object, on_progress: OnProgress | None = None) -> bytes:
            raise ImageGenInterrupted("stopped")

    fake = _Interrupting()
    svc, repo, _, _, cg = _service(fake)
    with pytest.raises(ImageGenInterrupted):
        await svc.generate(_OWNER, prompt="x", aspect="square", resolution="medium")
    assert repo.inserted is None  # nothing stored
    assert cg.frees == []  # ComfyUI not freed on the error path


async def test_generate_error_propagates() -> None:
    class _Failing(FakeImageGen):
        async def generate(self, spec: object, on_progress: OnProgress | None = None) -> bytes:
            raise ImageGenError("boom")

    svc, repo, _, _, _ = _service(_Failing())
    with pytest.raises(ImageGenError):
        await svc.generate(_OWNER, prompt="x", aspect="square", resolution="medium")
    assert repo.inserted is None


async def test_edit_records_source_and_real_output_dims() -> None:
    fake = FakeImageGen(out_dims=(1264, 948))  # a megapixel-scaled, non-square output
    svc, repo, _, _, _ = _service(fake)

    row = await svc.edit(
        _OWNER,
        prompt="make it night",
        source_bytes=b"\x89PNG\r\n\x1a\nsource",
        source_sha="src-sha",
        resolution="medium",
    )

    assert row.kind == "edit" and (row.width, row.height) == (1264, 948)  # the real PNG dims
    assert row.source_sha256 == "src-sha"  # the source blob's sha recorded on the edit row
    assert fake.last_source == b"\x89PNG\r\n\x1a\nsource"


async def test_edit_passes_references_primary_first() -> None:
    fake = FakeImageGen()
    svc, _, _, _, _ = _service(fake)
    await svc.edit(
        _OWNER,
        prompt="combine",
        source_bytes=b"\x89PNG\r\n\x1a\nprimary",
        source_sha="s",
        resolution="medium",
        extra_sources=[b"\x89PNG\r\n\x1a\nref1", b"\x89PNG\r\n\x1a\nref2"],
    )
    assert fake.last_sources[0] == b"\x89PNG\r\n\x1a\nprimary"  # primary stays first
    assert len(fake.last_sources) == 3


async def test_edit_fast_not_installed_raises() -> None:
    fake = FakeImageGen()
    svc, _, _, _, _ = _service(fake, provisioned=("qwen-image", "qwen-image-edit"))
    with pytest.raises(ModelNotInstalledError, match="qwen-image-edit-lightning"):
        await svc.edit(
            _OWNER,
            prompt="x",
            source_bytes=b"\x89PNG\r\n\x1a\ns",
            source_sha="s",
            resolution="medium",
            speed="fast",
        )
    assert fake.last_edit is None


async def test_edit_fast_records_lightning_edit_at_four_steps() -> None:
    fake = FakeImageGen()
    svc, _, _, _, _ = _service(fake)
    row = await svc.edit(
        _OWNER,
        prompt="x",
        source_bytes=b"\x89PNG\r\n\x1a\ns",
        source_sha="s",
        resolution="medium",
        speed="fast",
    )
    assert row.model == "qwen-image-edit-lightning" and row.steps == 4
