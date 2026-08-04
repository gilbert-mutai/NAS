"""Synchronisation endpoints."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from nas.domain.entities import SyncRun
from nas.domain.enums import SyncStatus, SyncTrigger
from tests.fakes import InMemorySyncRunRepository

PATH = "/api/v1/sync"


def seeded_run(
    run_id: int = 1,
    *,
    status: SyncStatus = SyncStatus.SUCCESS,
    started_at: datetime | None = None,
) -> SyncRun:
    moment = started_at or datetime(2026, 8, 4, 10, 0, tzinfo=UTC)
    return SyncRun(
        id=run_id,
        trigger=SyncTrigger.SCHEDULED,
        status=status,
        started_at=moment,
        correlation_id=f"corr-{run_id}",
        finished_at=moment if status.is_terminal else None,
        duration_ms=900,
        switches_total=3,
        switches_succeeded=3,
    )


class TestTriggerSync:
    async def test_returns_202_with_the_run(self, auth_client: AsyncClient) -> None:
        response = await auth_client.post(PATH)
        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "partial"
        assert body["trigger"] == "manual"
        assert body["correlation_id"]

    async def test_per_switch_detail_is_returned(self, auth_client: AsyncClient) -> None:
        """A partial run has to be explainable, not just labelled."""
        body = (await auth_client.post(PATH)).json()
        results = {r["switch_name"]: r for r in body["switch_results"]}
        assert results["sw-a"]["outcome"] == "success"
        assert results["sw-c"]["outcome"] == "failed"
        assert "unreachable" in results["sw-c"]["error_message"]

    async def test_switch_ids_are_forwarded(
        self, auth_client: AsyncClient, sync_service: object
    ) -> None:
        await auth_client.post(PATH, json={"switch_ids": [1, 2]})
        assert sync_service.calls[-1]["switch_ids"] == [1, 2]  # type: ignore[attr-defined]

    async def test_empty_body_syncs_everything(
        self, auth_client: AsyncClient, sync_service: object
    ) -> None:
        await auth_client.post(PATH)
        assert sync_service.calls[-1]["switch_ids"] is None  # type: ignore[attr-defined]

    async def test_unknown_body_field_is_rejected(self, auth_client: AsyncClient) -> None:
        response = await auth_client.post(PATH, json={"switch_ids": [1], "wat": True})
        assert response.status_code == 422

    async def test_concurrent_run_returns_409(
        self, app_factory: object, settings: object, generated_key: object
    ) -> None:
        """A double-clicked 'Sync Now' must not start two runs."""
        from tests.fakes import FakeSyncService

        app: FastAPI = app_factory(settings)  # type: ignore[operator]
        app.state.sync_service = FakeSyncService(conflict=True)
        async with AsyncClient(
            transport=ASGITransport(app=app, client=("127.0.0.1", 1234)),
            base_url="http://nas.test",
            headers={"X-API-Key": generated_key.plaintext},  # type: ignore[attr-defined]
        ) as client:
            response = await client.post(PATH)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CONFLICT"

    async def test_requires_sync_write_scope(self, client: AsyncClient) -> None:
        assert (await client.post(PATH)).status_code == 401


class TestSyncStatus:
    async def test_reports_never_run(self, auth_client: AsyncClient) -> None:
        body = (await auth_client.get(f"{PATH}/status")).json()
        assert body["never_run"] is True
        assert body["is_running"] is False
        assert body["latest_run"] is None

    async def test_reports_the_latest_run(
        self, auth_client: AsyncClient, sync_run_repository: InMemorySyncRunRepository
    ) -> None:
        sync_run_repository.seed(seeded_run(1))
        sync_run_repository.seed(seeded_run(2, started_at=datetime(2026, 8, 4, 11, 0, tzinfo=UTC)))
        body = (await auth_client.get(f"{PATH}/status")).json()
        assert body["never_run"] is False
        assert body["latest_run"]["id"] == 2

    async def test_reports_a_run_in_progress(
        self, auth_client: AsyncClient, sync_run_repository: InMemorySyncRunRepository
    ) -> None:
        sync_run_repository.seed(seeded_run(1, status=SyncStatus.RUNNING))
        body = (await auth_client.get(f"{PATH}/status")).json()
        assert body["is_running"] is True


class TestSyncRunHistory:
    async def test_lists_newest_first(
        self, auth_client: AsyncClient, sync_run_repository: InMemorySyncRunRepository
    ) -> None:
        for index in range(1, 4):
            sync_run_repository.seed(
                seeded_run(index, started_at=datetime(2026, 8, 4, 9 + index, 0, tzinfo=UTC))
            )
        body = (await auth_client.get(f"{PATH}/runs")).json()
        assert [r["id"] for r in body["data"]] == [3, 2, 1]
        assert body["pagination"]["total"] == 3

    async def test_paginates(
        self, auth_client: AsyncClient, sync_run_repository: InMemorySyncRunRepository
    ) -> None:
        for index in range(1, 8):
            sync_run_repository.seed(
                seeded_run(index, started_at=datetime(2026, 8, 4, 1, index, tzinfo=UTC))
            )
        body = (await auth_client.get(f"{PATH}/runs", params={"page_size": 3})).json()
        assert len(body["data"]) == 3
        assert body["pagination"]["total_pages"] == 3

    async def test_get_one_run(
        self, auth_client: AsyncClient, sync_run_repository: InMemorySyncRunRepository
    ) -> None:
        sync_run_repository.seed(seeded_run(5))
        body = (await auth_client.get(f"{PATH}/runs/5")).json()
        assert body["id"] == 5
        assert body["correlation_id"] == "corr-5"

    async def test_unknown_run_returns_404_envelope(self, auth_client: AsyncClient) -> None:
        response = await auth_client.get(f"{PATH}/runs/999")
        assert response.status_code == 404
        error = response.json()["error"]
        assert error["code"] == "NOT_FOUND"
        assert error["details"]["sync_run_id"] == 999

    async def test_running_run_is_not_cached(
        self, auth_client: AsyncClient, sync_run_repository: InMemorySyncRunRepository
    ) -> None:
        """Its counters are not final yet."""
        sync_run_repository.seed(seeded_run(6, status=SyncStatus.RUNNING))
        response = await auth_client.get(f"{PATH}/runs/6")
        assert response.headers["Cache-Control"] == "no-store"

    async def test_zero_id_is_rejected(self, auth_client: AsyncClient) -> None:
        assert (await auth_client.get(f"{PATH}/runs/0")).status_code == 422


class TestOpenApiCoverage:
    async def test_new_routes_are_documented(self, client: AsyncClient) -> None:
        paths = (await client.get("/openapi.json")).json()["paths"]
        assert "/api/v1/vlans" in paths
        assert "/api/v1/vlans/lookup/{vlan_id}" in paths
        assert "/api/v1/sync" in paths
        assert "/api/v1/sync/status" in paths
        assert "/api/v1/sync/runs" in paths

    async def test_sync_documents_the_409(self, client: AsyncClient) -> None:
        paths = (await client.get("/openapi.json")).json()["paths"]
        assert "409" in paths["/api/v1/sync"]["post"]["responses"]
