"""The audit trail: what gets written, and reading it back.

Covers the two things the API layer still owns: forwarding attribution to the
service, and auditing a scope denial (which happens during dependency resolution, so
the route never runs and cannot record it). Plus reading the trail back.

The sync audit *write* lives in SyncService — see
tests/integration/test_sync_service.py::TestEveryTriggerIsAudited, which proves every
trigger is recorded, not just the HTTP one. Transaction behaviour (an entry surviving
a request that rolls back) is in tests/integration/test_audit_repository.py.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from nas.core.security import Scope, generate_api_key
from nas.domain.entities import ApiKey, AuditEntry
from nas.domain.enums import AuditAction, AuditOutcome
from nas.services.audit import AuditContext
from tests.fakes import FakeAuditService, FakeSyncService, InMemoryAuditRepository

SYNC_PATH = "/api/v1/sync"
AUDIT_PATH = "/api/v1/audit"
ACTOR = "gilbert@angani.co"


class TestTheRouteForwardsAttribution:
    """What the route is responsible for now.

    The audit *write* moved into SyncService, so that every trigger — API, scheduler,
    CLI — is recorded at one choke point. What is left here is the route's actual job:
    supplying the context the service cannot know. Whether the entry then lands is the
    service's contract, proved against a real database in
    tests/integration/test_sync_service.py::TestEveryTriggerIsAudited.
    """

    async def test_the_actor_header_is_forwarded(
        self, auth_client: AsyncClient, sync_service: FakeSyncService
    ) -> None:
        response = await auth_client.post(SYNC_PATH, headers={"X-Actor": ACTOR})
        assert response.status_code == 202
        context = sync_service.calls[-1]["audit_context"]
        assert isinstance(context, AuditContext)
        assert context.actor == ACTOR

    async def test_the_authenticated_key_is_forwarded_alongside_the_actor(
        self, auth_client: AsyncClient, sync_service: FakeSyncService, api_key: ApiKey
    ) -> None:
        """Both, not either. The key is what NAS proved; the actor is what it was
        told. Forwarding only the actor would let the service record an unverified
        claim with nothing to weigh it against."""
        await auth_client.post(SYNC_PATH, headers={"X-Actor": ACTOR})
        context = sync_service.calls[-1]["audit_context"]
        assert isinstance(context, AuditContext)
        assert context.api_key_id == api_key.id
        assert context.api_key_name == api_key.name
        assert context.actor == ACTOR

    async def test_a_missing_actor_header_still_forwards_the_key(
        self, auth_client: AsyncClient, sync_service: FakeSyncService, api_key: ApiKey
    ) -> None:
        await auth_client.post(SYNC_PATH)
        context = sync_service.calls[-1]["audit_context"]
        assert isinstance(context, AuditContext)
        assert context.actor is None
        assert context.api_key_name == api_key.name

    async def test_the_source_ip_is_forwarded(
        self, auth_client: AsyncClient, sync_service: FakeSyncService
    ) -> None:
        await auth_client.post(SYNC_PATH)
        context = sync_service.calls[-1]["audit_context"]
        assert isinstance(context, AuditContext)
        assert context.source_ip == "127.0.0.1"

    async def test_the_request_id_becomes_the_runs_correlation_id(
        self, auth_client: AsyncClient, sync_service: FakeSyncService
    ) -> None:
        """One id spans the ClientManager request, NAS's logs, the sync_runs row and
        the audit entry. Passing it explicitly is what ties them together — without
        it the service would mint its own and the trail would not join up."""
        response = await auth_client.post(
            SYNC_PATH, headers={"X-Request-ID": "clientmanager-abc-123"}
        )
        assert sync_service.calls[-1]["correlation_id"] == "clientmanager-abc-123"
        assert response.headers["X-Request-ID"] == "clientmanager-abc-123"

    async def test_the_raw_header_is_forwarded_unsanitised(
        self, auth_client: AsyncClient, sync_service: FakeSyncService
    ) -> None:
        """Deliberate, and the reason sanitisation lives in AuditService rather than
        at the edge: one implementation covers the API, the CLI and any future caller.
        A route that cleaned the value itself would be a second place to keep correct.
        """
        await auth_client.post(SYNC_PATH, headers={"X-Actor": "gilbert@angani.co\r\n<script>"})
        context = sync_service.calls[-1]["audit_context"]
        assert isinstance(context, AuditContext)
        assert "<script>" in (context.actor or ""), "the route must not pre-clean"


class TestScopeDenialIsAudited:
    @staticmethod
    def _app_with_key(app_factory: object, settings: object, *scopes: Scope) -> tuple[FastAPI, str]:
        from nas.api import deps
        from tests.fakes import InMemoryApiKeyRepository

        generated = generate_api_key()
        key = ApiKey(
            id=7,
            name="narrow-key",
            prefix=generated.prefix,
            key_hash=generated.key_hash,
            scopes=frozenset(scope.value for scope in scopes),
            is_active=True,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        app: FastAPI = app_factory(settings)  # type: ignore[operator]
        app.dependency_overrides[deps.get_api_key_repository] = lambda: InMemoryApiKeyRepository(
            [key]
        )
        return app, generated.plaintext

    async def test_a_denial_is_recorded(
        self,
        app_factory: object,
        settings: object,
        audit_service: FakeAuditService,
    ) -> None:
        """A key reaching for a privilege it was not granted is worth a durable
        record, whether it is a misconfiguration or a probe.

        Recorded inside require_scopes rather than in the route, because the route
        never runs — the request is rejected during dependency resolution.
        """
        app, plaintext = self._app_with_key(app_factory, settings, Scope.SYNC_READ)
        async with AsyncClient(
            transport=ASGITransport(app=app, client=("127.0.0.1", 1234)),
            base_url="http://nas.test",
            headers={"X-API-Key": plaintext, "X-Actor": ACTOR},
        ) as client:
            response = await client.post(SYNC_PATH)

        assert response.status_code == 403
        entry = audit_service.only
        assert entry.action is AuditAction.AUTH_DENIED
        assert entry.outcome is AuditOutcome.DENIED
        assert entry.api_key_name == "narrow-key"
        assert entry.actor == ACTOR

    async def test_the_denial_records_what_was_missing_not_what_was_held(
        self,
        app_factory: object,
        settings: object,
        audit_service: FakeAuditService,
    ) -> None:
        """The row should not become an inventory of the key's privileges."""
        app, plaintext = self._app_with_key(app_factory, settings, Scope.SYNC_READ)
        async with AsyncClient(
            transport=ASGITransport(app=app, client=("127.0.0.1", 1234)),
            base_url="http://nas.test",
            headers={"X-API-Key": plaintext},
        ) as client:
            await client.post(SYNC_PATH)

        detail = audit_service.only.detail
        assert detail is not None
        assert detail["missing_scopes"] == [Scope.SYNC_WRITE.value]
        assert detail["method"] == "POST"
        assert detail["path"] == SYNC_PATH
        assert Scope.SYNC_READ.value not in str(detail)

    async def test_no_sync_ran(
        self,
        app_factory: object,
        settings: object,
        sync_service: FakeSyncService,
    ) -> None:
        """Guards the guard: the denial must happen before the service is reached,
        or the audit entry would be describing a sync that already ran."""
        app, plaintext = self._app_with_key(app_factory, settings, Scope.SYNC_READ)
        async with AsyncClient(
            transport=ASGITransport(app=app, client=("127.0.0.1", 1234)),
            base_url="http://nas.test",
            headers={"X-API-Key": plaintext},
        ) as client:
            await client.post(SYNC_PATH)
        assert sync_service.calls == []

    async def test_an_unauthenticated_call_is_not_audited(
        self, client: AsyncClient, audit_service: FakeAuditService
    ) -> None:
        """Deliberate. The presented key is unknown, so there is nothing to attribute
        the row to — and an anonymous caller could otherwise fill the table by
        looping. Those stay in the access log."""
        assert (await client.post(SYNC_PATH)).status_code == 401
        assert audit_service.entries == []


class TestReadingTheTrail:
    @staticmethod
    def entry(
        entry_id: int,
        *,
        action: AuditAction = AuditAction.SYNC_TRIGGER,
        outcome: AuditOutcome = AuditOutcome.SUCCESS,
        actor: str | None = ACTOR,
        hour: int = 10,
    ) -> AuditEntry:
        return AuditEntry(
            id=entry_id,
            action=action,
            outcome=outcome,
            occurred_at=datetime(2026, 8, 4, hour, 0, tzinfo=UTC),
            api_key_id=1,
            api_key_name="clientmanager",
            actor=actor,
            source_ip="10.10.10.238",
            correlation_id=f"corr-{entry_id}",
        )

    async def test_newest_first(
        self, auth_client: AsyncClient, audit_repository: InMemoryAuditRepository
    ) -> None:
        for index in range(1, 4):
            await audit_repository.record(self.entry(index, hour=8 + index))
        body = (await auth_client.get(AUDIT_PATH)).json()
        assert [item["id"] for item in body["data"]] == [3, 2, 1]

    async def test_filters_by_action(
        self, auth_client: AsyncClient, audit_repository: InMemoryAuditRepository
    ) -> None:
        await audit_repository.record(self.entry(1))
        await audit_repository.record(
            self.entry(2, action=AuditAction.AUTH_DENIED, outcome=AuditOutcome.DENIED)
        )
        body = (await auth_client.get(AUDIT_PATH, params={"action": "auth.denied"})).json()
        assert [item["id"] for item in body["data"]] == [2]

    async def test_filters_by_actor_case_insensitively(
        self, auth_client: AsyncClient, audit_repository: InMemoryAuditRepository
    ) -> None:
        """An email's local part is technically case-sensitive but never treated so."""
        await audit_repository.record(self.entry(1, actor="Gilbert@Angani.co"))
        await audit_repository.record(self.entry(2, actor="someone@angani.co"))
        body = (await auth_client.get(AUDIT_PATH, params={"actor": "gilbert@angani.co"})).json()
        assert [item["id"] for item in body["data"]] == [1]

    async def test_filters_by_since(
        self, auth_client: AsyncClient, audit_repository: InMemoryAuditRepository
    ) -> None:
        await audit_repository.record(self.entry(1, hour=8))
        await audit_repository.record(self.entry(2, hour=12))
        body = (await auth_client.get(AUDIT_PATH, params={"since": "2026-08-04T10:00:00Z"})).json()
        assert [item["id"] for item in body["data"]] == [2]

    async def test_the_response_distinguishes_key_from_actor(
        self, auth_client: AsyncClient, audit_repository: InMemoryAuditRepository
    ) -> None:
        await audit_repository.record(self.entry(1))
        item = (await auth_client.get(AUDIT_PATH)).json()["data"][0]
        assert item["api_key_name"] == "clientmanager"
        assert item["actor"] == ACTOR

    async def test_pagination_is_reported(
        self, auth_client: AsyncClient, audit_repository: InMemoryAuditRepository
    ) -> None:
        for index in range(1, 6):
            await audit_repository.record(self.entry(index, hour=index))
        body = (await auth_client.get(AUDIT_PATH, params={"page_size": 2})).json()
        assert len(body["data"]) == 2
        assert body["pagination"]["total"] == 5

    @pytest.mark.parametrize(
        "params", [{"action": "not-an-action"}, {"page": "0"}, {"page_size": "0"}]
    )
    async def test_bad_query_is_rejected(
        self, auth_client: AsyncClient, params: dict[str, str]
    ) -> None:
        response = await auth_client.get(AUDIT_PATH, params=params)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    async def test_requires_the_audit_read_scope(
        self, app_factory: object, settings: object
    ) -> None:
        """audit:read is deliberately outside the read-only bundle: a key that only
        looks up VLANs has no business reading who triggered what."""
        app, plaintext = TestScopeDenialIsAudited._app_with_key(
            app_factory, settings, Scope.VLANS_READ, Scope.SYNC_READ
        )
        async with AsyncClient(
            transport=ASGITransport(app=app, client=("127.0.0.1", 1234)),
            base_url="http://nas.test",
            headers={"X-API-Key": plaintext},
        ) as client:
            response = await client.get(AUDIT_PATH)
        assert response.status_code == 403
        assert response.json()["error"]["details"]["missing_scopes"] == [Scope.AUDIT_READ.value]

    async def test_unauthenticated_is_rejected(self, client: AsyncClient) -> None:
        assert (await client.get(AUDIT_PATH)).status_code == 401
