# Handoff — resume here

Last worked: **2026-08-04**. Phase 1 discovery is working end to end, across the
real fleet's platforms.

---

## Where things stand

| | |
|---|---|
| NAS repo | `nas/` — separate git repo inside the CRM working directory, gitignored from it |
| Remote | `https://github.com/gilbert-mutai/NAS.git` |
| `master` | `1ddceb4` — Milestone 1 |
| `nas-gilbert` | `0d79eae` — Milestone 2, pushed. Cisco driver work is **uncommitted** on top |
| Quality gate | ruff clean · `mypy --strict` clean (59 files) · **504 tests** · no migration drift · `pip-audit` clean |
| Schema | `0002_vlan_discovery`. The Cisco work needed **no migration** |
| CRM side | `netops` app complete, **uncommitted** (145 CRM tests pass) |

### Delivered

1. **Milestone 1** — foundation, security spine, device inventory.
2. **Milestone 2** — VLAN discovery, reconciliation, sync engine, API.
3. **Milestone 3** — Django `netops` app: VLAN search, availability lookup, switch
   list, sync status.
4. **Cisco drivers** — inserted ahead of hardening once the infra team reported the
   primary fleet is Cisco, not Juniper.

### Platforms

| `--vendor` | Devices | Transport | Extra |
|---|---|---|---|
| `cisco_iosxe` | Catalyst 3650, 9300, 2960 | SSH CLI :22 | `[cisco]` → netmiko |
| `cisco_nxos` | Nexus 9000 | NX-API JSON/HTTPS **:443** | none |
| `juniper` | EX, QFX | NETCONF :22 | `[juniper]` → PyEZ |
| `mock` | local dev / CI | none | none |

`cisco` (bare) is legacy and deliberately unimplemented — sync skips it with a
message naming the two replacements.

---

## Uncommitted work

Both repos have unstaged changes. Nothing is lost, but nothing is saved either.

```bash
# NAS: the Cisco driver milestone
cd nas && git status

# CRM: the netops app + the migration fix
cd .. && git status
```

The CRM side also carries a **fix to two pre-existing migrations**
(`threecx/0003`, `pm/0005`) that used PostgreSQL-only `ALTER TABLE … DROP
CONSTRAINT`. That made *every* database-backed test unrunnable under
`settings_ci` (SQLite) — CI would have failed the moment anyone added one. Worth
committing separately from the feature work.

---

## Restarting the environment

```bash
cd ~/Documents/Personal/Projects/ClientManager/nas
docker compose up -d                       # PostgreSQL on 127.0.0.1:5434
export PATH="$PWD/.venv/bin:$PATH"

nas db current                             # expect 0002_vlan_discovery
nas switch list
nas sync run                               # exits 1 on a partial run, by design

export NAS_TEST_DATABASE_URL=postgresql+asyncpg://nas:nas@localhost:5434/nas_test
pytest                                     # 504
```

**Port 5434 is deliberate.** 5432 is the CRM's PostgreSQL; 5433 is taken by an
unrelated `isp_postgres` container on this machine.

`nas_dev` holds mock switches plus deliberately broken ones (unreachable, missing
credential, legacy `cisco` vendor, Juniper with no PyEZ installed). A local run is
*expected* to be `partial` — that keeps every failure path visible.

### CRM side

```bash
cd ~/Documents/Personal/Projects/ClientManager
export DJANGO_SETTINGS_MODULE=anganicrm.settings_ci
./venv/bin/python manage.py test            # 145

# Screens against a live NAS
cd nas && NAS_SYNC_ENABLED=false nas serve --port 8126 &
cd .. && export NETOPS_LIVE_NAS_URL=http://127.0.0.1:8126 \
                NETOPS_LIVE_NAS_KEY=nas_...
./venv/bin/python manage.py test netops.tests.test_live
```

### Local experiments

```bash
NAS_MOCK_DRIFT=7 nas sync run   # changes mock VLAN sets -> real creates/updates/removals
nas sync run                    # revert -> reactivations + removals
```

Hostname markers inject driver failures: `-unreachable`, `-badauth`, `-garbled`.

---

## What the infra team still owes us

None of these block work; all of them improve it.

1. **Sample device output** — the single most valuable item. Recorded
   `show vlan brief`, `show ip interface brief`, `show vlan | json` and
   `show version` from a real 3650 and a real N9K would replace hand-built
   fixtures with genuine ones. Redacted names are fine; the *shape* is what
   matters.
2. **Is `feature nxapi` enabled on the N9Ks?** If enabling it is unacceptable, an
   NX-OS-over-SSH fallback driver is needed (the parser would be reused; only the
   transport changes).
3. **Read-only service accounts, or TACACS with enable?** `enable_password` exists
   in the credential store and is wired into the IOS-XE driver, but has never been
   exercised against a real device.
4. **Is VXLAN in use on the Nexus fleet?** Determines whether `vn-segment`
   enrichment matters, and whether the `show running-config vlan` privilege is
   worth requesting.

---

## Next: Milestone 4 — production hardening

The last piece of Phase 1. See [roadmap.md](roadmap.md).

1. **Audit log** — the `audit_log` table was designed in Milestone 1 but never
   built. Every sync trigger and every authenticated call should land in it.
2. **Rate limiting** on `/api/v1`.
3. **Real-device validation.** The highest-value item once a Cisco switch is
   reachable from staging: `pip install '.[devices]'`, register one Catalyst and one
   Nexus, and run a sync. The parsers are tested against realistic fixtures but
   **have never seen a real device**, and netmiko/PyEZ have never been installed
   here.
4. **Staging deployment** — Nginx + systemd, IP allowlist, `NAS_ENVIRONMENT=staging`
   (which refuses to boot without an allowlist).
5. **Security review** of the whole Phase 1 surface.

### Then, worth considering

- **Trunk/access detection on Cisco** via `show interfaces switchport`. Note the
  cost: every interface signature changes, so the next sync reports every Cisco
  VLAN as `updated` exactly once. Expected, not a bug.
- **Retiring the plaintext SSH password in the CRM's `pbx_backups`** app
  (`CXFTPServer.ssh_password`). NAS's credential design is the replacement pattern,
  now proven across three platforms.
- **NETCONF for IOS-XE 16.x+**, as an optimisation over CLI parsing.

---

## Things to be careful about

**Three bugs so far have passed both `ruff` and `mypy --strict`** and were caught
only by running the code. Two patterns worth remembering:

1. **Assert identity, not just counts.** A leaked loop variable made every
   reconciliation plan entry point at the wrong record; the counts were all correct.
2. **Use realistic fixtures.** `parse_version` worked on a single-line banner and
   failed on the wrapped one real devices emit.

**Editing with `sed`/string replacement after `ruff format` has run is how two of
them were introduced.** A pattern that no longer matches fails silently and leaves
half a rename behind. Prefer targeted edits against current file contents, and
re-read before patching.

## Reading order for a cold start

1. This file
2. [architecture.md](architecture.md) — layering, the driver pattern, where safety lives
3. [decisions.md](decisions.md) — why things are as they are, including the Cisco pivot
4. [testing.md](testing.md) — what "done" means, and the isolation gotcha
5. [api.md](api.md) — the contract the CRM consumes
