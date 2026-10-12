"""The host-mode preview allocator: per-session port + unguessable hostname, no
subprocess, no network (Wave P1 of docs/archive/JCODE_PREVIEW_HOST_PLAN.md)."""

from __future__ import annotations

import pytest

from jcode_ctl.host_preview import HostPreviewManager
from jcode_ctl.preview import PreviewError


def _mgr(base_host: str = "box.example.com", **kw) -> HostPreviewManager:
    return HostPreviewManager(base_host=base_host, **kw)


def test_ensure_allocates_a_port_slug_and_url() -> None:
    alloc = _mgr().ensure("s1")
    assert alloc.port == 5173  # first of the default pool
    assert len(alloc.slug) == 16  # token_hex(8) → 16 unguessable hex chars
    assert alloc.url == f"https://{alloc.slug}-preview.box.example.com"


def test_ensure_is_idempotent_per_session() -> None:
    mgr = _mgr()
    first = mgr.ensure("s1")
    # A repeat returns the SAME reservation — the URL/port must be stable for the
    # session's life so a restart resumes on the same $PORT.
    assert mgr.ensure("s1") == first
    assert mgr.url("s1") == first.url
    assert mgr.port_for("s1") == first.port


def test_distinct_sessions_get_distinct_ports() -> None:
    mgr = _mgr()
    ports = {mgr.ensure(s).port for s in ("a", "b", "c")}
    assert ports == {5173, 5174, 5175}


def test_resolve_maps_slug_back_to_session() -> None:
    mgr = _mgr()
    alloc = mgr.ensure("s1")
    assert mgr.resolve(alloc.slug) == "s1"
    assert mgr.resolve("nope") is None


def test_release_frees_the_port_and_slug_for_reuse() -> None:
    mgr = _mgr()
    first = mgr.ensure("s1")
    mgr.release("s1")
    assert mgr.url("s1") is None
    assert mgr.port_for("s1") is None
    assert mgr.resolve(first.slug) is None
    # The freed port is available again to the next session.
    assert mgr.ensure("s2").port == first.port


def test_pool_exhaustion_raises() -> None:
    # A two-port pool serves two sessions, then refuses the third.
    mgr = _mgr(port_low=5173, port_high=5174)
    mgr.ensure("a")
    mgr.ensure("b")
    with pytest.raises(PreviewError, match="exhausted"):
        mgr.ensure("c")


def test_release_all_clears_every_reservation() -> None:
    mgr = _mgr()
    mgr.ensure("a")
    mgr.ensure("b")
    mgr.release_all()
    assert mgr.port_for("a") is None and mgr.port_for("b") is None


def test_base_host_is_sanitized() -> None:
    # Stray whitespace / a trailing dot in the configured host must not leak to the URL.
    mgr = _mgr(base_host="  box.example.com.  ")
    alloc = mgr.ensure("s1")
    assert alloc.url == f"https://{alloc.slug}-preview.box.example.com"


def test_releasing_one_session_leaves_the_others_routable() -> None:
    # The by_sid/by_slug maps are the routing state, so a partial release must not
    # disturb another session's port or slug→sid resolution.
    mgr = _mgr()
    a = mgr.ensure("a")
    b = mgr.ensure("b")
    mgr.release("a")
    assert mgr.resolve(a.slug) is None
    assert mgr.resolve(b.slug) == "b"
    assert mgr.port_for("b") == b.port


def test_empty_base_host_fail_closes() -> None:
    mgr = _mgr(base_host="")
    assert mgr.enabled is False
    with pytest.raises(PreviewError, match="not enabled"):
        mgr.ensure("s1")


def test_rotate_mints_a_new_slug_on_the_same_port() -> None:
    # A revoked share recipient may have kept the preview URL: rotating retires the old
    # slug while the dev server keeps its $PORT.
    mgr = _mgr()
    old = mgr.ensure("s1")
    other = mgr.ensure("s2")
    new = mgr.rotate("s1")
    assert new is not None and new.slug != old.slug and new.port == old.port
    assert mgr.resolve(old.slug) is None
    assert mgr.resolve(new.slug) == "s1"
    assert mgr.url("s1") == new.url == f"https://{new.slug}-preview.box.example.com"
    assert mgr.resolve(other.slug) == "s2"


def test_rotate_without_a_reservation_is_a_no_op() -> None:
    mgr = _mgr()
    assert mgr.rotate("s1") is None
    assert mgr.url("s1") is None
