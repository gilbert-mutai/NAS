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
| No `audit_log` table | Phase 1 has no mutating endpoints; the structured access log covers read activity |
| No TLS terminated by NAS | Nginx terminates TLS in staging/production; local runs on loopback |
| No dependency vulnerability scanning in CI | Added with the CI hardening pass; dependencies are pinned to compatible ranges |
| Credentials unencrypted at rest on disk | `0600` plus host-level controls; full-disk encryption is the right layer for this |

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
