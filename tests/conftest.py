"""Shared test fixtures.

API tests run the real application — real routing, real middleware, real error
handlers — with only the repository and database dependencies replaced. That way
a test exercises the same code path a live request takes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from nas.api import deps
from nas.core.config import Environment, Settings
from nas.core.security import GeneratedApiKey, Scope, generate_api_key
from nas.domain.entities import ApiKey
from nas.main import create_app
from tests.fakes import (
    FakeCredentialProvider,
    FakeDatabase,
    FakeSyncService,
    InMemoryApiKeyRepository,
    InMemorySwitchRepository,
    InMemorySyncRunRepository,
    InMemoryVlanRepository,
    make_switch,
    make_vlan,
)

# Never reachable; API tests must not open a connection. Integration tests use
# NAS_TEST_DATABASE_URL instead.
DUMMY_DATABASE_URL = "postgresql+asyncpg://nas:nas@127.0.0.1:1/nas_test"

ALL_SCOPES = frozenset(Scope.values())


def build_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": Environment.TEST,
        "database_url": DUMMY_DATABASE_URL,
        "log_format": "console",
        "log_level": "WARNING",
        "docs_enabled": True,
        "credentials_file": None,
        "allowed_ip_ranges": [],
        "trust_proxy_headers": False,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.fixture
def settings() -> Settings:
    return build_settings()


@pytest.fixture
def switch_repository() -> InMemorySwitchRepository:
    return InMemorySwitchRepository([make_switch()])


@pytest.fixture
def vlan_repository() -> InMemoryVlanRepository:
    return InMemoryVlanRepository([make_vlan()])


@pytest.fixture
def sync_run_repository() -> InMemorySyncRunRepository:
    return InMemorySyncRunRepository()


@pytest.fixture
def sync_service() -> FakeSyncService:
    return FakeSyncService()


@pytest.fixture
def credential_provider() -> FakeCredentialProvider:
    return FakeCredentialProvider({"juniper-core"})


@pytest.fixture
def generated_key() -> GeneratedApiKey:
    return generate_api_key()


@pytest.fixture
def api_key(generated_key: GeneratedApiKey) -> ApiKey:
    """A usable key carrying every scope."""
    return ApiKey(
        id=1,
        name="test-key",
        prefix=generated_key.prefix,
        key_hash=generated_key.key_hash,
        scopes=ALL_SCOPES,
        is_active=True,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


@pytest.fixture
def api_key_repository(api_key: ApiKey) -> InMemoryApiKeyRepository:
    return InMemoryApiKeyRepository([api_key])


@pytest.fixture
def app_factory(
    switch_repository: InMemorySwitchRepository,
    api_key_repository: InMemoryApiKeyRepository,
    vlan_repository: InMemoryVlanRepository,
    sync_run_repository: InMemorySyncRunRepository,
    sync_service: FakeSyncService,
    credential_provider: FakeCredentialProvider,
) -> Iterator[object]:
    """Returns a callable building an app with the given settings.

    Used by tests that need non-default configuration (e.g. an IP allowlist)
    without duplicating the wiring.
    """
    created: list[FastAPI] = []

    def factory(settings: Settings) -> FastAPI:
        application = create_app(settings)

        # Lifespan does not run under httpx's ASGITransport, so process-scoped
        # state is installed directly.
        application.state.database = FakeDatabase()
        application.state.credential_provider = credential_provider

        # SyncService normally comes from app.state — it owns a session factory and
        # is shared with the scheduler — so it is replaced on state, not overridden.
        application.state.sync_service = sync_service

        application.dependency_overrides[deps.get_app_settings] = lambda: settings
        application.dependency_overrides[deps.get_switch_repository] = lambda: switch_repository
        application.dependency_overrides[deps.get_api_key_repository] = lambda: api_key_repository
        application.dependency_overrides[deps.get_credential_provider] = lambda: credential_provider
        application.dependency_overrides[deps.get_vlan_repository] = lambda: vlan_repository
        application.dependency_overrides[deps.get_sync_run_repository] = lambda: sync_run_repository
        created.append(application)
        return application

    yield factory

    for application in created:
        application.dependency_overrides.clear()


@pytest.fixture
def app(app_factory: object, settings: Settings) -> FastAPI:
    return app_factory(settings)  # type: ignore[operator]


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Unauthenticated client."""
    transport = ASGITransport(app=app, client=("127.0.0.1", 1234))
    async with AsyncClient(transport=transport, base_url="http://nas.test") as http_client:
        yield http_client


@pytest.fixture
async def auth_client(app: FastAPI, generated_key: GeneratedApiKey) -> AsyncIterator[AsyncClient]:
    """Client presenting a valid, fully scoped API key."""
    transport = ASGITransport(app=app, client=("127.0.0.1", 1234))
    async with AsyncClient(
        transport=transport,
        base_url="http://nas.test",
        headers={"X-API-Key": generated_key.plaintext},
    ) as http_client:
        yield http_client
