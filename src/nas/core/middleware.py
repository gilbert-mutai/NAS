"""HTTP middleware: request correlation, access logging, IP allowlisting, headers."""

from __future__ import annotations

import ipaddress
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence

from fastapi import status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from nas.core.errors import ErrorCode, build_error_response
from nas.core.logging import bind_request_context, clear_request_context, get_logger

logger = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
FORWARDED_FOR_HEADER = "X-Forwarded-For"

# Liveness/readiness probes must work before a caller is known, and expose no
# switch or customer data. They are therefore exempt from the IP allowlist so
# that monitoring does not have to be whitelisted separately.
ALLOWLIST_EXEMPT_PATHS: frozenset[str] = frozenset({"/health", "/live", "/ready"})

_NextCall = Callable[[Request], Awaitable[Response]]


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a request id, binds log context, and emits one access log line.

    An inbound ``X-Request-ID`` is honoured so a correlation id set by the Django
    CRM (or Nginx) flows through NAS logs and back out on the response. Inbound
    values are length-capped and sanitised — they land in log records, so they
    are treated as untrusted input.
    """

    async def dispatch(self, request: Request, call_next: _NextCall) -> Response:
        request_id = _resolve_request_id(request)
        request.state.request_id = request_id

        client_ip = get_client_ip(request, trust_proxy_headers=_trust_proxy(request))
        request.state.client_ip = client_ip

        bind_request_context(
            request_id=request_id,
            client_ip=client_ip,
            method=request.method,
            path=request.url.path,
        )

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            # The exception propagates to Starlette's handler, which produces the
            # 500 envelope; this line guarantees the failure is still in the log
            # with its timing and correlation id.
            logger.exception("request_errored", duration_ms=duration_ms)
            clear_request_context()
            raise

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers[REQUEST_ID_HEADER] = request_id
        logger.info(
            "request_completed",
            status_code=response.status_code,
            duration_ms=duration_ms,
        )
        clear_request_context()
        return response


class IpAllowlistMiddleware(BaseHTTPMiddleware):
    """Rejects requests from addresses outside the configured CIDR allowlist.

    An empty allowlist means "allow any" — permitted only in local/test
    environments, which ``Settings`` enforces at boot. Returns the standard error
    envelope directly, because exceptions raised in middleware sit outside the
    application's exception handlers.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        allowed_ranges: Sequence[str],
        trust_proxy_headers: bool,
    ) -> None:
        super().__init__(app)
        self._networks = [ipaddress.ip_network(entry, strict=False) for entry in allowed_ranges]
        self._trust_proxy_headers = trust_proxy_headers

    async def dispatch(self, request: Request, call_next: _NextCall) -> Response:
        if not self._networks or request.url.path in ALLOWLIST_EXEMPT_PATHS:
            return await call_next(request)

        client_ip = get_client_ip(request, trust_proxy_headers=self._trust_proxy_headers)
        if not self._is_allowed(client_ip):
            logger.warning("ip_rejected", client_ip=client_ip)
            return build_error_response(
                status_code=status.HTTP_403_FORBIDDEN,
                code=ErrorCode.IP_NOT_ALLOWED,
                message="Requests from this address are not permitted.",
                request_id=getattr(request.state, "request_id", None),
            )

        return await call_next(request)

    def _is_allowed(self, client_ip: str | None) -> bool:
        if not client_ip:
            return False
        try:
            address = ipaddress.ip_address(client_ip)
        except ValueError:
            return False
        return any(address in network for network in self._networks)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Applies conservative security headers to every response.

    This is a JSON API with no browser-rendered surface beyond the Swagger page,
    so the CSP is restrictive and framing is denied outright.
    """

    def __init__(self, app: ASGIApp, *, hsts: bool) -> None:
        super().__init__(app)
        self._hsts = hsts

    async def dispatch(self, request: Request, call_next: _NextCall) -> Response:
        response = await call_next(request)
        headers = response.headers
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "no-referrer")
        headers.setdefault("Cache-Control", "no-store")
        if self._hsts:
            headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response


def get_client_ip(request: Request, *, trust_proxy_headers: bool) -> str | None:
    """Resolve the caller's IP address.

    When ``trust_proxy_headers`` is False the socket peer is used and forwarding
    headers are ignored entirely — a caller cannot spoof its way past the
    allowlist by setting its own ``X-Forwarded-For``.

    When True, the **last** entry of ``X-Forwarded-For`` is used. Nginx configured
    with ``proxy_add_x_forwarded_for`` appends the address of the peer that
    contacted it, so with a single trusted proxy the last entry is the only
    non-spoofable one. Earlier entries are attacker-controlled.
    """
    if trust_proxy_headers:
        forwarded = request.headers.get(FORWARDED_FOR_HEADER)
        if forwarded:
            hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
            if hops:
                return hops[-1]
    if request.client is None:
        return None
    return request.client.host


def _trust_proxy(request: Request) -> bool:
    settings = getattr(request.app.state, "settings", None)
    return bool(getattr(settings, "trust_proxy_headers", False))


_MAX_REQUEST_ID_LENGTH = 64


def _resolve_request_id(request: Request) -> str:
    inbound = request.headers.get(REQUEST_ID_HEADER)
    if inbound:
        # Keep only characters safe to place in logs and response headers.
        cleaned = "".join(
            ch for ch in inbound[:_MAX_REQUEST_ID_LENGTH] if ch.isalnum() or ch in "-_."
        )
        if cleaned:
            return cleaned
    return uuid.uuid4().hex
