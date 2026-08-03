"""API key authentication and scope authorisation."""

from __future__ import annotations

from datetime import UTC, datetime

from nas.core.errors import InsufficientScopeError, InvalidApiKeyError, UnauthenticatedError
from nas.core.logging import get_logger
from nas.core.security import extract_prefix, hash_api_key, verify_api_key
from nas.domain.entities import ApiKey
from nas.repositories.protocols import ApiKeyRepository

logger = get_logger(__name__)

# Compared against when no key matches, so a request with an unknown prefix costs
# the same as one with a known prefix. Prevents the response time from revealing
# which prefixes exist.
_DUMMY_HASH = hash_api_key("nas_00000000_unused-placeholder-for-constant-time-compare")


class AuthenticationService:
    def __init__(self, api_keys: ApiKeyRepository) -> None:
        self._api_keys = api_keys

    async def authenticate(self, presented_key: str | None) -> ApiKey:
        """Resolve a presented key to an ApiKey, or raise.

        Every rejection path returns the same error code and message. The caller
        learns only that the key is unusable — never whether it was malformed,
        unknown, revoked or expired.
        """
        if not presented_key:
            raise UnauthenticatedError("An API key is required. Supply the X-API-Key header.")

        prefix = extract_prefix(presented_key)
        if prefix is None:
            verify_api_key(presented_key, _DUMMY_HASH)
            logger.info("auth_failed", reason="malformed_key")
            raise InvalidApiKeyError

        api_key = await self._api_keys.get_by_prefix(prefix)
        if api_key is None:
            verify_api_key(presented_key, _DUMMY_HASH)
            logger.info("auth_failed", reason="unknown_prefix", key_prefix=prefix)
            raise InvalidApiKeyError

        if not verify_api_key(presented_key, api_key.key_hash):
            logger.warning("auth_failed", reason="hash_mismatch", key_prefix=prefix)
            raise InvalidApiKeyError

        now = datetime.now(UTC)
        if not api_key.is_usable(now=now):
            reason = "expired" if api_key.is_expired(now=now) else "revoked"
            logger.info("auth_failed", reason=reason, api_key_name=api_key.name)
            raise InvalidApiKeyError

        await self._api_keys.mark_used(api_key.id, when=now)
        logger.debug("auth_succeeded", api_key_name=api_key.name)
        return api_key

    @staticmethod
    def authorize(api_key: ApiKey, required_scopes: frozenset[str]) -> None:
        """Assert the key carries every required scope."""
        if not required_scopes:
            return
        if api_key.has_all_scopes(required_scopes):
            return
        missing = sorted(required_scopes - api_key.scopes)
        logger.info(
            "authorization_failed",
            api_key_name=api_key.name,
            missing_scopes=missing,
        )
        raise InsufficientScopeError(
            "The API key does not carry the scope required for this operation.",
            details={"required_scopes": sorted(required_scopes), "missing_scopes": missing},
        )
