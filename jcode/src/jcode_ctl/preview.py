"""Shared preview error type.

The per-session TryCloudflare quick-tunnel adapter that used to live here was retired
at the Wave P5b cutover (docs/JCODE_PREVIEW_HOST_PLAN.md) in favour of the host-served
per-session preview (``host_preview.py`` + ``preview_proxy.py``). This module keeps only
the error the preview surface raises, imported by both the allocator and the app.
"""

from __future__ import annotations


class PreviewError(RuntimeError):
    """A preview couldn't be served (e.g. web preview is disabled)."""
