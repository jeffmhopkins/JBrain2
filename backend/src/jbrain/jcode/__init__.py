"""Client to the jcode control server (code mode, Wave J2).

The api never runs the coding agent itself — it proxies an owner's session to the
internal jcode control server (docs/archive/JCODE_PLAN.md). This package is the
thin httpx transport to that server, mirroring `jbrain.web.search.SearxngClient`:
the only place an HTTP request reaches it, bearer-authed, graceful-degrade when
unconfigured.
"""

from jbrain.jcode.client import (
    FakeJcodeClient,
    JcodeApi,
    JcodeClient,
    JcodeError,
)

__all__ = ["FakeJcodeClient", "JcodeApi", "JcodeClient", "JcodeError"]
