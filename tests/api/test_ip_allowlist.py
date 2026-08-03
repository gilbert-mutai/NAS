"""IP allowlist enforcement and proxy-header trust."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from nas.core.config import Settings
from nas.core.security import GeneratedApiKey
from tests.conftest import build_settings

PATH = "/api/v1/switches"


@asynccontextmanager
async def make_client(
    app_factory: Callable[[Settings], FastAPI],
    settings: Settings,
    *,
    host: str,
    api_key: str,
    headers: dict[str, str] | None = None,
) -> AsyncIterator[AsyncClient]:
    app = app_factory(settings)
    all_headers = {"X-API-Key": api_key, **(headers or {})}
    async with AsyncClient(
        transport=ASGITransport(app=app, client=(host, 1234)),
        base_url="http://nas.test",
        headers=all_headers,
    ) as client:
        yield client


class TestOpenAllowlist:
    async def test_empty_allowlist_permits_any_address(
        self, app_factory: object, generated_key: GeneratedApiKey
    ) -> None:
        settings = build_settings(allowed_ip_ranges=[])
        async with make_client(
            app_factory,  # type: ignore[arg-type]
            settings,
            host="203.0.113.9",
            api_key=generated_key.plaintext,
        ) as client:
            assert (await client.get(PATH)).status_code == 200


class TestClosedAllowlist:
    async def test_address_inside_the_range_is_permitted(
        self, app_factory: object, generated_key: GeneratedApiKey
    ) -> None:
        settings = build_settings(allowed_ip_ranges=["10.20.0.0/16"])
        async with make_client(
            app_factory,  # type: ignore[arg-type]
            settings,
            host="10.20.5.7",
            api_key=generated_key.plaintext,
        ) as client:
            assert (await client.get(PATH)).status_code == 200

    async def test_address_outside_the_range_is_rejected(
        self, app_factory: object, generated_key: GeneratedApiKey
    ) -> None:
        settings = build_settings(allowed_ip_ranges=["10.20.0.0/16"])
        async with make_client(
            app_factory,  # type: ignore[arg-type]
            settings,
            host="203.0.113.9",
            api_key=generated_key.plaintext,
        ) as client:
            response = await client.get(PATH)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "IP_NOT_ALLOWED"

    async def test_rejection_happens_before_authentication(self, app_factory: object) -> None:
        """A blocked address gets 403 even with no API key at all.

        Confirms the network boundary is evaluated first, so a disallowed caller
        cannot probe key validity.
        """
        settings = build_settings(allowed_ip_ranges=["10.20.0.0/16"])
        app = app_factory(settings)  # type: ignore[operator]
        async with AsyncClient(
            transport=ASGITransport(app=app, client=("203.0.113.9", 1234)),
            base_url="http://nas.test",
        ) as client:
            response = await client.get(PATH)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "IP_NOT_ALLOWED"

    async def test_rejection_carries_a_request_id(
        self, app_factory: object, generated_key: GeneratedApiKey
    ) -> None:
        settings = build_settings(allowed_ip_ranges=["10.20.0.0/16"])
        async with make_client(
            app_factory,  # type: ignore[arg-type]
            settings,
            host="203.0.113.9",
            api_key=generated_key.plaintext,
        ) as client:
            response = await client.get(PATH)
        assert response.json()["error"]["request_id"]
        assert response.headers["X-Request-ID"]


class TestProxyHeaderTrust:
    async def test_forwarded_for_is_ignored_when_proxy_is_not_trusted(
        self, app_factory: object, generated_key: GeneratedApiKey
    ) -> None:
        """The central spoofing defence: an untrusted caller cannot claim an
        allowlisted source address via X-Forwarded-For."""
        settings = build_settings(allowed_ip_ranges=["10.20.0.0/16"], trust_proxy_headers=False)
        async with make_client(
            app_factory,  # type: ignore[arg-type]
            settings,
            host="203.0.113.9",
            api_key=generated_key.plaintext,
            headers={"X-Forwarded-For": "10.20.5.7"},
        ) as client:
            response = await client.get(PATH)
        assert response.status_code == 403

    async def test_forwarded_for_is_honoured_when_proxy_is_trusted(
        self, app_factory: object, generated_key: GeneratedApiKey
    ) -> None:
        settings = build_settings(allowed_ip_ranges=["10.20.0.0/16"], trust_proxy_headers=True)
        async with make_client(
            app_factory,  # type: ignore[arg-type]
            settings,
            host="127.0.0.1",
            api_key=generated_key.plaintext,
            headers={"X-Forwarded-For": "10.20.5.7"},
        ) as client:
            assert (await client.get(PATH)).status_code == 200

    async def test_last_hop_wins_over_attacker_prepended_entries(
        self, app_factory: object, generated_key: GeneratedApiKey
    ) -> None:
        """Nginx appends the real peer, so only the last entry is trustworthy.

        A caller prepending an allowlisted address must still be rejected.
        """
        settings = build_settings(allowed_ip_ranges=["10.20.0.0/16"], trust_proxy_headers=True)
        async with make_client(
            app_factory,  # type: ignore[arg-type]
            settings,
            host="127.0.0.1",
            api_key=generated_key.plaintext,
            headers={"X-Forwarded-For": "10.20.5.7, 203.0.113.9"},
        ) as client:
            response = await client.get(PATH)
        assert response.status_code == 403

    async def test_trusted_proxy_appending_an_allowed_peer_is_permitted(
        self, app_factory: object, generated_key: GeneratedApiKey
    ) -> None:
        settings = build_settings(allowed_ip_ranges=["10.20.0.0/16"], trust_proxy_headers=True)
        async with make_client(
            app_factory,  # type: ignore[arg-type]
            settings,
            host="127.0.0.1",
            api_key=generated_key.plaintext,
            headers={"X-Forwarded-For": "203.0.113.9, 10.20.5.7"},
        ) as client:
            assert (await client.get(PATH)).status_code == 200

    async def test_malformed_forwarded_for_is_rejected(
        self, app_factory: object, generated_key: GeneratedApiKey
    ) -> None:
        settings = build_settings(allowed_ip_ranges=["10.20.0.0/16"], trust_proxy_headers=True)
        async with make_client(
            app_factory,  # type: ignore[arg-type]
            settings,
            host="127.0.0.1",
            api_key=generated_key.plaintext,
            headers={"X-Forwarded-For": "not-an-ip"},
        ) as client:
            assert (await client.get(PATH)).status_code == 403
