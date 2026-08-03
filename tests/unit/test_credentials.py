"""Credential store loading, permission enforcement and secret hygiene."""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from nas.core.config import Environment
from nas.core.credentials import (
    AuthMethod,
    CredentialError,
    CredentialNotFoundError,
    DeviceCredential,
    FileCredentialProvider,
    NullCredentialProvider,
    build_credential_provider,
)
from tests.conftest import build_settings

VALID_YAML = textwrap.dedent(
    """
    credentials:
      juniper-core:
        username: nas-readonly
        auth_method: password
        password: s3cret
      juniper-edge:
        username: nas-readonly
        auth_method: ssh_key
        private_key_path: /etc/nas/keys/id_ed25519
        private_key_passphrase: keypass
    """
).strip()


def write_credentials(tmp_path: Path, content: str, *, mode: int = 0o600) -> Path:
    path = tmp_path / "credentials.yaml"
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)
    return path


class TestFileCredentialProvider:
    def test_loads_password_credential(self, tmp_path: Path) -> None:
        provider = FileCredentialProvider(write_credentials(tmp_path, VALID_YAML))
        credential = provider.get("juniper-core")
        assert credential.username == "nas-readonly"
        assert credential.auth_method is AuthMethod.PASSWORD
        assert credential.password == "s3cret"

    def test_loads_ssh_key_credential(self, tmp_path: Path) -> None:
        provider = FileCredentialProvider(write_credentials(tmp_path, VALID_YAML))
        credential = provider.get("juniper-edge")
        assert credential.auth_method is AuthMethod.SSH_KEY
        assert credential.private_key_path == Path("/etc/nas/keys/id_ed25519")

    def test_reports_known_refs(self, tmp_path: Path) -> None:
        provider = FileCredentialProvider(write_credentials(tmp_path, VALID_YAML))
        assert provider.refs() == frozenset({"juniper-core", "juniper-edge"})
        assert provider.has("juniper-core") is True
        assert provider.has("nope") is False

    def test_unknown_ref_raises(self, tmp_path: Path) -> None:
        provider = FileCredentialProvider(write_credentials(tmp_path, VALID_YAML))
        with pytest.raises(CredentialNotFoundError):
            provider.get("nope")

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(CredentialError, match="not found"):
            FileCredentialProvider(tmp_path / "absent.yaml")

    def test_group_readable_file_is_rejected_when_strict(self, tmp_path: Path) -> None:
        path = write_credentials(tmp_path, VALID_YAML, mode=0o640)
        with pytest.raises(CredentialError, match="must not be readable"):
            FileCredentialProvider(path, require_strict_permissions=True)

    def test_group_readable_file_is_allowed_when_not_strict(self, tmp_path: Path) -> None:
        path = write_credentials(tmp_path, VALID_YAML, mode=0o644)
        provider = FileCredentialProvider(path, require_strict_permissions=False)
        assert provider.has("juniper-core")

    def test_malformed_yaml_error_does_not_echo_file_contents(self, tmp_path: Path) -> None:
        path = write_credentials(tmp_path, "credentials: [unclosed\n  password: leaky-secret")
        with pytest.raises(CredentialError) as exc_info:
            FileCredentialProvider(path)
        assert "leaky-secret" not in str(exc_info.value)

    def test_non_mapping_root_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(CredentialError, match="must contain a mapping"):
            FileCredentialProvider(write_credentials(tmp_path, "- just\n- a\n- list"))

    def test_missing_username_rejected(self, tmp_path: Path) -> None:
        content = "credentials:\n  broken:\n    password: x"
        with pytest.raises(CredentialError, match="missing a username"):
            FileCredentialProvider(write_credentials(tmp_path, content))

    def test_unknown_auth_method_rejected(self, tmp_path: Path) -> None:
        content = "credentials:\n  broken:\n    username: u\n    auth_method: telepathy"
        with pytest.raises(CredentialError, match="unknown auth_method"):
            FileCredentialProvider(write_credentials(tmp_path, content))

    def test_password_auth_without_password_rejected(self, tmp_path: Path) -> None:
        content = "credentials:\n  broken:\n    username: u\n    auth_method: password"
        with pytest.raises(CredentialError, match="no password"):
            FileCredentialProvider(write_credentials(tmp_path, content))

    def test_ssh_key_auth_without_key_path_rejected(self, tmp_path: Path) -> None:
        content = "credentials:\n  broken:\n    username: u\n    auth_method: ssh_key"
        with pytest.raises(CredentialError, match="no private_key_path"):
            FileCredentialProvider(write_credentials(tmp_path, content))

    def test_empty_credentials_section_is_valid(self, tmp_path: Path) -> None:
        provider = FileCredentialProvider(write_credentials(tmp_path, "credentials: {}"))
        assert provider.refs() == frozenset()

    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permissions")
    def test_unreadable_file_raises_credential_error_not_oserror(self, tmp_path: Path) -> None:
        """A 0600 file owned by another user must not escape as PermissionError.

        Regression guard: an OSError here propagates past build_credential_provider
        and crash-loops startup, which is what happens when the service runs as a
        different UID than the owner of a bind-mounted secret.
        """
        path = write_credentials(tmp_path, VALID_YAML, mode=0o000)
        with pytest.raises(CredentialError, match="could not be read"):
            FileCredentialProvider(path, require_strict_permissions=False)

    def test_invalid_utf8_raises_credential_error(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.yaml"
        path.write_bytes(b"\xff\xfe not utf-8")
        path.chmod(0o600)
        with pytest.raises(CredentialError, match="not valid UTF-8"):
            FileCredentialProvider(path)


class TestSecretHygiene:
    def test_repr_hides_password(self) -> None:
        credential = DeviceCredential(
            ref="juniper-core",
            username="nas-readonly",
            auth_method=AuthMethod.PASSWORD,
            password="do-not-print-me",
        )
        rendered = repr(credential)
        assert "do-not-print-me" not in rendered
        assert "juniper-core" in rendered

    def test_repr_hides_key_passphrase(self) -> None:
        credential = DeviceCredential(
            ref="juniper-edge",
            username="nas-readonly",
            auth_method=AuthMethod.SSH_KEY,
            private_key_path=Path("/etc/nas/keys/id_ed25519"),
            private_key_passphrase="do-not-print-me",
        )
        assert "do-not-print-me" not in repr(credential)


class TestNullCredentialProvider:
    def test_resolves_nothing(self) -> None:
        provider = NullCredentialProvider()
        assert provider.has("anything") is False
        assert provider.refs() == frozenset()
        with pytest.raises(CredentialNotFoundError, match="No credential store configured"):
            provider.get("anything")


class TestBuildCredentialProvider:
    def test_returns_null_provider_when_unconfigured(self) -> None:
        provider = build_credential_provider(build_settings(credentials_file=None))
        assert isinstance(provider, NullCredentialProvider)

    def test_returns_file_provider_when_configured(self, tmp_path: Path) -> None:
        path = write_credentials(tmp_path, VALID_YAML)
        provider = build_credential_provider(build_settings(credentials_file=path))
        assert provider.has("juniper-core")

    def test_deployed_environment_enforces_strict_permissions(self, tmp_path: Path) -> None:
        path = write_credentials(tmp_path, VALID_YAML, mode=0o644)
        settings = build_settings(
            credentials_file=path,
            environment=Environment.PRODUCTION,
            allowed_ip_ranges=["10.0.0.0/8"],
        )
        with pytest.raises(CredentialError, match="must not be readable"):
            build_credential_provider(settings)
