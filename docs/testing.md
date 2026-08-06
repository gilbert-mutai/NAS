# Testing strategy

**588 tests. 504 need nothing but Python; 84 need PostgreSQL.**

(Counts below are per-milestone snapshots kept for context; the figures above are current.)

```bash
pytest                  # all 588; the 84 integration ones need NAS_TEST_DATABASE_URL
pytest -m "not integration"   # 504, no database, a few seconds
pytest -m integration   # 84 repository/service tests against real PostgreSQL
ruff check . && mypy    # lint + strict type checking
```

## Three layers, three purposes

| Layer | Count | Needs | Answers |
|---|---|---|---|
| `tests/unit/` | 366 | nothing | Is the logic correct in isolation? |
| `tests/api/` | 138 | nothing | Does a real HTTP request behave correctly end-to-end? |
| `tests/integration/` | 84 | PostgreSQL | Does the SQL, and the migration that creates it, actually work? |

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

---

## Milestone 2 additions

417 tests: 349 run with no database (unit + API against in-memory fakes), 68 integration tests
against live PostgreSQL.

### The tests that matter most

| Test | Guards |
|---|---|
| `test_sync_service.py::TestTheInvariant` | An unreachable switch never marks VLANs missing, and a failure is attributed to the right switch |
| `test_reconciler.py::TestPlanIdentity` | Plan entries reference the *matching* record — see below |
| `test_reconciler.py::TestMassRemovalGuard` | A device reporting zero VLANs cannot wipe a switch |
| `test_sync_service.py::TestConcurrency` | Two concurrent syncs produce one run, not two |
| `test_auth_service.py::TestUsageThrottling` | API-key bookkeeping does not serialise the API |
| `test_juniper_parser.py` | Both Junos schemas, odd entries skipped not fatal, XXE/billion-laughs blocked |

### Why `TestPlanIdentity` exists

A leaked loop variable once made every reconciliation plan entry reference the *last* stored
record instead of the matching one. Counts were still correct, `ruff` passed, and `mypy --strict`
passed — the leaked name was a valid `Vlan`. The only visible symptom was VLANs staying `missing`
after being rediscovered.

The lesson generalises: **assert identity, not just totals.** A test that checks
`len(plan.to_update) == 3` would not have caught it. The integration counterpart is
`test_active_count_equals_discovered_count` — after a successful sync, active must equal
discovered.

### Test isolation with a live database

Rollback alone is **not** sufficient. Application code commits mid-request by design (`mark_used`
commits immediately so it does not hold an API-key row lock for the request's lifetime), and
anything committed leaks into the next test. The `db` fixture therefore truncates every table
after each test, with the table list read from `Base.metadata` so a future migration is covered
automatically.

Cleanup lives on the `db` fixture rather than `session`, because sync-service tests take `db`
directly — the service owns its own sessions. Attaching isolation to `session` alone produced
tests that passed alone and failed in a suite.

### Exercising drift locally

`NAS_MOCK_DRIFT=<int>` changes the mock driver's reported VLAN set, so a re-sync produces genuine
creations, updates and removals without editing code. Hostname markers `unreachable`, `badauth`
and `garbled` inject the three driver failure classes.

---

## Cisco driver testing

504 tests. 85 cover the Cisco work, and **none of them need a switch, netmiko, or
PyEZ installed.**

| File | Covers |
|---|---|
| `test_cisco_iosxe_parser.py` | `show vlan brief`: wrapped port lists, variable column widths, IOS default VLANs, suspended VLANs, rejected commands |
| `test_cisco_nxos_parser.py` | NX-OS JSON: ins_api envelope, single-row-as-object, scalar-or-list ports, `vn-segment`, error codes |
| `test_cisco_drivers.py` | NX-API request construction and status mapping via `httpx.MockTransport`; IOS-XE connect kwargs and the missing-dependency path |

### Two properties worth restating

**A rejected command must raise, not return an empty list.** Tested explicitly for
both platforms. Returning `()` from a failed read would reach the reconciler as
"every VLAN on this switch was deleted" — the mass-deletion failure mode the whole
design guards against. The reconciler's own empty-discovery guard is the second
line of defence, not the first.

**Every driver failure must be catchable as `DriverError`.** One `except` clause in
the sync service is what keeps an unreadable switch from being reconciled, so the
NX-OS tests assert it across auth failures, 404s, 500s and non-JSON bodies.

### Testing NX-API with no device

`httpx.MockTransport` with a handler that dispatches on the NX-API `input` command,
injected directly onto the driver's client. That exercises real request
construction, real status-code handling and real parsing — everything except the
socket.

### A bug realistic fixtures caught

`parse_version` originally filtered lines case-sensitively for `IOS`/`Software`.
Real `show version` output **wraps**, putting `Version 16.12.05b` on a continuation
line whose only marker is uppercase `RELEASE SOFTWARE`. A single-line fixture passed;
the wrapped one did not. The fix scans the whole output, anchored on a digit, and is
now verified against 3650, 2960 and 9300 banner shapes.

---

## Real-device fixtures

`tests/fixtures/switch01_westpoint_show_vlan_brief.txt` is `show vlan brief` recorded from
a production WS-C3650-48PD (IOS-XE 16.6.9, stack member 3, 69 VLANs).
`tests/unit/test_cisco_iosxe_real_device.py` asserts against it.

This matters more than the count of tests it adds. Every other Cisco fixture in the suite
was written by hand from knowledge of the format — a guess, however well informed. This one
is evidence. It caught a real bug immediately: `parse_version` filtered lines
case-sensitively and missed the continuation line that real `show version` output wraps onto.

What the real output exercises that hand-written fixtures did not:

- VLAN 1 carrying **52 ports across 13 continuation lines**
- 68 of 69 VLANs with a **blank** Ports column, because they are trunk-carried
- Stack-member interface naming (`Gi3/0/x`, not `Gi1/0/x`)
- Names like `VLAN0095-LDAP` and `prod.js_db-svrs` — digits, dots, underscores
- All four IOS-created defaults (1002–1005) present and needing exclusion

When another platform reaches real hardware, record its output the same way.

---

## Milestone 4 — audit trail

588 tests. The audit work added 66: 26 API, 24 unit, 16 integration.

| Test | Guards |
|---|---|
| `test_audit_repository.py::TestServiceCommitsIndependently` | An audit entry survives a request whose transaction rolls back — the property that makes the rejected-sync entry possible at all |
| `test_audit.py::TestRejectedSyncIsAudited` | The 409 and an unexpected crash are both recorded, not just the happy path |
| `test_audit.py::TestScopeDenialIsAudited::test_no_sync_ran` | The denial happens *before* the service is reached, so the entry is not describing a sync that already ran |
| `test_audit_actor.py::TestHostileInput` | Newlines, ANSI escapes and NUL bytes are stripped from a caller-supplied identity |
| `test_audit_repository.py::TestSurvivingAKeyDeletion` | Deleting an API key nulls the FK but keeps the name — revocation cannot erase history |
| `test_actor_forwarding.py` (ClientManager) | The acting user is forwarded on the sync trigger and on nothing else |

### Why the transaction test needs a real database

`AuditService` holds a session *factory* and commits on its own session, so an entry
written during a request that later raises is not rolled back with it. No in-memory
fake can demonstrate that — the fake has no transaction. Removing the service's
`commit()` turns two integration tests red and leaves every API test green, which is
exactly the right split.

### What was learned mutation-testing this suite

Each of the guards above was checked by breaking the thing it protects and confirming
the test went red. Two findings worth keeping:

- **One test did not earn its docstring.** "Identical timestamps paginate without
  loss" passed with *and* without the `id DESC` tiebreak it was written to protect:
  at this table size PostgreSQL walks the index backwards and returns equal keys in
  reverse insertion order anyway. The tiebreak stays (SQL does not promise that
  ordering), but the test now says plainly that it would not catch its removal. An
  untested guard that reads like a tested one is worse than no test.
- **A scripted mutation that silently fails to apply looks like a passing test.**
  The first attempt at the above used a string replacement that no longer matched
  after `ruff format`, so "the test still passes" was measuring nothing. Assert the
  replacement applied before trusting the result.

### The actor is not proof

`actor` is asserted by the caller. The tests treat it as untrusted input — sent as a
real HTTP header carrying a newline, an ANSI escape and `<script>` — and assert the
stored value is clean. Sanitisation lives inside `AuditService` rather than at the
edge, so `FakeAuditService` applies it too; otherwise an API test could assert a
hostile string was stored verbatim and pass while production stored the cleaned one.
