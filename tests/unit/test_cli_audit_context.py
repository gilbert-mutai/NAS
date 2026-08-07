"""Attributing a CLI action to the human who invoked it.

This is the gap that moved the audit write out of the API route. A `nas sync run`
reaches a production switch from a shell, the process runs as the `nas` service
account, and the deployment docs say to invoke it with `sudo -u nas ...` — so without
deliberate effort the least supervised path to a device is also the only unattributed
one.

Advisory, like every actor: an operator can set `SUDO_USER` to anything. What NAS can
state as fact is that the action came from the host.
"""

from __future__ import annotations

import getpass
import os
from unittest.mock import patch

from nas.cli import _cli_audit_context


class TestActorResolution:
    def test_sudo_user_is_preferred(self) -> None:
        """The process user is `nas`; the interesting identity is who escalated."""
        with patch.dict(os.environ, {"SUDO_USER": "infra-admin"}, clear=True):
            assert _cli_audit_context().actor == "infra-admin"

    def test_falls_back_to_the_login_name(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(getpass, "getuser", return_value="gilbert"),
        ):
            assert _cli_audit_context().actor == "gilbert"

    def test_an_empty_sudo_user_does_not_shadow_the_fallback(self) -> None:
        """`SUDO_USER=` set but blank must not produce an empty actor."""
        with (
            patch.dict(os.environ, {"SUDO_USER": ""}, clear=True),
            patch.object(getpass, "getuser", return_value="gilbert"),
        ):
            assert _cli_audit_context().actor == "gilbert"

    def test_no_identity_available_yields_none(self) -> None:
        """None rather than a placeholder. An unattributed entry is honest; "unknown"
        in an actor column reads like a real identity."""
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(getpass, "getuser", side_effect=OSError("no passwd entry")),
        ):
            assert _cli_audit_context().actor is None

    def test_the_source_is_marked_as_cli(self) -> None:
        """There is no client address for a local invocation, and leaving it NULL
        would be indistinguishable from the scheduler. "cli" says which path it came
        through."""
        with patch.dict(os.environ, {"SUDO_USER": "infra-admin"}, clear=True):
            assert _cli_audit_context().source_ip == "cli"

    def test_no_api_key_is_claimed(self) -> None:
        """A CLI run authenticates nothing — shell access *is* the authorisation. A
        key name here would invent a credential that was never presented."""
        with patch.dict(os.environ, {"SUDO_USER": "infra-admin"}, clear=True):
            context = _cli_audit_context()
            assert context.api_key_id is None
            assert context.api_key_name is None

    def test_a_hostile_sudo_user_is_left_for_the_service_to_clean(self) -> None:
        """Sanitisation belongs to AuditService, so there is one implementation
        covering every caller rather than one per entry point."""
        with patch.dict(os.environ, {"SUDO_USER": "admin\nfake=line"}, clear=True):
            assert _cli_audit_context().actor == "admin\nfake=line"
