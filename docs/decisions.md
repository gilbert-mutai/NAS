# Design decisions

Each entry records what was decided, what it was chosen over, and why — so a future maintainer
can tell a deliberate trade-off from an accident.

---

## 1. Python + FastAPI, not Node + TypeScript + Fastify

**Context.** The original brief specified Node/TypeScript/Fastify/Prisma/Zod/Pino/BullMQ, while
inviting a better alternative.

**Decision.** Python 3.12 + FastAPI + SQLAlchemy 2.0 + Alembic + Pydantic v2 + structlog.

**Why.** The deciding factor is not team familiarity — it is **Juniper PyEZ (`junos-eznc`)**,
Juniper's own library, which speaks NETCONF/RPC and returns *structured* XML/JSON. In Node you
would SSH in and regex-parse `show vlans` CLI text, which breaks on Junos version changes and
table-width differences. Screen-scraping a production switch is the wrong foundation for a system
whose entire purpose is to be an authoritative source of VLAN data. Netmiko covers the other
vendors later behind the same driver interface.

Secondary benefits: one CI toolchain, one deployment pattern (venv + systemd, exactly how the CRM
already deploys), one language for the team to maintain.

Every element of the brief maps to a true equivalent: Fastify→FastAPI, Zod→Pydantic v2,
Prisma→SQLAlchemy+Alembic, Pino→structlog, Vitest/Supertest→pytest/httpx, ESLint+Prettier→ruff,
strict TS→`mypy --strict`. OpenAPI, PostgreSQL, Docker and Nginx are unchanged.

**Trade-off.** PyEZ is synchronous, so Milestone 2's driver calls run in a thread pool. That is a
well-understood pattern and a small cost against structured device output.

---

## 2. Separate repository, developed in the CRM's working directory

**Decision.** `nas/` is its own git repo, located inside the CRM checkout, with `/nas/` in the
CRM's `.gitignore`.

**Why.** The services share no code — only an HTTP contract — and have different languages,
dependencies, deploy targets and release cadences. Vendoring NAS into the CRM repo would mean two
CI toolchains and two deploy paths in one history, for no benefit. Keeping the directory in place
lets both be worked on in one session.

**Risk, and its mitigation.** Someone could `git add nas/` in the CRM. The `.gitignore` entry
prevents it, and carries a comment explaining why.

---

## 3. Switch credentials are not stored in the database

**Decision.** `switches.credential_ref` holds a *name*. Secrets live in a `0600` YAML file
resolved by a `CredentialProvider` Protocol.

**Considered and rejected:** envelope encryption in the database (a `secret_encrypted` column
plus a key from the environment).

**Why.** The rejected option is more code *and* weaker: the ciphertext and the application that
holds the key sit on the same host, so a host compromise yields both, while a database dump alone
yields ciphertext that is still worth attacking. Keeping secrets out of the database entirely
means **a database dump grants no device access at all** — a categorical improvement rather than
an incremental one.

It also directly improves on the CRM's existing
[`pbx_backups.CXFTPServer`](../../pbx_backups/models.py), which stores `ssh_password` as a
plaintext column.

Because consumers depend on the Protocol, moving to Vault or AWS Secrets Manager is a new class
and one factory line.

**Trade-off.** Rotating a secret requires filesystem access and a restart, rather than a database
update. For a handful of switches that is acceptable, and arguably desirable — secret changes
should be deliberate.

---

## 4. Administrative actions are CLI-only

**Decision.** No HTTP endpoint creates API keys or registers switches.

**Why.** It collapses a privilege-escalation path. A leaked read-only API key cannot mint more
keys, widen its own scope, or point NAS at an attacker-controlled device — those need shell access
to the host. The cost is that provisioning is not self-service, which for an internal service with
a handful of switches is the right side of the trade.

---

## 5. SHA-256 for API keys, not bcrypt or argon2

**Decision.** Store `sha256(key)`; compare with `hmac.compare_digest`.

**Why.** Slow KDFs exist to compensate for *low-entropy* human-chosen passwords, where an
attacker has a dictionary. These keys are 256 bits of CSPRNG output: no dictionary, no feasible
brute force. A slow KDF would add latency to every API call and buy nothing. The reasoning is
recorded in `core/security.py` so it is not "corrected" by mistake later.

**Supporting controls.** A prefix-based lookup keeps authentication to one indexed query, and the
miss path still performs a dummy comparison so response timing does not reveal which prefixes
exist.

---

## 6. Availability will be derived, never stored

**Decision.** (Milestone 2, recorded now because it shapes the schema.) No `is_available` column.
A VLAN is available when no row exists for it in the queried scope.

**Why.** A stored flag would drift out of sync with the switches, and a system whose purpose is to
be trustworthy about VLAN state cannot afford a field that is confidently wrong. Deriving it is
also simpler.

Relatedly, `vlans` is unique on `(switch_id, vlan_id)`, not `vlan_id` — the same VLAN legitimately
exists on several switches, and the search must aggregate across them.

---

## 7. Removals are soft; reachability is tri-state

**Decision.** Discovery marks vanished VLANs `state = missing` rather than deleting them.
`switches.is_reachable` is nullable, with `NULL` meaning "never checked".

**Why.** Hard deletion destroys the audit trail, and — more importantly — a briefly unreachable
switch must never be mistaken for "all its VLANs were deleted". Distinguishing "never checked"
from "checked and down" matters for the same reason: a newly registered switch is not a failing
one.

---

## 8. Boot-time hardening: refuse to start rather than start insecure

**Decision.** In `staging`/`production`, an empty `NAS_ALLOWED_IP_RANGES` raises during settings
validation.

**Why.** The failure mode being prevented is silent: a service that is the only thing able to
reach production switches, listening to the whole network, with nothing visibly wrong. A crash at
boot is loud, immediate, and caught in deployment rather than in an incident.

The same principle drives the `0600` credential-file check being fatal in deployed environments
and a warning locally.

---

## 9. Proxy headers are untrusted by default, and only the last hop counts

**Decision.** `NAS_TRUST_PROXY_HEADERS` defaults to `false`, in which case `X-Forwarded-For` is
ignored entirely. When enabled, the **last** entry is used.

**Why.** `X-Forwarded-For` is caller-controlled. Trusting the first entry — the common mistake —
lets anyone bypass the IP allowlist with one header. Nginx with `proxy_add_x_forwarded_for`
*appends* the peer it actually spoke to, so with a single trusted proxy the last entry is the only
non-spoofable one. Defaulting to `false` means a misconfiguration fails closed.

---

## 10. `vendor` as `varchar`, not a PostgreSQL `ENUM`

**Decision.** Store the enum's string value in a `varchar(32)`; the application's `Vendor` enum is
the source of truth.

**Why.** Vendor support is expected to grow, and a `varchar` means adding one needs no migration.
The cost — the database will accept an unknown vendor string — is bounded because every write path
goes through the validated enum.

---

## 11. Application factory, not a module-level `app`

**Decision.** `uvicorn nas.main:create_app --factory`; no module-level `app` object.

**Why.** A module-level `app` would construct settings and a connection pool at *import* time,
meaning every test, every migration and even `nas --help` would require `NAS_DATABASE_URL` to be
set. The factory also lets tests build an app from an explicit `Settings` instead of mutating the
environment.

---

## 12. APScheduler plus a CLI entrypoint, not Celery/Redis

**Decision.** (Milestone 2.) An embedded scheduler with a configurable interval, plus a CLI
entrypoint so a systemd timer can drive the same service layer. A PostgreSQL advisory lock
prevents overlapping runs.

**Why.** Celery + Redis is the closest analogue to the brief's BullMQ, but it means running and
monitoring Redis for one periodic job. That is real operational weight against no current
benefit. The CLI entrypoint also matches how the CRM already schedules its sync jobs. The job
layer sits behind an interface, so the swap stays cheap if queue semantics are genuinely needed.

---

## 13. Response DTOs separate from domain entities

**Decision.** `api/v1/schemas.py` defines Pydantic models distinct from `domain/entities.py`.

**Why.** It makes the public contract explicit. A field added to an entity does not silently
appear in the API, and an internal rename cannot break a consumer. The cost is a mapping function
per resource — cheap, and the place where `vendor_label` and `credential_status` are computed.

---

## 14. Health split across three endpoints

**Decision.** `/live`, `/ready`, `/health`, all unauthenticated and exempt from the IP allowlist.

**Why.** They drive different actions: a failing `/live` means restart the process; a failing
`/ready` means take it out of the load balancer but leave it alone. Collapsing them into one
endpoint makes an orchestrator restart a healthy process because its database is briefly
unavailable. Exempting probes from the allowlist means monitoring does not need whitelisting; they
expose no switch, customer or credential data.

---

## 15. Security headers and the error envelope built in Milestone 1

**Decision.** Pulled forward from the Milestone 4 hardening pass.

**Why.** Both are cross-cutting. Retrofitting a single error envelope after several routers exist
means revisiting every one; establishing it first makes every later route conform for free. The
same argument applies to the request-id middleware and the auth dependency.

Rate limiting and the `audit_log` table remain in Milestone 4 — they are additive, and the IP
allowlist plus scoped keys already bound the caller set meanwhile.

---

## Milestone 2 decisions

### The parser is a separate pure module

`juniper_parser.py` takes XML text and returns DTOs — no PyEZ import, no I/O. The riskiest part of
the Juniper integration is reading real device output across Junos releases, and this makes it
testable against recorded fixtures with the optional dependency absent. Two schemas are handled
(ELS `l2ng-l2ald-vlan-instance-group`, legacy `vlan`), matched by *local* element name so
namespaces are irrelevant.

Odd entries — no tag, out-of-range tag, non-numeric tag — are **skipped with a warning, not
fatal**. Failing the whole read over one strange VLAN would leave the switch unreadable, which
freezes its data. `defusedxml` is used because device output is untrusted input.

### Availability is derived, never stored

There is no `is_available` column. A VLAN is available when no active record exists in scope. A
stored flag would drift from the switches — precisely the failure this service exists to prevent.
Tags 0 and 4095 report `reserved` rather than `available`, because reporting them as free would
invite an engineer to try assigning one.

The lookup also returns `is_stale` and `data_as_of`. NAS is a synchronised cache; the switches
remain authoritative. Consumers need to know when not to trust an `available` verdict.

### Removals are soft, and guarded twice

`state` moves `active → missing` with history preserved. Beyond that, the reconciler **refuses**
to mark every active VLAN missing because a device reported none — far more likely a silent read
failure than a genuine mass deletion. `NAS_SYNC_ALLOW_EMPTY_DISCOVERY=true` overrides it for a
switch that really has no VLANs.

### One transaction per switch

Not one per run. A run failing on switch four keeps switches one to three. With
`sync_run_switches` rows, a partial run is durable *and* explainable. `partial` is a first-class
status for exactly this reason.

### APScheduler, not Celery + Redis

The brief asked for BullMQ or equivalent. A single periodic job does not justify a broker and a
second daemon to operate. Critically, the **advisory lock** — not a queue — is what prevents
overlapping runs, and it works across every entry point: the embedded scheduler, an HTTP POST, and
a `nas sync run` fired by hand or by a systemd timer on another host. A queue would only serialise
work that went through the queue. When NAS needs retries, fan-out or a real work queue,
`scheduler/runner.py` is the only module that changes.

### `pg_try_advisory_lock`, not `pg_advisory_lock`

A second sync request should be told "one is already running" (409), not silently queue behind it.
Session-scoped rather than transaction-scoped, so it spans the whole run; PostgreSQL releases it
automatically if the connection dies, so a crashed process cannot wedge syncing.

### API-key usage tracking is committed immediately and throttled

`mark_used` UPDATEs the `api_keys` row, taking a row lock. Committing it with the rest of the
request held that lock for the request's entire lifetime — and since every caller sharing a key
touches the same row, **all requests using that key serialised behind the slowest one**. A long
`POST /sync` stalled every other CRM call. It now commits immediately (microseconds of lock) and is
only rewritten when the stored timestamp is older than five minutes, so reads do not each cost an
UPDATE. The structured access log remains the precise record of key usage.
