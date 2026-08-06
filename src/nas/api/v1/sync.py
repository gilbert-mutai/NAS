"""Synchronisation endpoints."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Path, Query, Response, status

from nas.api.deps import (
    RequestContext,
    audited_context,
    get_audit_service,
    get_sync_run_repository,
    get_sync_service,
    require_scopes,
)
from nas.api.v1.schemas import (
    ERROR_RESPONSES,
    PaginatedResponse,
    PaginationMeta,
    SyncRunResponse,
    SyncStatusResponse,
    SyncTriggerRequest,
)
from nas.core.errors import ConflictError, NotFoundError
from nas.core.security import Scope
from nas.domain.enums import AuditAction, AuditOutcome, SyncTrigger
from nas.domain.pagination import MAX_PAGE_SIZE, PageRequest
from nas.repositories.protocols import SyncRunRepository
from nas.services.audit import AuditService
from nas.services.sync import SyncService

router = APIRouter(prefix="/sync", tags=["sync"], responses=ERROR_RESPONSES)


@router.post(
    "",
    response_model=SyncRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Trigger a synchronisation",
    description=(
        "Polls the configured switches, reconciles what they report against the "
        "database, and returns the completed run.\n\n"
        "Runs are serialised by a PostgreSQL advisory lock: if one is already in "
        "progress this returns **409 CONFLICT** rather than queueing, so a "
        "double-clicked 'Sync Now' cannot start two runs.\n\n"
        "A run where some switches fail completes with status `partial`. **A switch "
        "that cannot be read never has its VLANs marked missing** — its stored data "
        "is left exactly as the last successful run left it."
    ),
    responses={
        **ERROR_RESPONSES,
        409: {"description": "A synchronisation is already in progress"},
    },
)
async def trigger_sync(
    service: Annotated[SyncService, Depends(get_sync_service)],
    context: Annotated[RequestContext, Depends(audited_context(Scope.SYNC_WRITE))],
    audit: Annotated[AuditService, Depends(get_audit_service)],
    payload: Annotated[SyncTriggerRequest | None, Body()] = None,
) -> SyncRunResponse:
    """Trigger a run and record who did it.

    This is the only Phase 1 endpoint that reaches a switch, so it is the one that
    has to be answerable for. Every outcome is audited — including the 409, because
    a rejected attempt is as informative as an accepted one, and including a crash,
    because "a sync was started and never finished" is exactly what an operator
    needs to see.
    """
    requested_switch_ids = payload.switch_ids if payload else None
    detail: dict[str, Any] = {"switch_ids": requested_switch_ids}

    async def audit_outcome(
        outcome: AuditOutcome,
        *,
        target_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        await audit.record(
            action=AuditAction.SYNC_TRIGGER,
            outcome=outcome,
            api_key_id=context.api_key.id,
            api_key_name=context.api_key.name,
            actor=context.actor,
            source_ip=context.source_ip,
            correlation_id=context.correlation_id,
            target_type="sync_run" if target_id else None,
            target_id=target_id,
            detail=detail | (extra or {}),
        )

    try:
        run = await service.run(
            trigger=SyncTrigger.MANUAL,
            switch_ids=requested_switch_ids,
        )
    except ConflictError as exc:
        # Not an authorisation failure — the caller was entitled to ask, and a run
        # was already under way. Recorded so a burst of rejected clicks is visible.
        await audit_outcome(AuditOutcome.ERROR, extra={"rejected": exc.code.value})
        raise
    except Exception as exc:
        await audit_outcome(AuditOutcome.ERROR, extra={"error": type(exc).__name__})
        raise

    await audit_outcome(
        AuditOutcome.SUCCESS,
        target_id=str(run.id),
        extra={"status": run.status.value, "switches_total": run.switches_total},
    )
    return SyncRunResponse.from_entity(run)


@router.get(
    "/status",
    response_model=SyncStatusResponse,
    summary="Current synchronisation state",
    description="The endpoint to poll for a dashboard. Reports the most recent run.",
    dependencies=[Depends(require_scopes(Scope.SYNC_READ))],
)
async def sync_status(
    repository: Annotated[SyncRunRepository, Depends(get_sync_run_repository)],
) -> SyncStatusResponse:
    latest = await repository.latest()
    return SyncStatusResponse(
        is_running=latest.is_running if latest else False,
        latest_run=SyncRunResponse.from_entity(latest) if latest else None,
        never_run=latest is None,
    )


@router.get(
    "/runs",
    response_model=PaginatedResponse[SyncRunResponse],
    summary="Synchronisation history",
    description=("Newest first. Per-switch detail is omitted here — fetch a single run for that."),
    dependencies=[Depends(require_scopes(Scope.SYNC_READ))],
)
async def list_sync_runs(
    repository: Annotated[SyncRunRepository, Depends(get_sync_run_repository)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 20,
) -> PaginatedResponse[SyncRunResponse]:
    result = await repository.list(page_request=PageRequest(page=page, page_size=page_size))
    return PaginatedResponse[SyncRunResponse](
        data=[SyncRunResponse.from_entity(item) for item in result.items],
        pagination=PaginationMeta.from_page(result),
    )


@router.get(
    "/runs/{sync_run_id}",
    response_model=SyncRunResponse,
    summary="One synchronisation run, with per-switch detail",
    dependencies=[Depends(require_scopes(Scope.SYNC_READ))],
)
async def get_sync_run(
    repository: Annotated[SyncRunRepository, Depends(get_sync_run_repository)],
    sync_run_id: Annotated[int, Path(ge=1)],
    response: Response,
) -> SyncRunResponse:
    run = await repository.get_by_id(sync_run_id)
    if run is None:
        raise NotFoundError(
            f"No synchronisation run exists with id {sync_run_id}.",
            details={"sync_run_id": sync_run_id},
        )
    if run.is_running:
        # An in-progress run's counters are not final; tell caches not to keep it.
        response.headers["Cache-Control"] = "no-store"
    return SyncRunResponse.from_entity(run)
