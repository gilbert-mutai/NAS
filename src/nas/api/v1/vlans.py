"""VLAN discovery endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query

from nas.api.deps import get_vlan_service, require_scopes
from nas.api.v1.schemas import (
    ERROR_RESPONSES,
    PaginatedResponse,
    PaginationMeta,
    VlanLookupResponse,
    VlanResponse,
)
from nas.core.security import Scope
from nas.domain.enums import VlanState
from nas.domain.pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, PageRequest
from nas.repositories.protocols import VlanFilters
from nas.services.vlans import VlanService

router = APIRouter(prefix="/vlans", tags=["vlans"], responses=ERROR_RESPONSES)


@router.get(
    "",
    response_model=PaginatedResponse[VlanResponse],
    summary="Search discovered VLANs",
    description=(
        "One row per VLAN **per switch** — the same 802.1Q tag on two switches is "
        "two records. To ask whether a tag is free, use "
        "`/vlans/lookup/{vlan_id}` instead, which aggregates across switches and "
        "returns an availability verdict."
    ),
    dependencies=[Depends(require_scopes(Scope.VLANS_READ))],
)
async def search_vlans(
    service: Annotated[VlanService, Depends(get_vlan_service)],
    vlan_id: Annotated[int | None, Query(ge=1, le=4094, description="Filter by 802.1Q tag")] = None,
    switch_id: Annotated[int | None, Query(ge=1, description="Filter by switch")] = None,
    site: Annotated[str | None, Query(max_length=100, description="Filter by site")] = None,
    state: Annotated[VlanState | None, Query(description="Filter by record state")] = None,
    q: Annotated[
        str | None,
        Query(
            max_length=200,
            description="Free text over VLAN name, description and member interface name",
        ),
    ] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> PaginatedResponse[VlanResponse]:
    result = await service.search(
        filters=VlanFilters(
            vlan_id=vlan_id,
            switch_id=switch_id,
            site=site,
            state=state,
            search=q,
        ),
        page_request=PageRequest(page=page, page_size=page_size),
    )
    return PaginatedResponse[VlanResponse](
        data=[VlanResponse.from_entity(item) for item in result.items],
        pagination=PaginationMeta.from_page(result),
    )


@router.get(
    "/lookup/{vlan_id}",
    response_model=VlanLookupResponse,
    summary="Is this VLAN free, and if not, who has it and where",
    description=(
        "Aggregates one 802.1Q tag across every switch and returns an availability "
        "verdict in a single call — the question a support engineer previously "
        "answered by SSHing into switches.\n\n"
        "`availability` is derived from current records, never stored. **Check "
        "`is_stale` before trusting an `available` verdict**: the switches remain "
        "authoritative and NAS holds a synchronised cache. Tags 0 and 4095 are "
        "reported `reserved` rather than available."
    ),
    dependencies=[Depends(require_scopes(Scope.VLANS_READ))],
)
async def lookup_vlan(
    service: Annotated[VlanService, Depends(get_vlan_service)],
    vlan_id: Annotated[
        int,
        Path(
            ge=0,
            le=4095,
            description="802.1Q tag. 0 and 4095 are reserved and reported as such.",
        ),
    ],
) -> VlanLookupResponse:
    return VlanLookupResponse.from_lookup(await service.lookup(vlan_id))


@router.get(
    "/{vlan_record_id}",
    response_model=VlanResponse,
    summary="Retrieve one VLAN record",
    description=(
        "Takes the **record id**, not the 802.1Q tag. Look up a tag with `/vlans/lookup/{vlan_id}`."
    ),
    dependencies=[Depends(require_scopes(Scope.VLANS_READ))],
)
async def get_vlan(
    service: Annotated[VlanService, Depends(get_vlan_service)],
    vlan_record_id: Annotated[int, Path(ge=1, description="VLAN record id")],
) -> VlanResponse:
    return VlanResponse.from_entity(await service.get_vlan(vlan_record_id))
