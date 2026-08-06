"""API v1 router aggregation.

Versioning is by URL prefix, so a future v2 can coexist with v1 while consumers
migrate.
"""

from __future__ import annotations

from fastapi import APIRouter

from nas.api.v1 import audit, switches, sync, vlans

api_router = APIRouter()
api_router.include_router(switches.router)
api_router.include_router(vlans.router)
api_router.include_router(sync.router)
api_router.include_router(audit.router)
