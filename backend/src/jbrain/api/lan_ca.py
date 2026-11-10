"""The box's LAN root certificate as a download (docs/runbooks/LOCAL_ACCESS.md).

A phone that trusts this root opens `https://jbrain.local` and the GrowBot's `:8444` site with
no certificate warning. Before this route the only way to get the file off the box was a
`docker cp`, a terminal step the owner cannot take (CLAUDE.md #10). It is the public half of
Caddy's internal CA, the same bytes `api/endpoint.py` writes into a panel and a robot; it is
still owner-only, since nothing else on the internet has a reason to ask this box for it.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response

from jbrain.api import endpoint
from jbrain.api.deps import OwnerDep

router = APIRouter(prefix="/lan", tags=["lan"])

FILENAME = "jbrain-box-root.crt"


@router.get("/root.crt")
async def root_certificate(_owner: OwnerDep) -> Response:
    pem = endpoint._lan_ca()
    if not pem:
        raise HTTPException(
            status_code=404,
            detail="This box has no LAN certificate to download (no LAN site, or unreadable).",
        )
    return Response(
        content=pem.encode(),
        media_type="application/x-x509-ca-cert",
        headers={
            "Content-Disposition": f'attachment; filename="{FILENAME}"',
            "Cache-Control": "no-store",
        },
    )
