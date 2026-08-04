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

from nas.domain.entities import SyncRun, SyncRunSwitch, Vlan, VlanInterface
from nas.domain.enums import (
    CredentialStatus,
    InterfaceMode,
    ReachabilityState,
    SwitchSyncOutcome,
    SyncStatus,
    SyncTrigger,
    Vendor,
    VlanAvailability,
    VlanState,
)
from nas.domain.pagination import Page
from nas.services.switches import SwitchView
from nas.services.vlans import VlanLookup, VlanUsage


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


# ── VLANs ─────────────────────────────────────────────────────────────────────
class VlanInterfaceResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = Field(examples=["ge-0/0/12.0"])
    mode: InterfaceMode

    @classmethod
    def from_entity(cls, entity: VlanInterface) -> VlanInterfaceResponse:
        return cls(name=entity.name, mode=entity.mode)


class VlanResponse(BaseModel):
    """A VLAN as discovered on one switch."""

    model_config = ConfigDict(frozen=True)

    id: int = Field(description="Record id. Not the 802.1Q tag — that is vlan_id.")
    vlan_id: int = Field(description="The 802.1Q tag.", examples=[110])
    switch_id: int
    switch_name: str | None
    switch_site: str | None
    name: str | None = Field(examples=["sip-safaricom"])
    description: str | None
    l3_interface: str | None = Field(examples=["irb.110"])
    vxlan_vni: int | None
    state: VlanState
    interfaces: list[VlanInterfaceResponse]
    interface_count: int
    first_seen_at: datetime
    last_seen_at: datetime = Field(
        description="When this VLAN was last actually observed on the device."
    )
    last_synced_at: datetime = Field(
        description=(
            "When the switch was last polled successfully. Later than last_seen_at "
            "means the switch was reachable but no longer reports this VLAN."
        )
    )

    @classmethod
    def from_entity(cls, entity: Vlan) -> VlanResponse:
        return cls(
            id=entity.id,
            vlan_id=entity.vlan_id,
            switch_id=entity.switch_id,
            switch_name=entity.switch_name,
            switch_site=entity.switch_site,
            name=entity.name,
            description=entity.description,
            l3_interface=entity.l3_interface,
            vxlan_vni=entity.vxlan_vni,
            state=entity.state,
            interfaces=[VlanInterfaceResponse.from_entity(i) for i in entity.interfaces],
            interface_count=entity.interface_count,
            first_seen_at=entity.first_seen_at,
            last_seen_at=entity.last_seen_at,
            last_synced_at=entity.last_synced_at,
        )


class VlanUsageResponse(BaseModel):
    """One switch on which a VLAN id is configured."""

    model_config = ConfigDict(frozen=True)

    switch_id: int
    switch_name: str | None
    switch_site: str | None
    state: VlanState
    vlan: VlanResponse

    @classmethod
    def from_usage(cls, usage: VlanUsage) -> VlanUsageResponse:
        return cls(
            switch_id=usage.switch_id,
            switch_name=usage.switch_name,
            switch_site=usage.switch_site,
            state=usage.state,
            vlan=VlanResponse.from_entity(usage.vlan),
        )


class VlanLookupResponse(BaseModel):
    """The answer to "is this VLAN free, and if not, who has it and where".

    `availability` is derived from current records, never stored: `available` means
    no active record exists on any switch in the inventory.

    **Read `is_stale` before trusting `available`.** The switches remain
    authoritative; NAS holds a synchronised cache. If the data is stale, or if a
    switch failed in the last run, an "available" verdict may be wrong.
    """

    model_config = ConfigDict(frozen=True)

    vlan_id: int
    availability: VlanAvailability
    is_available: bool
    switch_count: int = Field(description="Number of switches actively using this tag.")
    active_usages: list[VlanUsageResponse]
    historic_usages: list[VlanUsageResponse] = Field(
        description="Records now marked missing — this tag was here before."
    )
    data_as_of: datetime | None = Field(
        description="Completion time of the most recent sync. Null if never synced."
    )
    is_stale: bool = Field(
        description="True when the underlying data is older than the staleness threshold."
    )

    @classmethod
    def from_lookup(cls, lookup: VlanLookup) -> VlanLookupResponse:
        return cls(
            vlan_id=lookup.vlan_id,
            availability=lookup.availability,
            is_available=lookup.is_available,
            switch_count=lookup.switch_count,
            active_usages=[VlanUsageResponse.from_usage(u) for u in lookup.active_usages],
            historic_usages=[VlanUsageResponse.from_usage(u) for u in lookup.historic_usages],
            data_as_of=lookup.data_as_of,
            is_stale=lookup.is_stale,
        )


# ── Synchronisation ───────────────────────────────────────────────────────────
class SyncRunSwitchResponse(BaseModel):
    """Per-switch outcome within a run, so a partial run is explainable."""

    model_config = ConfigDict(frozen=True)

    switch_id: int | None
    switch_name: str
    outcome: SwitchSyncOutcome
    vlans_discovered: int
    vlans_created: int
    vlans_updated: int
    vlans_unchanged: int
    vlans_marked_missing: int
    duration_ms: int | None
    error_message: str | None

    @classmethod
    def from_entity(cls, entity: SyncRunSwitch) -> SyncRunSwitchResponse:
        return cls(
            switch_id=entity.switch_id,
            switch_name=entity.switch_name,
            outcome=entity.outcome,
            vlans_discovered=entity.vlans_discovered,
            vlans_created=entity.vlans_created,
            vlans_updated=entity.vlans_updated,
            vlans_unchanged=entity.vlans_unchanged,
            vlans_marked_missing=entity.vlans_marked_missing,
            duration_ms=entity.duration_ms,
            error_message=entity.error_message,
        )


class SyncRunResponse(BaseModel):
    """One synchronisation pass.

    `status` is `partial` when some switches succeeded and others failed — treat
    that as "the data is only as fresh as `switch_results` says it is".
    """

    model_config = ConfigDict(frozen=True)

    id: int
    trigger: SyncTrigger
    status: SyncStatus
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    correlation_id: str = Field(description="Quote this when reporting a problem.")
    switches_total: int
    switches_succeeded: int
    switches_failed: int
    switches_skipped: int
    vlans_discovered: int
    vlans_created: int
    vlans_updated: int
    vlans_unchanged: int
    vlans_marked_missing: int
    error_message: str | None
    switch_results: list[SyncRunSwitchResponse]

    @classmethod
    def from_entity(cls, entity: SyncRun) -> SyncRunResponse:
        return cls(
            id=entity.id,
            trigger=entity.trigger,
            status=entity.status,
            started_at=entity.started_at,
            finished_at=entity.finished_at,
            duration_ms=entity.duration_ms,
            correlation_id=entity.correlation_id,
            switches_total=entity.switches_total,
            switches_succeeded=entity.switches_succeeded,
            switches_failed=entity.switches_failed,
            switches_skipped=entity.switches_skipped,
            vlans_discovered=entity.vlans_discovered,
            vlans_created=entity.vlans_created,
            vlans_updated=entity.vlans_updated,
            vlans_unchanged=entity.vlans_unchanged,
            vlans_marked_missing=entity.vlans_marked_missing,
            error_message=entity.error_message,
            switch_results=[SyncRunSwitchResponse.from_entity(r) for r in entity.switch_results],
        )


class SyncStatusResponse(BaseModel):
    """Current synchronisation state — the endpoint a dashboard polls."""

    model_config = ConfigDict(frozen=True)

    is_running: bool
    latest_run: SyncRunResponse | None
    never_run: bool = Field(description="True when no sync has ever been recorded.")


class SyncTriggerRequest(BaseModel):
    """Optional body for POST /sync."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    switch_ids: list[int] | None = Field(
        default=None,
        description="Restrict the run to these switches. Omit to sync all of them.",
        examples=[[1, 2]],
    )
