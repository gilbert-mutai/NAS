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

Secondary benefits: one CI toolchain, one deployment pattern (venv + systemd, exactly how ClientManager
already deploys), one language for the team to maintain.

Every element of the brief maps to a true equivalent: Fastify→FastAPI, Zod→Pydantic v2,
Prisma→SQLAlchemy+Alembic, Pino→structlog, Vitest/Supertest→pytest/httpx, ESLint+Prettier→ruff,
strict TS→`mypy --strict`. OpenAPI, PostgreSQL, Docker and Nginx are unchanged.

**Trade-off.** PyEZ is synchronous, so Milestone 2's driver calls run in a thread pool. That is a
well-understood pattern and a small cost against structured device output.

---

## 2. Separate repository, developed in ClientManager's working directory

**Decision.** `nas/` is its own git repo, located inside ClientManager checkout, with `/nas/` in the
ClientManager's `.gitignore`.

**Why.** The services share no code — only an HTTP contract — and have different languages,
dependencies, deploy targets and release cadences. Vendoring NAS into ClientManager repo would mean two
CI toolchains and two deploy paths in one history, for no benefit. Keeping the directory in place
lets both be worked on in one session.

**Risk, and its mitigation.** Someone could `git add nas/` in ClientManager. The `.gitignore` entry
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

It also directly improves on ClientManager's existing
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
benefit. The CLI entrypoint also matches how ClientManager already schedules its sync jobs. The job
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

Rate limiting and the `audit_log` table remained in Milestone 4 — they are additive, and the IP
allowlist plus scoped keys already bound the caller set meanwhile. The audit log has since been
built; see the Milestone 4 section.

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
`POST /sync` stalled every other ClientManager call. It now commits immediately (microseconds of lock) and is
only rewritten when the stored timestamp is older than five minutes, so reads do not each cost an
UPDATE. The structured access log remains the precise record of key usage.

---

## Cisco drivers (2026-08-04)

### Context: the primary fleet is Cisco, not Juniper

The infrastructure team reported that the primary switches are Catalyst 3650s,
other Catalyst models, and Nexus 9000s; Juniper is secondary. Milestones 1–3 were
built with Juniper as the first driver.

**What this changed:** two drivers, two enum members, a `DriverOptions` dataclass,
and a `netmiko` extra.

**What it did not change:** the schema (no migration — `switches.vendor` carries no
CHECK constraint by design), the reconciliation engine, the sync service, the API,
and the `netops` UI. The abstraction held. The VLAN model also fitted Cisco without
alteration: IOS's `interface Vlan110` maps to `l3_interface`, and NX-OS's
`vn-segment` maps to `vxlan_vni`.

### Revisiting the Python decision

The original justification for Python over the brief's Node/TypeScript was PyEZ's
structured NETCONF output for Juniper. That argument now applies to a *secondary*
vendor and is much weaker than when it was made.

The conclusion still holds, for different reasons:

* **TextFSM / `ntc-templates`**, the ecosystem for parsing Cisco CLI output, is
  Python-only. For older Catalyst gear there is no structured alternative, and no
  Node equivalent of that ecosystem.
* **`netmiko`** (multi-vendor network SSH) and **`ncclient`** (NETCONF) are
  Python-first.
* NX-API is plain JSON over HTTPS, so it is language-neutral.

Recorded here rather than left as a stale rationale in the Milestone 1 notes.

### Cisco is two platforms, not one vendor

`Vendor` gained `cisco_iosxe` and `cisco_nxos` rather than a single `cisco`,
because the registry keys on vendor and the two platforms differ in command
syntax, structured-output mechanism, *and transport*. One driver would have been a
branch-on-platform conditional in every method.

The legacy `cisco` value is retained so existing rows still load, but is
deliberately **not** implemented: sync skips it with a message naming the two
replacements and the port gotcha. That guidance is exposed via
`registry.unsupported_reason()` — the sync service decides to skip without ever
constructing a driver, so without that function the hint would have been
unreachable in the normal path.

### IOS-XE: SSH CLI, not NETCONF

NETCONF/YANG on IOS-XE requires 16.x and is off by default; Catalyst 3650s in the
field run anything from 3.x upward. Parsing `show vlan brief` works on every one of
them, so **the firmware version stopped being a prerequisite** — which also removed
a blocking question from the infra team. NETCONF is a worthwhile optimisation later
for the subset that supports it.

A hand-written parser was chosen over `ntc-templates`: for two stable, well-known
commands, the template library plus its resolution layer is a large dependency for
~150 lines. Column geometry is derived from the separator row rather than
hardcoded, which is what makes it robust across platforms with different field
widths. If a third or fourth Cisco command is ever needed, revisit this.

### NX-OS: NX-API over HTTPS

`show vlan | json` returns real structured data — no scraping, no per-release drift
to chase. It also needs no thread offload, since `httpx` is natively async; the
PyEZ and netmiko drivers both block and are dispatched to worker threads.

Two NX-OS JSON conventions are normalised in the parser, and both are the kind of
thing that works in a lab and fails in production:

* `ROW_vlanbriefxbrief` is a **list** for several VLANs and a bare **object** for
  one.
* `vlanshowplist-ifidx` is a **string** for a short port list and a **list** for a
  long one.

**A Nexus registered on port 22 is refused** with the exact re-registration command,
rather than silently substituting 443. Guessing a different port than the operator
specified is worse than failing.

### Interface mode is UNKNOWN on Cisco, deliberately

Neither `show vlan brief` nor `show vlan | json` reliably distinguishes access from
trunk membership. Determining it needs `show interfaces switchport`, a third, much
more verbose command.

Reporting `unknown` is better than guessing, and — because the reconciler compares
`(name, mode)` pairs — a *consistent* `unknown` produces no spurious changes.

**Follow-up, and it has a cost:** if trunk detection is added later, every
interface signature changes, so the next sync will report every Cisco VLAN as
`updated` exactly once. Harmless, but worth expecting rather than debugging.

### VLAN status is recorded in `raw`, not promoted to a column

IOS reports `active`, `suspended`, `act/lshut`; NX-OS reports `vlanshowbr-vlanstate`
and `shutstate`. None of it affects availability — **a suspended VLAN still occupies
its ID** — so adding a column would have meant a migration for information nothing
consumes. It is kept in the `raw` JSONB for audit.

### Device TLS verification defaults to off

`NAS_DRIVER_VERIFY_TLS=false` by default. Nexus switches ship self-signed
certificates and NAS reaches them over a private management network, so
verification would fail on essentially every device while the network boundary does
the real work. It is a genuine weakening of transport security, so it is a
documented setting rather than a hardcoded `verify=False`.


## Milestone 4 (2026-08-06)

### The audit log stores who NAS *proved* and who it was *told*, in separate columns

NAS authenticates an API key. It has no way to authenticate the human behind a
request — ClientManager does that, then calls NAS with its own key. So an audit row
carries both `api_key_name` (proved) and `actor` (asserted by the caller, forwarded
in `X-Actor`).

**Why not just the actor.** It would present an unverified claim as fact. Anything
holding the ClientManager key can send any actor string; a trail that showed only
`gilbert@angani.co` would read as proof of who acted when it is really ClientManager's
word for it.

**Why not just the key.** Then the trail says "clientmanager triggered a sync" and
cannot answer "who". The sync button is staff-only precisely because it reaches
production hardware, so accountability is the point of recording it at all.

Keeping both makes the limit visible in the data rather than buried in a document.
`AuditEntry.attribution` renders it as "actor via key" for exactly that reason.

### The audit write belongs in the service, not the route

First implemented at the API route, which recorded only HTTP-triggered syncs. The
scheduler and `nas sync run` produced no entry at all.

**Why that was wrong.** `sync_runs` still recorded those runs, so nothing was
invisible — but attribution existed only for the API path, and the CLI is the least
supervised route to a production switch. Someone with shell access could sync a
production device and the trail would name nobody. Auditing the best-supervised path
and skipping the worst is close to backwards.

**The fix.** `SyncService.run` is the single choke point every trigger passes through,
so the write moved there. A future caller is audited by construction rather than by
remembering. The route's remaining job is to supply what the service cannot know —
the authenticated key, the forwarded actor, the client address — which travels as one
`AuditContext` rather than four loose parameters.

**Cost.** `SyncService` now depends on `AuditService`, and that dependency is
**required**, not optional with a `None` default. An audit gap should not be creatable
by omitting an argument, so mypy fails every construction site that forgets it. The
same fail-closed reasoning as `NETOPS_ENABLED` defaulting to False.

### Audit entries commit on their own session

`AuditService` takes a session *factory*, not a session, and commits independently of
the request transaction.

**Why.** A `POST /sync` rejected with 409 raises, so FastAPI rolls the request
transaction back — and an entry written on that session would vanish with it. A
rejected attempt is exactly what the trail should hold: a burst of them is somebody
clicking a button that appears not to work. Same reasoning as
`ApiKeyRepository.mark_used`, for a different reason.

**Cost.** One extra connection checkout per audited action, and the entry is not
atomic with the operation. Acceptable because the operation it describes has already
had its effect on the devices by then.

### A failed audit write degrades rather than failing the request

The write is best-effort. A failure logs at `error` with the whole entry inline and
returns `None`.

**Why not fail the request.** By the time the entry is written the switches have been
polled. A 500 would report failure for work that succeeded, and the caller's retry
would poll them again — turning an audit outage into a second sync against production
hardware.

**Why not fail silently.** An audit log that quietly drops entries is worthless. The
event still lands in the structured log, which is shipped to journald; only its
queryable form is lost.

This is a real trade-off, not a free choice: if a guaranteed queryable trail is ever
required, it needs a different design — write-ahead, or refusing the request — and
Phase 1 does not have one.

### Reads are not audited

Only actions that reach a device or change state, plus scope denials. The structured
access log already records every request, and a row per VLAN lookup would bury a sync
against production hardware under routine traffic. Authentication failures are also
excluded: the presented key is unknown, so there is nothing to attribute the row to,
and an anonymous caller could otherwise fill the table by looping.

### `audit:read` is outside the read-only bundle

The trail records who triggered what. A key issued to look up VLANs has no business
enumerating that, so `READ_ONLY_SCOPES` — what ClientManager gets — deliberately
excludes it. Reading the trail needs a key minted for an operator.
