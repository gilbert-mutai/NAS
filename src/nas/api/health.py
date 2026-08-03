"""Health, liveness and readiness endpoints.

Three separate probes because they answer three different questions:

* ``/live``  — is the process running? Never touches a dependency. A failure here
  means the container should be restarted.
* ``/ready`` — can this instance serve traffic? Checks the database. A failure
  means take it out of the load balancer but do **not** restart it.
* ``/health`` — a human/monitoring summary combining both.

These are unauthenticated and exempt from the IP allowlist so that probes work
without being whitelisted. They expose no switch, customer or credential data.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict

from nas import __version__
from nas.api.deps import get_app_settings, get_database
from nas.core.config import Environment, Settings
from nas.db.session import Database

router = APIRouter(tags=["health"])


class LivenessResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["alive"] = "alive"
    service: str
    version: str


class ReadinessResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["ready", "not_ready"]
    database: Literal["ok", "unavailable"]


class HealthResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["ok", "degraded"]
    service: str
    version: str
    environment: Environment
    database: Literal["ok", "unavailable"]


@router.get("/live", response_model=LivenessResponse, summary="Liveness probe")
async def live(
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> LivenessResponse:
    return LivenessResponse(service=settings.service_name, version=__version__)


@router.get("/ready", response_model=ReadinessResponse, summary="Readiness probe")
async def ready(
    response: Response,
    database: Annotated[Database, Depends(get_database)],
) -> ReadinessResponse:
    db_ok = await database.check()
    if not db_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(
        status="ready" if db_ok else "not_ready",
        database="ok" if db_ok else "unavailable",
    )


@router.get("/health", response_model=HealthResponse, summary="Health summary")
async def health(
    response: Response,
    settings: Annotated[Settings, Depends(get_app_settings)],
    database: Annotated[Database, Depends(get_database)],
) -> HealthResponse:
    db_ok = await database.check()
    if not db_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        status="ok" if db_ok else "degraded",
        service=settings.service_name,
        version=__version__,
        environment=settings.environment,
        database="ok" if db_ok else "unavailable",
    )
