"""API authentication, scope enforcement and the error envelope."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from nas.core.security import GeneratedApiKey, Scope, generate_api_key
from nas.domain.entities import ApiKey
from tests.fakes import InMemoryApiKeyRepository

PROTECTED_PATH = "/api/v1/switches"


def client_for(app: FastAPI, *, api_key: str | None = None, host: str = "127.0.0.1") -> AsyncClient:
    headers = {"X-API-Key": api_key} if api_key else {}
    return AsyncClient(
        transport=ASGITransport(app=app, client=(host, 1234)),
        base_url="http://nas.test",
        headers=headers,
    )


class TestMissingOrInvalidKeys:
    async def test_missing_key_returns_401(self, client: AsyncClient) -> None:
        response = await client.get(PROTECTED_PATH)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHENTICATED"

    async def test_malformed_key_returns_401(self, app: FastAPI) -> None:
        async with client_for(app, api_key="not-a-key") as client:
            response = await client.get(PROTECTED_PATH)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "INVALID_API_KEY"

    async def test_unknown_key_returns_401(self, app: FastAPI) -> None:
        async with client_for(app, api_key=generate_api_key().plaintext) as client:
            response = await client.get(PROTECTED_PATH)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "INVALID_API_KEY"

    async def test_valid_key_is_accepted(self, auth_client: AsyncClient) -> None:
        assert (await auth_client.get(PROTECTED_PATH)).status_code == 200

    async def test_error_body_never_echoes_the_presented_key(self, app: FastAPI) -> None:
        presented = generate_api_key().plaintext
        async with client_for(app, api_key=presented) as client:
            response = await client.get(PROTECTED_PATH)
        assert presented not in response.text


class TestRevokedAndExpiredKeys:
    @staticmethod
    def _install(app: FastAPI, key: ApiKey) -> None:
        from nas.api import deps

        repository = InMemoryApiKeyRepository([key])
        app.dependency_overrides[deps.get_api_key_repository] = lambda: repository

    async def test_revoked_key_returns_401(
        self, app: FastAPI, generated_key: GeneratedApiKey
    ) -> None:
        self._install(
            app,
            ApiKey(
                id=1,
                name="revoked",
                prefix=generated_key.prefix,
                key_hash=generated_key.key_hash,
                scopes=frozenset(Scope.values()),
                is_active=False,
                created_at=datetime.now(UTC),
            ),
        )
        async with client_for(app, api_key=generated_key.plaintext) as client:
            response = await client.get(PROTECTED_PATH)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "INVALID_API_KEY"

    async def test_expired_key_returns_401(
        self, app: FastAPI, generated_key: GeneratedApiKey
    ) -> None:
        self._install(
            app,
            ApiKey(
                id=1,
                name="expired",
                prefix=generated_key.prefix,
                key_hash=generated_key.key_hash,
                scopes=frozenset(Scope.values()),
                is_active=True,
                created_at=datetime.now(UTC) - timedelta(days=2),
                expires_at=datetime.now(UTC) - timedelta(days=1),
            ),
        )
        async with client_for(app, api_key=generated_key.plaintext) as client:
            response = await client.get(PROTECTED_PATH)
        assert response.status_code == 401


class TestScopeEnforcement:
    async def test_key_without_required_scope_returns_403(
        self, app: FastAPI, generated_key: GeneratedApiKey
    ) -> None:
        from nas.api import deps

        # Carries sync:write but not switches:read.
        repository = InMemoryApiKeyRepository(
            [
                ApiKey(
                    id=1,
                    name="wrong-scope",
                    prefix=generated_key.prefix,
                    key_hash=generated_key.key_hash,
                    scopes=frozenset({Scope.SYNC_WRITE.value}),
                    is_active=True,
                    created_at=datetime.now(UTC),
                )
            ]
        )
        app.dependency_overrides[deps.get_api_key_repository] = lambda: repository

        async with client_for(app, api_key=generated_key.plaintext) as client:
            response = await client.get(PROTECTED_PATH)

        assert response.status_code == 403
        body = response.json()["error"]
        assert body["code"] == "INSUFFICIENT_SCOPE"
        assert body["details"]["missing_scopes"] == [Scope.SWITCHES_READ.value]


class TestErrorEnvelope:
    async def test_shape_is_consistent(self, client: AsyncClient) -> None:
        body = (await client.get(PROTECTED_PATH)).json()
        assert set(body) == {"error"}
        assert {"code", "message", "request_id"} <= set(body["error"])

    async def test_request_id_is_present_and_matches_the_header(self, client: AsyncClient) -> None:
        response = await client.get(PROTECTED_PATH)
        assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]

    async def test_inbound_request_id_is_propagated(self, auth_client: AsyncClient) -> None:
        """A correlation id set by the CRM must flow through NAS and back."""
        response = await auth_client.get(PROTECTED_PATH, headers={"X-Request-ID": "crm-abc-123"})
        assert response.headers["X-Request-ID"] == "crm-abc-123"

    async def test_hostile_request_id_is_sanitised(self, auth_client: AsyncClient) -> None:
        response = await auth_client.get(
            PROTECTED_PATH, headers={"X-Request-ID": "abc<script>alert(1)</script>"}
        )
        returned = response.headers["X-Request-ID"]
        assert "<" not in returned and ">" not in returned

    async def test_404_uses_the_envelope(self, auth_client: AsyncClient) -> None:
        response = await auth_client.get("/api/v1/switches/9999")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "SWITCH_NOT_FOUND"

    async def test_unknown_route_uses_the_envelope(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/does-not-exist")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"

    async def test_validation_error_reports_fields_without_echoing_input(
        self, auth_client: AsyncClient
    ) -> None:
        response = await auth_client.get(PROTECTED_PATH, params={"page": "not-a-number"})
        assert response.status_code == 422
        body = response.json()["error"]
        assert body["code"] == "VALIDATION_ERROR"
        assert body["details"]["fields"]
        assert "not-a-number" not in response.text


class TestSecurityHeaders:
    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            ("X-Content-Type-Options", "nosniff"),
            ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "no-referrer"),
            ("Cache-Control", "no-store"),
        ],
    )
    async def test_headers_are_present(
        self, client: AsyncClient, header: str, expected: str
    ) -> None:
        response = await client.get("/live")
        assert response.headers[header] == expected

    async def test_hsts_absent_outside_deployment(self, client: AsyncClient) -> None:
        response = await client.get("/live")
        assert "Strict-Transport-Security" not in response.headers
