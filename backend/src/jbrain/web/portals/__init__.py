"""Government-portal search resolvers (DYNAMIC_PORTAL_FETCH_PLAN.md, "dinosaurs").

Adding a portal is a one-file change: write the adapter under `jbrain/web/portals/`, then
construct it in `main.py` alongside the others and it joins the `portal_search` registry.
"""

from jbrain.web.portals.base import (
    PortalResolver,
    PortalResult,
    PortalRow,
    advertised_capabilities,
    select_resolvers,
)
from jbrain.web.portals.fl_dfs import FlDfsResolver
from jbrain.web.portals.fl_sunbiz import FlSunbizResolver

__all__ = [
    "FlDfsResolver",
    "FlSunbizResolver",
    "PortalResolver",
    "PortalResult",
    "PortalRow",
    "advertised_capabilities",
    "select_resolvers",
]
