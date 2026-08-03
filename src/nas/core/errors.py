"""Centralised error handling and the single API error envelope.

Every failure — expected or not — leaves the service as:

    {"error": {"code": "...", "message": "...", "request_id": "...", "details": {...}}}

Unexpected exceptions are logged with a full stack trace but reported to the
caller as a generic INTERNAL_ERROR, so implementation details never leak across
the trust boundary.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from nas.core.logging import get_logger

logger = get_logger(__name__)


class ErrorCode(StrEnum):
    # 4xx
    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    INVALID_API_KEY = "INVALID_API_KEY"
    FORBIDDEN = "FORBIDDEN"
    INSUFFICIENT_SCOPE = "INSUFFICIENT_SCOPE"
    IP_NOT_ALLOWED = "IP_NOT_ALLOWED"
    NOT_FOUND = "NOT_FOUND"
    SWITCH_NOT_FOUND = "SWITCH_NOT_FOUND"
    CONFLICT = "CONFLICT"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    # 5xx
    INTERNAL_ERROR = "INTERNAL_ERROR"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"


class AppError(Exception):
    """Base class for all errors this service raises deliberately."""

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: ErrorCode = ErrorCode.INTERNAL_ERROR
    message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.message
        self.details = details or {}
        super().__init__(self.message)


class ValidationError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = ErrorCode.VALIDATION_ERROR
    message = "The request payload is invalid."


class UnauthenticatedError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = ErrorCode.UNAUTHENTICATED
    message = "Authentication is required."


class InvalidApiKeyError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = ErrorCode.INVALID_API_KEY
    message = "The supplied API key is invalid, expired or revoked."


class InsufficientScopeError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = ErrorCode.INSUFFICIENT_SCOPE
    message = "The API key does not carry the scope required for this operation."


class IpNotAllowedError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = ErrorCode.IP_NOT_ALLOWED
    message = "Requests from this address are not permitted."


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = ErrorCode.NOT_FOUND
    message = "The requested resource does not exist."


class SwitchNotFoundError(NotFoundError):
    code = ErrorCode.SWITCH_NOT_FOUND
    message = "No switch exists with that identifier."


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = ErrorCode.CONFLICT
    message = "The request conflicts with the current state of the resource."


class ConfigurationError(AppError):
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    code = ErrorCode.CONFIGURATION_ERROR
    message = "The service is misconfigured."


def build_error_response(
    *,
    status_code: int,
    code: ErrorCode | str,
    message: str,
    request_id: str | None,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    payload: dict[str, Any] = {
        "error": {
            "code": str(code),
            "message": message,
            "request_id": request_id,
        }
    }
    if details:
        payload["error"]["details"] = details
    return JSONResponse(status_code=status_code, content=payload)


def _request_id(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return str(value) if value else None


_HTTP_STATUS_TO_CODE: dict[int, ErrorCode] = {
    status.HTTP_401_UNAUTHORIZED: ErrorCode.UNAUTHENTICATED,
    status.HTTP_403_FORBIDDEN: ErrorCode.FORBIDDEN,
    status.HTTP_404_NOT_FOUND: ErrorCode.NOT_FOUND,
    status.HTTP_405_METHOD_NOT_ALLOWED: ErrorCode.METHOD_NOT_ALLOWED,
    status.HTTP_409_CONFLICT: ErrorCode.CONFLICT,
    status.HTTP_422_UNPROCESSABLE_CONTENT: ErrorCode.VALIDATION_ERROR,
}


def register_exception_handlers(app: FastAPI) -> None:
    """Install handlers so no code path can return an off-format error body."""

    @app.exception_handler(AppError)
    async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        log = logger.bind(error_code=str(exc.code), status_code=exc.status_code)
        if exc.status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR:
            log.error("request_failed", exc_info=exc)
        else:
            log.info("request_rejected", reason=exc.message)
        return build_error_response(
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            request_id=_request_id(request),
            details=exc.details,
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_request_validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Pydantic error objects can carry the offending input; strip it so a
        # malformed secret in a request body is never echoed back or logged.
        fields = [
            {
                "location": list(err.get("loc", [])),
                "message": err.get("msg", ""),
                "type": err.get("type", ""),
            }
            for err in exc.errors()
        ]
        logger.info("request_validation_failed", field_count=len(fields))
        return build_error_response(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code=ErrorCode.VALIDATION_ERROR,
            message="The request payload is invalid.",
            request_id=_request_id(request),
            details={"fields": fields},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_STATUS_TO_CODE.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
        message = str(exc.detail) if exc.detail else "Request could not be completed."
        return build_error_response(
            status_code=exc.status_code,
            code=code,
            message=message,
            request_id=_request_id(request),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled_exception", exc_info=exc)
        return build_error_response(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code=ErrorCode.INTERNAL_ERROR,
            message="An unexpected error occurred.",
            request_id=_request_id(request),
        )
