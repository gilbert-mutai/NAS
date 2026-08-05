"""Device credential resolution.

Switch credentials are deliberately **not** stored in the database. The
``switches`` table holds only a ``credential_ref`` — a name — and this module
resolves that name to a secret held outside the database entirely. A NAS
database dump therefore grants no access to any network device.

Phase 1 ships a file-backed provider (a ``0600`` YAML file outside the repo).
Because every consumer depends on the ``CredentialProvider`` protocol rather
than the implementation, moving to Vault or AWS Secrets Manager later is a new
class and a factory line — no change to the sync engine or the drivers.
"""

from __future__ import annotations

import stat
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

import yaml

from nas.core.config import Settings
from nas.core.logging import get_logger

logger = get_logger(__name__)


class AuthMethod(StrEnum):
    PASSWORD = "password"  # noqa: S105 - an auth-method name, not a credential
    SSH_KEY = "ssh_key"


class CredentialError(Exception):
    """Raised when a credential cannot be resolved or the store is unusable."""


class CredentialNotFoundError(CredentialError):
    pass


@dataclass(frozen=True, slots=True)
class DeviceCredential:
    """Resolved credentials for one device.

    Secret-bearing fields are ``repr=False`` so that logging or an exception
    traceback that captures this object cannot print the secret.
    """

    ref: str
    username: str
    auth_method: AuthMethod
    password: str | None = field(default=None, repr=False)
    private_key_path: Path | None = field(default=None, repr=False)
    private_key_passphrase: str | None = field(default=None, repr=False)
    enable_password: str | None = field(default=None, repr=False)
    """Cisco enable secret. Optional.

    VLAN discovery normally works at privilege 1, so most estates need nothing
    here. Supply it only where ``show`` commands are restricted; the IOS-XE driver
    attempts ``enable`` when it is set and continues without it if that fails."""

    def __post_init__(self) -> None:
        if self.auth_method is AuthMethod.PASSWORD and not self.password:
            raise CredentialError(
                f"Credential {self.ref!r} uses password auth but has no password."
            )
        if self.auth_method is AuthMethod.SSH_KEY and not self.private_key_path:
            raise CredentialError(
                f"Credential {self.ref!r} uses ssh_key auth but has no private_key_path."
            )


@runtime_checkable
class CredentialProvider(Protocol):
    """Resolves a credential reference to a concrete secret."""

    def get(self, ref: str) -> DeviceCredential:
        """Return the credential for ``ref``, or raise CredentialNotFoundError."""
        ...

    def has(self, ref: str) -> bool:
        """Whether ``ref`` can be resolved. Never raises."""
        ...

    def refs(self) -> frozenset[str]:
        """All known references. Used for configuration health checks."""
        ...


class FileCredentialProvider:
    """Reads credentials from a YAML file on disk.

    The file is loaded once at construction. Restart the service (or reload the
    provider) after rotating a secret — this keeps request handling free of
    filesystem I/O and makes the loaded state deterministic.
    """

    def __init__(self, path: Path, *, require_strict_permissions: bool = True) -> None:
        self._path = path
        self._credentials: dict[str, DeviceCredential] = {}
        self._load(require_strict_permissions=require_strict_permissions)

    def _load(self, *, require_strict_permissions: bool) -> None:
        if not self._path.is_file():
            raise CredentialError(f"Credentials file not found: {self._path}")

        self._check_permissions(require_strict=require_strict_permissions)

        try:
            contents = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            # Every failure to reach the store must surface as CredentialError so
            # callers have one exception type to handle. A bare PermissionError
            # here would escape build_credential_provider and crash-loop startup —
            # exactly what a 0600 file owned by another UID produces when the
            # service runs as a different user (a common container mount setup).
            raise CredentialError(
                f"Credentials file {self._path} could not be read: {exc.strerror}. "
                "Check that it is owned by, or readable by, the user running NAS."
            ) from exc
        except UnicodeDecodeError as exc:
            raise CredentialError(f"Credentials file {self._path} is not valid UTF-8.") from exc

        try:
            raw = yaml.safe_load(contents) or {}
        except yaml.YAMLError as exc:
            # Deliberately does not include the parser's context snippet, which
            # can quote the offending line — i.e. a secret.
            raise CredentialError(f"Credentials file {self._path} is not valid YAML.") from exc

        if not isinstance(raw, dict):
            raise CredentialError(f"Credentials file {self._path} must contain a mapping.")

        entries = raw.get("credentials", {})
        if not isinstance(entries, dict):
            raise CredentialError(
                f"Credentials file {self._path} must contain a 'credentials' mapping."
            )

        for ref, values in entries.items():
            if not isinstance(values, dict):
                raise CredentialError(f"Credential {ref!r} must be a mapping of fields.")
            self._credentials[str(ref)] = self._build(str(ref), values)

        logger.info(
            "credentials_loaded",
            path=str(self._path),
            credential_count=len(self._credentials),
        )

    def _check_permissions(self, *, require_strict: bool) -> None:
        try:
            mode = self._path.stat().st_mode
        except OSError as exc:
            raise CredentialError(
                f"Credentials file {self._path} could not be inspected: {exc.strerror}."
            ) from exc
        group_or_world_readable = bool(mode & (stat.S_IRGRP | stat.S_IROTH))
        if not group_or_world_readable:
            return
        octal = oct(stat.S_IMODE(mode))
        if require_strict:
            raise CredentialError(
                f"Credentials file {self._path} has mode {octal}; it must not be readable by "
                "group or other. Run: chmod 600 " + str(self._path)
            )
        logger.warning("credentials_file_permissive", path=str(self._path), mode=octal)

    @staticmethod
    def _build(ref: str, values: dict[str, object]) -> DeviceCredential:
        username = values.get("username")
        if not isinstance(username, str) or not username:
            raise CredentialError(f"Credential {ref!r} is missing a username.")

        raw_method = values.get("auth_method", AuthMethod.PASSWORD.value)
        try:
            auth_method = AuthMethod(str(raw_method))
        except ValueError as exc:
            allowed = ", ".join(m.value for m in AuthMethod)
            raise CredentialError(
                f"Credential {ref!r} has unknown auth_method {raw_method!r}. Allowed: {allowed}."
            ) from exc

        key_path = values.get("private_key_path")
        return DeviceCredential(
            ref=ref,
            username=username,
            auth_method=auth_method,
            password=_optional_str(values.get("password")),
            private_key_path=Path(str(key_path)) if key_path else None,
            private_key_passphrase=_optional_str(values.get("private_key_passphrase")),
            enable_password=_optional_str(values.get("enable_password")),
        )

    def get(self, ref: str) -> DeviceCredential:
        try:
            return self._credentials[ref]
        except KeyError as exc:
            raise CredentialNotFoundError(f"No credential named {ref!r} in {self._path}.") from exc

    def has(self, ref: str) -> bool:
        return ref in self._credentials

    def refs(self) -> frozenset[str]:
        return frozenset(self._credentials)


class NullCredentialProvider:
    """Resolves nothing. Used when no credential store is configured.

    Keeps the service bootable for API-only work (and for CI) while making the
    absence of credentials explicit rather than implicit: switches will report a
    ``missing`` credential status instead of failing mysteriously at sync time.
    """

    def get(self, ref: str) -> DeviceCredential:
        raise CredentialNotFoundError(
            f"No credential store configured; cannot resolve {ref!r}. Set NAS_CREDENTIALS_FILE."
        )

    def has(self, ref: str) -> bool:
        return False

    def refs(self) -> frozenset[str]:
        return frozenset()


def build_credential_provider(settings: Settings) -> CredentialProvider:
    if settings.credentials_file is None:
        if settings.is_deployed:
            logger.warning("credential_store_not_configured", environment=settings.environment)
        return NullCredentialProvider()
    # Locally, a permissive mode is a warning rather than a hard failure so a
    # freshly cloned checkout does not block a developer on chmod.
    return FileCredentialProvider(
        settings.credentials_file,
        require_strict_permissions=settings.is_deployed,
    )


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None
