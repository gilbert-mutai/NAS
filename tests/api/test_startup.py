"""Application startup behaviour (the lifespan contract)."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from nas.core.credentials import NullCredentialProvider
from nas.main import _lifespan, create_app
from tests.conftest import build_settings


class TestLifespan:
    async def test_starts_with_no_credential_store_configured(self) -> None:
        app = create_app(build_settings(credentials_file=None))
        async with _lifespan(app):
            assert isinstance(app.state.credential_provider, NullCredentialProvider)

    async def test_survives_a_missing_credentials_file(self) -> None:
        """A broken credential store must not prevent the API from serving.

        Switches report credential_status="not_configured" instead. Sync is what
        genuinely needs credentials, and it fails explicitly per switch.
        """
        app = create_app(build_settings(credentials_file=Path("/nonexistent/credentials.yaml")))
        async with _lifespan(app):
            assert isinstance(app.state.credential_provider, NullCredentialProvider)

    async def test_survives_an_unreadable_credentials_file(self, tmp_path: Path) -> None:
        """Regression guard for the container crash-loop.

        A 0600 file owned by a different UID raises PermissionError deep in the
        provider; if that escapes, uvicorn fails startup and restarts forever.
        """
        path = tmp_path / "credentials.yaml"
        path.write_text("credentials: {}\n", encoding="utf-8")
        path.chmod(0o000)
        app = create_app(build_settings(credentials_file=path))
        try:
            async with _lifespan(app):
                assert isinstance(app.state.credential_provider, NullCredentialProvider)
        finally:
            path.chmod(0o600)  # so tmp_path cleanup can remove it

    async def test_survives_a_malformed_credentials_file(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.yaml"
        path.write_text("credentials: [unclosed\n", encoding="utf-8")
        path.chmod(0o600)
        app = create_app(build_settings(credentials_file=path))
        async with _lifespan(app):
            assert isinstance(app.state.credential_provider, NullCredentialProvider)

    async def test_loads_a_valid_credentials_file(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.yaml"
        path.write_text(
            "credentials:\n  juniper-core:\n    username: u\n    password: p\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        app = create_app(build_settings(credentials_file=path))
        async with _lifespan(app):
            assert app.state.credential_provider.has("juniper-core")

    async def test_database_is_disposed_on_shutdown(self) -> None:
        app: FastAPI = create_app(build_settings())
        async with _lifespan(app):
            database = app.state.database
        # Disposing twice is safe; this asserts the engine was handed over cleanly.
        await database.dispose()
