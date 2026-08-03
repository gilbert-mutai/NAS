"""API key generation, parsing and verification."""

from __future__ import annotations

import pytest

from nas.core.security import (
    KEY_NAMESPACE,
    READ_ONLY_SCOPES,
    Scope,
    extract_prefix,
    generate_api_key,
    hash_api_key,
    redact,
    verify_api_key,
)


class TestGenerateApiKey:
    def test_has_expected_shape(self) -> None:
        # maxsplit=2 because the base64url secret may itself contain '_'.
        namespace, prefix, secret = (generated := generate_api_key()).plaintext.split("_", 2)
        assert namespace == KEY_NAMESPACE
        assert prefix == generated.prefix
        assert len(prefix) == 8
        assert len(secret) >= 40

    def test_secrets_containing_the_separator_still_parse(self) -> None:
        """token_urlsafe emits '_' and '-'; a key must not become unparseable."""
        crafted = f"{KEY_NAMESPACE}_1a2b3c4d_ab_cd-ef_gh"
        assert extract_prefix(crafted) == "1a2b3c4d"

    def test_every_generated_key_round_trips(self) -> None:
        """Regression guard: parsing must hold across the full character space."""
        for _ in range(200):
            generated = generate_api_key()
            assert extract_prefix(generated.plaintext) == generated.prefix
            assert verify_api_key(generated.plaintext, generated.key_hash) is True

    def test_hash_matches_plaintext(self) -> None:
        generated = generate_api_key()
        assert generated.key_hash == hash_api_key(generated.plaintext)
        assert len(generated.key_hash) == 64

    def test_plaintext_is_not_stored_in_the_hash(self) -> None:
        generated = generate_api_key()
        assert generated.plaintext not in generated.key_hash

    def test_keys_are_unique(self) -> None:
        keys = {generate_api_key().plaintext for _ in range(50)}
        assert len(keys) == 50


class TestExtractPrefix:
    def test_returns_prefix_for_valid_key(self) -> None:
        generated = generate_api_key()
        assert extract_prefix(generated.plaintext) == generated.prefix

    @pytest.mark.parametrize(
        "malformed",
        [
            "",
            "not-a-key",
            "nas_tooshort_secret",
            "wrong_1a2b3c4d_secret",
            "nas_1a2b3c4d_",  # empty secret
            "nas_zzzzzzzz_secret",  # prefix is not hex
            "nas_1a2b3c4_secret",  # prefix is the wrong length
        ],
    )
    def test_rejects_malformed_keys(self, malformed: str) -> None:
        assert extract_prefix(malformed) is None


class TestVerifyApiKey:
    def test_accepts_correct_key(self) -> None:
        generated = generate_api_key()
        assert verify_api_key(generated.plaintext, generated.key_hash) is True

    def test_rejects_wrong_key(self) -> None:
        generated = generate_api_key()
        other = generate_api_key()
        assert verify_api_key(other.plaintext, generated.key_hash) is False

    def test_rejects_truncated_key(self) -> None:
        generated = generate_api_key()
        assert verify_api_key(generated.plaintext[:-1], generated.key_hash) is False


class TestScopes:
    def test_values_covers_every_member(self) -> None:
        assert Scope.values() == {member.value for member in Scope}

    def test_read_only_bundle_excludes_write_scopes(self) -> None:
        assert Scope.SYNC_WRITE not in READ_ONLY_SCOPES


class TestRedact:
    def test_keeps_only_the_prefix(self) -> None:
        assert redact("nas_1a2b3c4d_supersecret", keep=4) == "nas_…"

    def test_short_values_are_fully_masked(self) -> None:
        assert redact("abc", keep=4) == "***"

    def test_empty_value(self) -> None:
        assert redact("") == ""
