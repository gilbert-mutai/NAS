"""Health, liveness and readiness endpoints."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from nas import __version__
from tests.conftest import build_settings
from tests.fakes import FakeDatabase


class TestLiveness:
    async def test_reports_alive(self, client: AsyncClient) -> None:
        response = await client.get("/live")
        assert response.status_code == 200
        assert response.json() == {"status": "alive", "service": "nas", "version": __version__}

    async def test_requires_no_api_key(self, client: AsyncClient) -> None:
        assert (await client.get("/live")).status_code == 200


class TestReadiness:
    async def test_ready_when_database_reachable(self, client: AsyncClient) -> None:
        response = await client.get("/ready")
        assert response.status_code == 200
        assert response.json() == {"status": "ready", "database": "ok"}

    async def test_returns_503_when_database_unreachable(self, app: FastAPI) -> None:
        """An unready instance must be pulled from the load balancer, not restarted."""
        app.state.database = FakeDatabase(healthy=False)
        transport = ASGITransport(app=app, client=("127.0.0.1", 1234))
        async with AsyncClient(transport=transport, base_url="http://nas.test") as client:
            response = await client.get("/ready")
        assert response.status_code == 503
        assert response.json() == {"status": "not_ready", "database": "unavailable"}


class TestHealth:
    async def test_summary_when_healthy(self, client: AsyncClient) -> None:
        response = await client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["database"] == "ok"
        assert body["version"] == __version__
        assert body["environment"] == "test"

    async def test_degraded_when_database_unreachable(self, app: FastAPI) -> None:
        app.state.database = FakeDatabase(healthy=False)
        transport = ASGITransport(app=app, client=("127.0.0.1", 1234))
        async with AsyncClient(transport=transport, base_url="http://nas.test") as client:
            response = await client.get("/health")
        assert response.status_code == 503
        assert response.json()["status"] == "degraded"

    async def test_exposes_no_credential_or_switch_data(self, client: AsyncClient) -> None:
        body = (await client.get("/health")).json()
        assert set(body) == {"status", "service", "version", "environment", "database"}


class TestProbesAreExemptFromTheAllowlist:
    @pytest.mark.parametrize("path", ["/health", "/live", "/ready"])
    async def test_probe_succeeds_from_a_non_allowlisted_address(
        self, app_factory: object, path: str
    ) -> None:
        settings = build_settings(allowed_ip_ranges=["10.0.0.0/8"])
        app = app_factory(settings)  # type: ignore[operator]
        transport = ASGITransport(app=app, client=("127.0.0.1", 1234))
        async with AsyncClient(transport=transport, base_url="http://nas.test") as client:
            assert (await client.get(path)).status_code in (200, 503)

    async def test_api_is_still_blocked_from_that_address(self, app_factory: object) -> None:
        settings = build_settings(allowed_ip_ranges=["10.0.0.0/8"])
        app = app_factory(settings)  # type: ignore[operator]
        transport = ASGITransport(app=app, client=("127.0.0.1", 1234))
        async with AsyncClient(transport=transport, base_url="http://nas.test") as client:
            response = await client.get("/api/v1/switches")
        assert response.status_code == 403
