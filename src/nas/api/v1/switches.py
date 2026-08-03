"""Switch inventory endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query

from nas.api.deps import get_switch_service, require_scopes
from nas.api.v1.schemas import (
    ERROR_RESPONSES,
    PaginatedResponse,
    PaginationMeta,
    SwitchResponse,
)
from nas.core.security import Scope
from nas.domain.enums import Vendor
from nas.domain.pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, PageRequest
from nas.repositories.protocols import SwitchFilters
from nas.services.switches import SwitchService

router = APIRouter(prefix="/switches", tags=["switches"], responses=ERROR_RESPONSES)


@router.get(
    "",
    response_model=PaginatedResponse[SwitchResponse],
    summary="List managed switches",
    description=(
        "Returns the switch inventory NAS synchronises from. Never returns "
        "credential material — only whether each switch's credential reference "
        "currently resolves."
    ),
    dependencies=[Depends(require_scopes(Scope.SWITCHES_READ))],
)
async def list_switches(
    service: Annotated[SwitchService, Depends(get_switch_service)],
    vendor: Annotated[Vendor | None, Query(description="Filter by device vendor")] = None,
    site: Annotated[str | None, Query(max_length=100, description="Filter by site")] = None,
    environment: Annotated[
        str | None, Query(max_length=50, description="Filter by environment label")
    ] = None,
    is_active: Annotated[bool | None, Query(description="Filter by active flag")] = None,
    q: Annotated[
        str | None,
        Query(max_length=200, description="Free-text search over name, hostname, description"),
    ] = None,
    page: Annotated[int, Query(ge=1, description="1-indexed page number")] = 1,
    page_size: Annotated[
        int, Query(ge=1, le=MAX_PAGE_SIZE, description="Results per page")
    ] = DEFAULT_PAGE_SIZE,
) -> PaginatedResponse[SwitchResponse]:
    result = await service.list_switches(
        filters=SwitchFilters(
            vendor=vendor,
            site=site,
            environment=environment,
            is_active=is_active,
            search=q,
        ),
        page_request=PageRequest(page=page, page_size=page_size),
    )
    return PaginatedResponse[SwitchResponse](
        data=[SwitchResponse.from_view(view) for view in result.items],
        pagination=PaginationMeta.from_page(result),
    )


@router.get(
    "/{switch_id}",
    response_model=SwitchResponse,
    summary="Retrieve one switch",
    dependencies=[Depends(require_scopes(Scope.SWITCHES_READ))],
)
async def get_switch(
    service: Annotated[SwitchService, Depends(get_switch_service)],
    switch_id: Annotated[int, Path(ge=1, description="Switch identifier")],
) -> SwitchResponse:
    view = await service.get_switch(switch_id)
    return SwitchResponse.from_view(view)
