"""API v1 data transfer objects.

These are the service's public contract. They are defined separately from the
domain entities on purpose: a field can be added to an entity without silently
appearing in the API, and a rename inside the domain cannot break a consumer.

Response conventions:
  * collections  -> {"data": [...], "pagination": {...}}
  * single item   -> the object itself
  * any error     -> {"error": {"code", "message", "request_id", "details?"}}
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from nas.domain.enums import CredentialStatus, ReachabilityState, Vendor
from nas.domain.pagination import Page
from nas.services.switches import SwitchView


class PaginationMeta(BaseModel):
    model_config = ConfigDict(frozen=True)

    page: int = Field(examples=[1])
    page_size: int = Field(examples=[50])
    total: int = Field(examples=[3])
    total_pages: int = Field(examples=[1])
    has_next: bool
    has_previous: bool

    @classmethod
    def from_page(cls, page: Page[object]) -> PaginationMeta:
        return cls(
            page=page.page,
            page_size=page.page_size,
            total=page.total,
            total_pages=page.total_pages,
            has_next=page.has_next,
            has_previous=page.has_previous,
        )


class PaginatedResponse[T](BaseModel):
    model_config = ConfigDict(frozen=True)

    data: list[T]
    pagination: PaginationMeta


class SwitchResponse(BaseModel):
    """A managed network device.

    Contains no credential material. ``credential_status`` reports only whether
    the device's credential reference currently resolves inside NAS.
    """

    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={
            "example": {
                "id": 1,
                "name": "adc-core-sw1",
                "hostname": "10.20.0.11",
                "port": 22,
                "vendor": "juniper",
                "vendor_label": "Juniper",
                "credential_ref": "juniper-core",
                "credential_status": "resolved",
                "site": "ADC NBO",
                "environment": "production",
                "model": None,
                "os_version": None,
                "description": "Core switch, SIP VLAN trunk",
                "is_active": True,
                "reachability": "unknown",
                "last_health_check": None,
                "health_error": None,
                "created_at": "2026-08-03T09:00:00Z",
                "updated_at": "2026-08-03T09:00:00Z",
            }
        },
    )

    id: int
    name: str
    hostname: str
    port: int
    vendor: Vendor
    vendor_label: str
    credential_ref: str
    credential_status: CredentialStatus
    site: str | None
    environment: str | None
    model: str | None
    os_version: str | None
    description: str | None
    is_active: bool
    reachability: ReachabilityState
    last_health_check: datetime | None
    health_error: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_view(cls, view: SwitchView) -> SwitchResponse:
        switch = view.switch
        return cls(
            id=switch.id,
            name=switch.name,
            hostname=switch.hostname,
            port=switch.port,
            vendor=switch.vendor,
            vendor_label=switch.vendor.label,
            credential_ref=switch.credential_ref,
            credential_status=view.credential_status,
            site=switch.site,
            environment=switch.environment,
            model=switch.model,
            os_version=switch.os_version,
            description=switch.description,
            is_active=switch.is_active,
            reachability=switch.reachability,
            last_health_check=switch.last_health_check,
            health_error=switch.health_error,
            created_at=switch.created_at,
            updated_at=switch.updated_at,
        )


class ErrorDetail(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str = Field(examples=["SWITCH_NOT_FOUND"])
    message: str = Field(examples=["No switch exists with that identifier."])
    request_id: str | None = Field(default=None, examples=["9f2c1d7e4b8a4f0c9d3e5a6b7c8d9e0f"])
    details: dict[str, object] | None = None


class ErrorResponse(BaseModel):
    """The single error shape returned by every endpoint."""

    model_config = ConfigDict(frozen=True)

    error: ErrorDetail


# Reused across routes so the OpenAPI document shows the real error contract.
ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse, "description": "Missing, malformed or unusable API key"},
    403: {"model": ErrorResponse, "description": "IP not allowed, or insufficient scope"},
    404: {"model": ErrorResponse, "description": "Resource does not exist"},
    422: {"model": ErrorResponse, "description": "Request validation failed"},
    500: {"model": ErrorResponse, "description": "Unexpected internal error"},
}
