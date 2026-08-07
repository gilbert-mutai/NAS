# Security

## The trust boundary

NAS exists so that **exactly one process, on one host, inside the private network, holds the
credentials that can reach production switches.**

ClientManager is internet-adjacent with a broad attack surface: sessions, file uploads, email
sending, many user roles, a large dependency tree. NAS is small, single-purpose, private, and
exposes a read-only API. Moving device access behind that boundary means a ClientManager compromise does
not become a network compromise.

Consequently ClientManager **never**: opens an SSH connection, stores a switch credential, or executes
a network command. It calls an HTTP API and renders the result.

## Credential handling

### Not in the database

The `switches` table has no password, key or secret column. It stores `credential_ref` — a name.
Secrets live in a `0600` YAML file outside the repository, resolved at use time by a
`CredentialProvider`.

**A NAS database dump therefore grants no access to any network device.**

This is a deliberate departure from the pattern in ClientManager's [`pbx_backups`
app](../../pbx_backups/models.py), where `CXFTPServer.ssh_password` is a plaintext
`CharField` — a database dump there hands over production SSH access. NAS should not reproduce
that, and once Milestone 3 lands, the same pattern can be used to retire it.

### Defences in the credential path

| Control | Where |
|---|---|
| File must be `0600` in staging/production; startup refuses otherwise | `FileCredentialProvider._check_permissions` |
| Secret fields are `repr=False`, so logging or a traceback cannot print them | `DeviceCredential` |
| YAML parse errors omit the parser's context snippet, which would quote the offending line | `FileCredentialProvider._load` |
| Every store failure becomes `CredentialError`, so an unreadable file cannot crash-loop startup | `FileCredentialProvider._load` |
| API reports only `resolved`/`missing`/`not_configured`, never credential content | `SwitchService`, `SwitchResponse` |
| `nas credentials check` prints credential *names* only | `cli.credentials_check` |
| Provider is a Protocol, so Vault or AWS Secrets Manager is a new class, not a refactor | `CredentialProvider` |

Grant NAS a **read-only** account on each switch. Phase 1 only reads device state; nothing in
this service modifies switch configuration. Least privilege limits the blast radius if the NAS
host is ever compromised.

## API authentication

Format: `nas_<8 hex prefix>_<43 char base64url secret>` — 256 bits of CSPRNG entropy.

Only `prefix` and `sha256(full_key)` are persisted. Authentication looks the row up by prefix
(indexed, one query), then compares digests with `hmac.compare_digest`.

**Why SHA-256 and not bcrypt/argon2.** Those exist to compensate for *low-entropy* human-chosen
passwords, where an attacker has a dictionary to work through. These keys are 256 bits of random
data: there is no dictionary and no feasible brute force. A deliberately slow KDF would add
latency to every single API call and buy no security. The reasoning is recorded in
`core/security.py` so it is not "fixed" later by mistake.

Additional controls:

- **Uniform rejection.** Malformed, unknown, revoked and expired keys all return the same code
  and message, so a caller cannot enumerate valid prefixes.
- **Constant-time comparison on the miss path too.** An unknown prefix is still compared against
  a dummy digest, so response time does not reveal which prefixes exist.
- **Scopes per route.** `Depends(require_scopes(Scope.SWITCHES_READ))` declares the privilege at
  the route, so an under-privileged key is rejected before the handler runs.
- **Revocation and expiry** are checked on every request; revocation takes effect immediately
  (verified end-to-end: `200` before, `401` after).
- **Keys are never logged.** Verified by grepping a live server's log for the plaintext key.

### Administrative actions are CLI-only

There is no HTTP endpoint to create a key or register a switch. Both require shell access to the
host. A leaked API key therefore cannot be used to mint more keys, escalate scope, or point NAS
at an attacker-controlled device.

## Network controls

**IP allowlist.** `NAS_ALLOWED_IP_RANGES` accepts IPs and CIDRs. Evaluated *before*
authentication. An empty allowlist means "allow any" and is permitted only in local/test — in
`staging` or `production` the service **refuses to start** without one, because silently exposing
the only component with switch access is worse than failing to boot.

**Proxy header trust is explicit and off by default.** When `NAS_TRUST_PROXY_HEADERS=false`,
`X-Forwarded-For` is ignored entirely and the socket peer is used, so a caller cannot spoof its
way past the allowlist by setting its own header. When enabled, the **last** entry is used —
Nginx with `proxy_add_x_forwarded_for` appends the address of the peer that contacted it, so with
one trusted proxy the last hop is the only non-spoofable entry. Earlier entries are
attacker-controlled. Four tests in `tests/api/test_ip_allowlist.py` pin this behaviour,
including the prepended-attacker-entry case.

**Security headers** on every response: `X-Content-Type-Options: nosniff`,
`X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`, plus HSTS in
deployed environments.

**Input validation.** Pydantic validates and coerces every query parameter, with bounds on page
size and string lengths. All SQL is parameterised via SQLAlchemy; `%` and `_` in search terms are
escaped so a search term cannot broaden the query.

## Container posture

- Multi-stage build: build tools are absent from the runtime image.
- Runs as an unprivileged user (uid 1001 by default), never root — verified.
- `.dockerignore` excludes `.env`, `credentials.yaml`, `*.pem`, `*.key`, so no secret is baked
  into an image layer — verified by inspecting the built image.
- Credentials are bind-mounted read-only at a path outside the image — verified that writes are
  refused.

## Threat notes

| Threat | Mitigation | Residual risk |
|---|---|---|
| ClientManager compromised | No credentials or device access in ClientManager; NAS key is read-only and scoped | Attacker can read VLAN inventory via ClientManager's key |
| NAS database exfiltrated | No secrets stored; API key hashes are not replayable | Inventory metadata (hostnames, sites) is disclosed |
| API key leaked | Scoped read-only; IP allowlist still applies; revocable in one command | Read access from an allowlisted host until revoked |
| Stolen NAS host disk | Credentials are `0600` and outside the DB, but unencrypted at rest | Full switch access. **Mitigate with full-disk encryption on the NAS host** |
| Source IP spoofing | Forwarding headers untrusted by default; last-hop only when enabled | Misconfiguring `TRUST_PROXY_HEADERS=true` without a real proxy in front would be exploitable |
| Credentials in logs | `repr=False` on secret fields; YAML errors omit file content; verified by log grep | A future contributor could log a `DeviceCredential` field explicitly |

## Milestone 1 security review

**Verified working:** credential exclusion from the schema; `0600` enforcement; secret-free
`repr`; API key hashing and uniform rejection; constant-time comparison; scope enforcement
(`403` with missing-scope detail); immediate revocation; IP allowlist ahead of auth; proxy-header
spoofing rejection; boot refusal on an open allowlist in staging; security headers; no secret in
logs; non-root container with no baked-in secrets; error envelope leaking no internals;
validation errors not echoing input.

**Deferred to Milestone 4, and why it is acceptable now:**

| Gap | Why it can wait |
|---|---|
| No rate limiting | IP allowlist plus scoped keys bound the caller set to hosts you control |
| No TLS terminated by NAS | Nginx terminates TLS in staging/production; local runs on loopback |
| No dependency vulnerability scanning in CI | Added with the CI hardening pass; dependencies are pinned to compatible ranges |
| Credentials unencrypted at rest on disk | `0600` plus host-level controls; full-disk encryption is the right layer for this |

## Audit trail — built in Milestone 4

The `audit_log` table was designed in Milestone 1 and deferred. It said at the time that "Phase 1
has no mutating endpoints", which had stopped being true: `POST /sync` and the `sync:write` scope
exist, and the ClientManager "Sync Now" button reaches a production switch. That claim is
withdrawn.

**What is recorded**

| Action | When |
|---|---|
| `sync.trigger` / `success` | A run completed; the entry points at the `sync_runs` row |
| `sync.trigger` / `error` | Rejected with 409 (a run was already going), or the run raised |
| `auth.denied` / `denied` | An authenticated key reached for a scope it does not hold |

**Every trigger is covered — API, scheduler and CLI.** The write lives in
`SyncService.run`, the one choke point all three pass through, so a new caller is
audited by construction. It was briefly in the API route instead, which left the
scheduler and `nas sync run` silent; the CLI is the least supervised path to a
production switch, so that was the worst one to miss.

Attribution differs by path, and the differences are meaningful rather than
incidental:

| Trigger | `actor` | `api_key_name` | `source_ip` |
|---|---|---|---|
| API | forwarded `X-Actor` | the authenticated key | client address |
| CLI | `SUDO_USER`, else the login name | none — shell access *is* the authorisation | `cli` |
| Scheduler | none | none | none |

A scheduled entry is `unattributed`, which is the honest record of machine-initiated
work rather than a placeholder that would read like an identity. `detail->>'trigger'`
distinguishes the three, so scheduled volume can be filtered out of a report without
being excluded from the record.

**What is deliberately not recorded**

- **Routine reads.** The structured access log already has every request. A row per VLAN lookup
  would bury a sync against production hardware under ordinary traffic.
- **Authentication failures.** The presented key is unknown by definition, so there is nothing to
  attribute the row to — and an unauthenticated caller could otherwise fill the table by looping.
  Those stay in the access log.

**Attribution, and its limit**

`actor` is forwarded by the caller — ClientManager sends the logged-in user's email on
`POST /sync` via `X-Actor`. **NAS does not verify it.** NAS authenticates the API key, not the
person behind it, so the value is exactly as trustworthy as the calling application. It is stored
beside `api_key_name`, never instead of it: the key is what NAS proved, the actor is what it was
told. Read them together, and treat `actor` as evidence rather than proof.

The header is untrusted input. It is sanitised inside `AuditService` — not at the edge, so no
call path can bypass it — stripping anything outside `[\w.@+\- ]` and truncating to 320
characters. A newline in a field that reaches log lines is a log-forging primitive, and nothing
in NAS depends on an intermediary having rejected it first.

**Integrity**

- No update or delete path exists in the application; the repository Protocol has `record` and
  `list` and nothing else. Retention is a DBA task, so "clear the evidence" is not a one-line
  code change.
- The table has no `updated_at` and must not gain one.
- Deleting an API key nulls the FK but keeps the name snapshot, so revocation cannot erase
  history.
- Reading the trail needs the `audit:read` scope, which is **not** in the read-only bundle issued
  to ClientManager. A key that looks up VLANs cannot enumerate who triggered what.

**Availability trade-off, stated plainly**

A failed audit write does not fail the operation. By the time the entry is written the switches
have been polled; raising would report failure for work that succeeded and the caller's retry
would poll them again. The failure is logged at `error` with the entire entry inline, so the
event survives in the structured log and only its queryable form is lost. This is a deliberate
choice of degradation over silence *and* over false failure — if a queryable trail must be
guaranteed, that needs a different design (write-ahead, or refusing the request), which Phase 1
does not have.

**Recommendations before staging goes live**

1. Enable full-disk encryption on the NAS host.
2. Create a dedicated read-only account on each switch; do not reuse an admin account.
3. Set `NAS_ALLOWED_IP_RANGES` to ClientManager host only — `/32`, not a subnet.
4. Terminate TLS at Nginx and set `NAS_TRUST_PROXY_HEADERS=true` **only** once Nginx is actually
   in front and sets `X-Forwarded-For`.
5. Consider `NAS_DOCS_ENABLED=false` in production; the OpenAPI document describes the whole
   attack surface.
6. Give the API key an expiry (`--expires-days`) and rotate on a schedule.
7. Restrict the NAS PostgreSQL role to its own database; ClientManager's role must have no access.
