"""The audit trail: what gets written, and reading it back.

`POST /sync` is the only Phase 1 endpoint that reaches a switch, so it is the one
that has to be answerable for. These tests pin down that every outcome is recorded —
success, rejection and crash — and that the attribution distinguishes the key NAS
authenticated from the human the caller merely claimed.

Transaction behaviour (an audit row surviving a request that rolls back) needs a real
database and lives in tests/integration/test_audit_repository.py.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from nas.core.security import GeneratedApiKey, Scope, generate_api_key
from nas.domain.entities import ApiKey, AuditEntry, SyncRun
from nas.domain.enums import AuditAction, AuditOutcome, SyncTrigger
from tests.fakes import FakeAuditService, FakeSyncService, InMemoryAuditRepository

SYNC_PATH = "/api/v1/sync"
AUDIT_PATH = "/api/v1/audit"
ACTOR = "gilbert@angani.co"


class TestSyncTriggerIsAudited:
    async def test_a_successful_trigger_is_recorded(
        self, auth_client: AsyncClient, audit_service: FakeAuditService
    ) -> None:
        response = await auth_client.post(SYNC_PATH, headers={"X-Actor": ACTOR})
        assert response.status_code == 202

        entry = audit_service.only
        assert entry.action is AuditAction.SYNC_TRIGGER
        assert entry.outcome is AuditOutcome.SUCCESS
        assert entry.actor == ACTOR

    async def test_the_entry_points_at_the_run_it_started(
        self, auth_client: AsyncClient, audit_service: FakeAuditService
    ) -> None:
        """Without this the trail says a sync happened but not which one, and the
        counters in sync_runs cannot be tied to the person who asked for it."""
        body = (await auth_client.post(SYNC_PATH, headers={"X-Actor": ACTOR})).json()
        entry = audit_service.only
        assert entry.target_type == "sync_run"
        assert entry.target_id == str(body["id"])

    async def test_both_the_key_and_the_actor_are_recorded(
        self, auth_client: AsyncClient, audit_service: FakeAuditService, api_key: ApiKey
    ) -> None:
        """The key is what NAS proved; the actor is what it was told. Storing only
        the actor would present an unverified claim as fact."""
        await auth_client.post(SYNC_PATH, headers={"X-Actor": ACTOR})
        entry = audit_service.only
        assert entry.api_key_id == api_key.id
        assert entry.api_key_name == api_key.name
        assert entry.actor == ACTOR
        assert entry.attribution == f"{ACTOR} via {api_key.name}"

    async def test_a_missing_actor_header_still_records_the_key(
        self, auth_client: AsyncClient, audit_service: FakeAuditService, api_key: ApiKey
    ) -> None:
        """A caller that does not forward an identity must not silently skip the
        audit entry — the sync still touched a switch."""
        await auth_client.post(SYNC_PATH)
        entry = audit_service.only
        assert entry.actor is None
        assert entry.api_key_name == api_key.name
        assert entry.attribution == api_key.name

    async def test_the_correlation_id_ties_the_entry_to_the_request(
        self, auth_client: AsyncClient, audit_service: FakeAuditService
    ) -> None:
        response = await auth_client.post(
            SYNC_PATH, headers={"X-Actor": ACTOR, "X-Request-ID": "clientmanager-abc-123"}
        )
        assert audit_service.only.correlation_id == "clientmanager-abc-123"
        assert response.headers["X-Request-ID"] == "clientmanager-abc-123"

    async def test_the_source_ip_is_recorded(
        self, auth_client: AsyncClient, audit_service: FakeAuditService
    ) -> None:
        await auth_client.post(SYNC_PATH)
        assert audit_service.only.source_ip == "127.0.0.1"

    async def test_requested_switch_ids_are_recorded(
        self, auth_client: AsyncClient, audit_service: FakeAuditService
    ) -> None:
        """A targeted sync and a full sync are different actions; the trail should
        say which was asked for."""
        await auth_client.post(SYNC_PATH, json={"switch_ids": [1, 2]})
        detail = audit_service.only.detail
        assert detail is not None
        assert detail["switch_ids"] == [1, 2]

    async def test_a_hostile_actor_is_sanitised_before_storage(
        self, auth_client: AsyncClient, audit_service: FakeAuditService
    ) -> None:
        """Sent as a real header, because that is the only way it arrives.

        Sanitising lives inside AuditService rather than at the edge, so no call path
        can bypass it. The payload below carries a newline: an intermediary would
        normally reject that, but nothing in NAS may depend on an intermediary having
        done so.
        """
        await auth_client.post(
            SYNC_PATH,
            headers={"X-Actor": "gilbert@angani.co\r\nX-Injected: yes\x1b[31m<script>"},
        )
        actor = audit_service.only.actor
        assert actor is not None
        assert actor.startswith("gilbert@angani.co")
        for forbidden in ("\n", "\r", "\x1b", "<", ">", ":"):
            assert forbidden not in actor, f"{forbidden!r} survived sanitisation"

    async def test_the_run_still_succeeds_when_the_audit_write_fails(
        self,
        app_factory: object,
        settings: object,
        generated_key: GeneratedApiKey,
    ) -> None:
        """The switches have already been polled by the time the entry is written.
        Returning 500 would report a failure for work that succeeded, and the
        caller's retry would poll them again."""
        from nas.api import deps

        app: FastAPI = app_factory(settings)  # type: ignore[operator]
        app.dependency_overrides[deps.get_audit_service] = lambda: FakeAuditService(fail=True)
        async with AsyncClient(
            transport=ASGITransport(app=app, client=("127.0.0.1", 1234)),
            base_url="http://nas.test",
            headers={"X-API-Key": generated_key.plaintext},
        ) as client:
            response = await client.post(SYNC_PATH)
        assert response.status_code == 202


class TestRejectedSyncIsAudited:
    async def test_a_409_is_recorded_as_an_error(
        self,
        app_factory: object,
        settings: object,
        generated_key: GeneratedApiKey,
        audit_service: FakeAuditService,
    ) -> None:
        """A rejected attempt is as informative as an accepted one — a burst of them
        is somebody clicking a button that appears not to work.

        This is also why AuditService commits on its own session: the 409 raises, so
        the request transaction rolls back, and an entry written on it would vanish.
        """
        app: FastAPI = app_factory(settings)  # type: ignore[operator]
        app.state.sync_service = FakeSyncService(conflict=True)
        async with AsyncClient(
            transport=ASGITransport(app=app, client=("127.0.0.1", 1234)),
            base_url="http://nas.test",
            headers={"X-API-Key": generated_key.plaintext, "X-Actor": ACTOR},
        ) as client:
            response = await client.post(SYNC_PATH)

        assert response.status_code == 409
        entry = audit_service.only
        assert entry.action is AuditAction.SYNC_TRIGGER
        assert entry.outcome is AuditOutcome.ERROR
        assert entry.actor == ACTOR
        assert entry.target_id is None, "nothing was started, so there is no run to point at"

    async def test_an_unexpected_failure_is_recorded(
        self,
        app_factory: object,
        settings: object,
        generated_key: GeneratedApiKey,
        audit_service: FakeAuditService,
    ) -> None:
        """'A sync was started and never finished' is exactly what an operator needs
        to see, so a crash must not leave the trail silent."""

        class ExplodingSyncService(FakeSyncService):
            async def run(
                self,
                *,
                trigger: SyncTrigger,
                correlation_id: str | None = None,
                switch_ids: list[int] | None = None,
            ) -> SyncRun:
                raise RuntimeError("driver blew up")

        app: FastAPI = app_factory(settings)  # type: ignore[operator]
        app.state.sync_service = ExplodingSyncService()
        transport = ASGITransport(app=app, client=("127.0.0.1", 1234), raise_app_exceptions=False)
        async with AsyncClient(
            transport=transport,
            base_url="http://nas.test",
            headers={"X-API-Key": generated_key.plaintext, "X-Actor": ACTOR},
        ) as client:
            response = await client.post(SYNC_PATH)

        assert response.status_code == 500
        entry = audit_service.only
        assert entry.outcome is AuditOutcome.ERROR
        detail = entry.detail
        assert detail is not None
        assert detail["error"] == "RuntimeError"


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
