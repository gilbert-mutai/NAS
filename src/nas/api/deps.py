"""FastAPI dependency wiring.

This module is the only place where the outer layers are assembled: sessions are
created here, repositories are constructed from sessions, and services are
constructed from repositories. Routers depend on services, never on SQLAlchemy.

The repository providers are also the override points for tests — substituting an
in-memory fake here lets the entire API surface be tested without a database.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Request, Security
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession

from nas.core.config import Settings, get_settings
from nas.core.credentials import CredentialProvider, NullCredentialProvider
from nas.core.logging import bind_request_context
from nas.core.security import Scope
from nas.db.session import Database
from nas.domain.entities import ApiKey
from nas.repositories.api_keys import SqlAlchemyApiKeyRepository
from nas.repositories.protocols import (
    ApiKeyRepository,
    SwitchRepository,
    SyncRunRepository,
    VlanRepository,
)
from nas.repositories.switches import SqlAlchemySwitchRepository
from nas.repositories.sync_runs import SqlAlchemySyncRunRepository
from nas.repositories.vlans import SqlAlchemyVlanRepository
from nas.services.auth import AuthenticationService
from nas.services.switches import SwitchService
from nas.services.sync import SyncService
from nas.services.vlans import VlanService

API_KEY_HEADER_NAME = "X-API-Key"

# auto_error=False so a missing header reaches our own handler and produces the
# standard error envelope rather than FastAPI's default {"detail": ...} body.
api_key_header = APIKeyHeader(name=API_KEY_HEADER_NAME, auto_error=False)


def get_app_settings() -> Settings:
    return get_settings()


def get_database(request: Request) -> Database:
    database: Database = request.app.state.database
    return database


def get_credential_provider(request: Request) -> CredentialProvider:
    provider: CredentialProvider = getattr(
        request.app.state, "credential_provider", NullCredentialProvider()
    )
    return provider


async def get_session(
    database: Annotated[Database, Depends(get_database)],
) -> AsyncIterator[AsyncSession]:
    """One session and one transaction per request.

    Commits when the handler returns, rolls back if it raises.

    Note that ``api_keys.last_used_at`` is *not* part of that transaction: it is
    committed immediately during authentication so its row lock is not held for
    the request's lifetime. See SqlAlchemyApiKeyRepository.mark_used.
    """
    async with database.session() as session:
        yield session


def get_switch_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SwitchRepository:
    return SqlAlchemySwitchRepository(session)


def get_api_key_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ApiKeyRepository:
    return SqlAlchemyApiKeyRepository(session)


def get_auth_service(
    repository: Annotated[ApiKeyRepository, Depends(get_api_key_repository)],
) -> AuthenticationService:
    return AuthenticationService(repository)


def get_switch_service(
    repository: Annotated[SwitchRepository, Depends(get_switch_repository)],
    credential_provider: Annotated[CredentialProvider, Depends(get_credential_provider)],
) -> SwitchService:
    return SwitchService(repository=repository, credential_provider=credential_provider)


def require_scopes(*scopes: Scope) -> Callable[..., Awaitable[ApiKey]]:
    """Build a dependency that authenticates the caller and asserts its scopes.

    Used as ``Depends(require_scopes(Scope.SWITCHES_READ))`` on each route, so the
    privilege a route needs is declared at the route itself.
    """
    required = frozenset(scope.value for scope in scopes)

    async def dependency(
        request: Request,
        presented_key: Annotated[str | None, Security(api_key_header)],
        auth: Annotated[AuthenticationService, Depends(get_auth_service)],
    ) -> ApiKey:
        api_key = await auth.authenticate(presented_key)
        auth.authorize(api_key, required)
        request.state.api_key = api_key
        # From here on, every log line in this request identifies the caller.
        bind_request_context(api_key_id=api_key.id)
        return api_key

    return dependency


def get_vlan_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> VlanRepository:
    return SqlAlchemyVlanRepository(session)


def get_sync_run_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SyncRunRepository:
    return SqlAlchemySyncRunRepository(session)


def get_vlan_service(
    repository: Annotated[VlanRepository, Depends(get_vlan_repository)],
    sync_runs: Annotated[SyncRunRepository, Depends(get_sync_run_repository)],
) -> VlanService:
    return VlanService(repository=repository, sync_runs=sync_runs)


def get_sync_service(request: Request) -> SyncService:
    """Return the process-wide SyncService.

    Built once at startup rather than per request: it owns a session *factory*
    (it holds the advisory lock on one connection while each switch commits on
    its own) and is shared with the scheduler, so the API and the scheduler
    contend for the same lock instead of running two independent implementations.
    """
    service: SyncService = request.app.state.sync_service
    return service
