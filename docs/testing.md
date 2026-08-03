# Testing strategy

**208 tests. 186 need nothing but Python; 22 need PostgreSQL.**

```bash
pytest                  # 186 unit + API tests, ~2s, no database
pytest -m integration   # 22 repository tests against real PostgreSQL
ruff check . && mypy    # lint + strict type checking
```

## Three layers, three purposes

| Layer | Count | Needs | Answers |
|---|---|---|---|
| `tests/unit/` | 130 | nothing | Is the logic correct in isolation? |
| `tests/api/` | 56 | nothing | Does a real HTTP request behave correctly end-to-end? |
| `tests/integration/` | 22 | PostgreSQL | Does the SQL, and the migration that creates it, actually work? |

### Unit tests

Pure logic, no I/O. `test_security.py` (key generation, parsing, verification),
`test_credentials.py` (store loading, permission enforcement, secret hygiene), `test_config.py`
(settings parsing, boot-time hardening), `test_domain.py` (entities, pagination arithmetic),
`test_auth_service.py`, `test_switch_service.py`.

### API tests — the important design payoff

These run the **real application**: real routing, real middleware, real authentication, real
error handlers, real Pydantic validation. Only the repository dependencies are swapped for
in-memory fakes.

```python
application.dependency_overrides[deps.get_switch_repository] = lambda: switch_repository
application.dependency_overrides[deps.get_api_key_repository] = lambda: api_key_repository
```

Because `services` depend on repository *Protocols* rather than SQLAlchemy, substituting a fake
requires no production code changes and no test-only branches. The result is that the entire
HTTP surface — status codes, error envelopes, security headers, scope enforcement, IP allowlist,
pagination — is covered in about two seconds, with no database, on any machine, in CI.

That is the concrete return on the layering described in [architecture.md](architecture.md).
The abstraction is not decorative; it is what makes this test layer possible.

### Integration tests

What fakes cannot verify: real SQL semantics, real constraint enforcement, and that the
migrations produce the schema the ORM expects.

The schema is built by running **Alembic migrations**, not `metadata.create_all()`. Each session
runs `downgrade base` → `upgrade head`, so a broken `downgrade()` fails here rather than during
a production rollback.

Skipped automatically unless `NAS_TEST_DATABASE_URL` is set, so a fresh clone and CI both work
without a database.

```bash
docker compose up -d
docker exec nas-postgres psql -U nas -d nas_dev -c "CREATE DATABASE nas_test OWNER nas;"
export NAS_TEST_DATABASE_URL=postgresql+asyncpg://nas:nas@localhost:5434/nas_test
pytest -m integration
```

## Security-specific coverage

Security properties are asserted, not assumed:

| Property | Test |
|---|---|
| Malformed / unknown / revoked / expired keys are indistinguishable | `test_auth_service.py::test_rejections_are_indistinguishable` |
| A valid prefix with the wrong secret is rejected | `test_correct_prefix_with_wrong_secret_is_rejected` |
| The presented key is never echoed in an error body | `test_error_body_never_echoes_the_presented_key` |
| A validation error does not echo the submitted value | `test_validation_error_reports_fields_without_echoing_input` |
| Credential secrets are absent from `repr()` | `test_credentials.py::TestSecretHygiene` |
| A YAML parse error does not quote file contents | `test_malformed_yaml_error_does_not_echo_file_contents` |
| A group-readable credentials file is rejected in deployed environments | `test_group_readable_file_is_rejected_when_strict` |
| The IP check runs *before* authentication | `test_rejection_happens_before_authentication` |
| `X-Forwarded-For` is ignored when the proxy is untrusted | `test_forwarded_for_is_ignored_when_proxy_is_not_trusted` |
| A prepended `X-Forwarded-For` entry cannot spoof the allowlist | `test_last_hop_wins_over_attacker_prepended_entries` |
| Staging refuses to boot with an open allowlist | `test_config.py::test_refuses_to_start_without_an_allowlist` |
| The API response carries no secret-bearing fields | `test_switches.py::test_payload_contains_no_secret_fields` |
| An unusable credentials file does not crash startup | `test_startup.py::test_survives_an_unreadable_credentials_file` |

## Regression tests earned during Milestone 1

Three real bugs surfaced during verification. Each has a test pinning it:

1. **`secrets.token_urlsafe` emits `_`**, so splitting the API key on `_` rejected most valid
   keys. Fixed with `maxsplit=2`; guarded by `test_every_generated_key_round_trips`, which
   round-trips 200 generated keys.
2. **pydantic-settings JSON-decodes `list[str]` from `.env`**, so `NAS_ALLOWED_IP_RANGES=` was a
   startup crash. Fixed with `NoDecode` plus a CSV validator; covered by `TestCsvParsing`.
3. **An unreadable credentials file raised `PermissionError`**, escaping the handler that was
   supposed to keep the API up and crash-looping the container. Fixed by mapping `OSError` to
   `CredentialError`; covered in both `test_credentials.py` and `test_startup.py`.

The first two were invisible to the type checker and the linter. They were only found by running
the thing — which is why the verification steps below are part of the definition of done.

## Conventions

- **Test names state the expected behaviour**, not the method under test:
  `test_forwarded_for_is_ignored_when_proxy_is_not_trusted`, not `test_get_client_ip_2`.
- **Docstrings explain *why* a test exists** when the reason is not obvious — especially for the
  security cases, so nobody "simplifies" one away later.
- **No shared mutable state between tests.** Fixtures are function-scoped; the integration
  session fixture saves and restores any environment variable it changes.
- **Order independence is verified**, not assumed. The suite is run in several orderings during
  verification; a session fixture leaking `NAS_DATABASE_URL` was caught exactly this way.

## Verification checklist for a milestone

Static checks alone are insufficient — all three of the Milestone 1 bugs passed `mypy --strict`.

```bash
ruff check . && ruff format --check .
mypy
pytest                                    # no database
pytest -m integration                     # real PostgreSQL
pytest tests/unit tests/api tests/integration   # alternate ordering
```

Then exercise it for real:

1. `nas db upgrade` against a live database, and confirm the schema.
2. Check for ORM/migration drift (script in [schema.md](schema.md)).
3. Every CLI command, including failure paths and exit codes.
4. Boot the server; probe `/health`, `/live`, `/ready`.
5. Call the API with no key, a bad key, an under-scoped key, and a valid key.
6. Revoke a key and confirm the next request gets `401`.
7. `grep` the server log for the plaintext key and for credential values — expect nothing.
8. `docker build`, confirm non-root and no baked-in secrets.
9. Bring up the containerised stack and call it end-to-end.

## Planned for Milestone 2

The mock driver is what makes the sync engine testable. Reconciliation outcomes — created,
updated, unchanged, marked-missing — become deterministic table-driven tests with no switch
involved, which is the only way this can be covered in CI.

Cases to cover: a VLAN appearing; a VLAN's description changing; a VLAN disappearing (soft
delete, not hard); **a switch being unreachable must not mark its VLANs missing** (the most
important one — it would otherwise look like mass VLAN deletion); the same VLAN id on multiple
switches; a partial run where some switches fail; and concurrent sync attempts being serialised
by the advisory lock.
