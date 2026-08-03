"""Application factory.

``create_app`` builds a fully wired FastAPI instance. It takes an optional
``Settings`` so tests can construct an app with an explicit configuration instead
of mutating the environment.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from nas import __version__
from nas.api.health import router as health_router
from nas.api.v1.router import api_router
from nas.core.config import Environment, Settings, get_settings
from nas.core.credentials import CredentialError, NullCredentialProvider, build_credential_provider
from nas.core.errors import register_exception_handlers
from nas.core.logging import configure_logging, get_logger
from nas.core.middleware import (
    IpAllowlistMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from nas.db.session import Database

logger = get_logger(__name__)

DESCRIPTION = """
Backend service for network automation. Owns all access to network devices:
authenticates to switches, discovers state, and serves it over a versioned REST
API. Consumers (such as the Angani CRM) never connect to a switch directly and
never hold switch credentials.

**Authentication** — every `/api/v1` endpoint requires an `X-API-Key` header.
Keys are scoped; a key is rejected unless it carries the scope the route
declares.

**Phase 1** covers device inventory and VLAN discovery. Nothing in this API
mutates switch configuration.
""".strip()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    app = FastAPI(
        title="Network Automation Service",
        description=DESCRIPTION,
        version=__version__,
        lifespan=_lifespan,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        # Errors are produced by our own handlers in a single envelope shape.
        responses={},
    )
    app.state.settings = settings

    _register_middleware(app, settings)
    register_exception_handlers(app)

    app.include_router(health_router)
    app.include_router(api_router, prefix=settings.api_v1_prefix)

    return app


def _register_middleware(app: FastAPI, settings: Settings) -> None:
    """Install middleware.

    Starlette applies middleware in reverse registration order, so the last one
    added is the outermost. RequestContextMiddleware is registered last and
    therefore runs first — every other layer's decisions, including an IP
    rejection, land in the access log with a correlation id.
    """
    if settings.cors_allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allow_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["X-API-Key", "X-Request-ID", "Content-Type"],
        )

    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.is_deployed)
    app.add_middleware(
        IpAllowlistMiddleware,
        allowed_ranges=settings.allowed_ip_ranges,
        trust_proxy_headers=settings.trust_proxy_headers,
    )
    app.add_middleware(RequestContextMiddleware)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings

    app.state.database = Database(settings)

    # A bad credentials file must not take the API down: switches simply report
    # credential_status="not_configured" and the failure is logged loudly. Sync
    # (Milestone 2) is what genuinely requires credentials, and it will fail
    # explicitly per switch.
    try:
        app.state.credential_provider = build_credential_provider(settings)
    except CredentialError as exc:
        logger.error("credential_provider_unavailable", error=str(exc))
        app.state.credential_provider = NullCredentialProvider()

    logger.info(
        "service_starting",
        version=__version__,
        environment=settings.environment,
        docs_enabled=settings.docs_enabled,
        ip_allowlist_entries=len(settings.allowed_ip_ranges),
        trust_proxy_headers=settings.trust_proxy_headers,
        credential_refs=len(app.state.credential_provider.refs()),
    )

    if settings.environment is Environment.LOCAL and not settings.allowed_ip_ranges:
        logger.warning("ip_allowlist_open", detail="Local development only.")

    try:
        yield
    finally:
        await app.state.database.dispose()
        logger.info("service_stopped")


# Uvicorn entrypoint:
#   uvicorn nas.main:create_app --factory
#
# Exposed as a factory rather than a module-level `app` on purpose: importing this
# module must not construct settings or open a connection pool. Otherwise every
# test, migration and `--help` invocation would require NAS_DATABASE_URL to be set.
