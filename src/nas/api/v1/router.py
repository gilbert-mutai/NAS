"""API v1 router aggregation.

Milestone 2 adds the ``vlans`` and ``sync`` routers here. Versioning is by URL
prefix, so a future v2 can coexist with v1 while consumers migrate.
"""

from __future__ import annotations

from fastapi import APIRouter

from nas.api.v1 import switches

api_router = APIRouter()
api_router.include_router(switches.router)
