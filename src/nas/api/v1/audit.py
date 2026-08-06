"""Reading the audit trail.

Read-only by design. There is no endpoint that writes an entry directly — entries
are a side effect of the action they describe — and none that amends or deletes one.
Retention belongs to database administration, not to the API.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from nas.api.deps import get_audit_repository, require_scopes
from nas.api.v1.schemas import (
    ERROR_RESPONSES,
    AuditEntryResponse,
    PaginatedResponse,
    PaginationMeta,
)
from nas.core.security import Scope
from nas.domain.enums import AuditAction, AuditOutcome
from nas.domain.pagination import MAX_PAGE_SIZE, PageRequest
from nas.repositories.protocols import AuditFilters, AuditRepository

router = APIRouter(prefix="/audit", tags=["audit"], responses=ERROR_RESPONSES)


@router.get(
    "",
    response_model=PaginatedResponse[AuditEntryResponse],
    summary="The audit trail, newest first",
    description=(
        "Who triggered what, and when. Covers actions that reach a device or change "
        "state, plus calls rejected for insufficient scope — not routine reads, "
        "which would bury those events under ordinary traffic.\n\n"
        "**`actor` is asserted by the caller, not verified by NAS.** NAS "
        "authenticates the API key; the human identity is forwarded by the calling "
        "application. Read `actor` and `api_key_name` together.\n\n"
        "Requires the `audit:read` scope, which is deliberately not part of the "
        "read-only bundle issued to ClientManager."
    ),
    dependencies=[Depends(require_scopes(Scope.AUDIT_READ))],
)
async def list_audit_entries(
    repository: Annotated[AuditRepository, Depends(get_audit_repository)],
    action: Annotated[AuditAction | None, Query(description="Exact action match.")] = None,
    outcome: Annotated[AuditOutcome | None, Query()] = None,
    actor: Annotated[
        str | None, Query(description="Case-insensitive exact match on the actor.")
    ] = None,
    since: Annotated[
        datetime | None, Query(description="Only entries at or after this instant.")
    ] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
) -> PaginatedResponse[AuditEntryResponse]:
    result = await repository.list(
        filters=AuditFilters(action=action, outcome=outcome, actor=actor, since=since),
        page_request=PageRequest(page=page, page_size=page_size),
    )
    return PaginatedResponse[AuditEntryResponse](
        data=[AuditEntryResponse.from_entity(item) for item in result.items],
        pagination=PaginationMeta.from_page(result),
    )
