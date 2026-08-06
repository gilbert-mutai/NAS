# Handoff — resume here

Last worked: **2026-08-05**. Phase 1 discovery is **live on real hardware**. The
ClientManager rename, the architecture diagrams and the docs pass are done.

**Next up: Milestone 4 — production hardening.**

---

## Current state

| | |
|---|---|
| NAS repo | `nas/` — separate git repo inside the ClientManager working dir, gitignored from it |
| NAS remote | `https://github.com/gilbert-mutai/NAS.git` · `master` / `nas-gilbert` |
| ClientManager | `master` / `gilbert` |
| Quality gate | ruff · `mypy --strict` (59 files) · **508 NAS tests** · **145 ClientManager tests** · no migration drift |
| Staging | live, syncing a production Catalyst 3650 every 15 min |

Shipped: Milestones 1–3, the Cisco drivers, the staging deployment, and the rename.
See [roadmap.md](roadmap.md).

## The deployment

Diagrammed in [architecture.md](architecture.md). Real addresses:

| Host | Mgmt LAN | Private link |
|---|---|---|
| App-Server | 192.168.95.238 | 10.10.10.238 |
| DB-Server | 192.168.95.241 | 10.10.10.241 |
| switch-01.westpoint | 192.168.95.237 | — |

NAS runs from `/opt/nas/app` as user `nas`, uvicorn on `127.0.0.1:8000` via
`nas.service`. Database `nas` on DB-Server, reachable over the `10.10.10.x` link only.
Switch is a `WS-C3650-48PD`, IOS-XE 16.6.9, **69 VLANs**, read-only account
`nas-readonly` at privilege 1 over SSH.

**venv + systemd, not Docker** — Gilbert's choice, for uniformity with ClientManager. The
Dockerfile stays for CI and parity checks.

### Working on it

```bash
# App-Server. /opt/nas is mode 0700, so run as the nas user and cd *inside* that shell
sudo -u nas sh -c 'cd /opt/nas/app && .venv/bin/nas sync run'
sudo -u nas sh -c 'cd /opt/nas/app && .venv/bin/nas sync status'
systemctl --no-pager status nas.service
journalctl -u nas.service -f

# From a laptop
ssh -N -L 8126:127.0.0.1:8000 infra-admin@192.168.95.238
# ClientManager .env:  NAS_API_BASE_URL=http://127.0.0.1:8126
```

Two things that will waste your time otherwise:

- **Django does not reload `.env`.** After editing it, kill and restart `runserver`, or
  the UI reports "Network Automation Service is not configured" while the file looks
  correct.
- **Long connection URIs break when pasted** across a terminal line wrap — the newline
  lands inside the password. Build them from a variable:
  `read -rs -p "pw: " PW; psql "postgresql://nas:$PW@10.10.10.241:5432/nas" -c "select 1"`

### Local development

```bash
cd nas
docker compose up -d                      # PostgreSQL on 127.0.0.1:5434
export PATH="$PWD/.venv/bin:$PATH"
export NAS_TEST_DATABASE_URL=postgresql+asyncpg://nas:nas@localhost:5434/nas_test
pytest                                    # 508
```

Port 5434 is deliberate: 5432 is ClientManager's PostgreSQL, 5433 belongs to an unrelated
container on this machine.

`nas_dev` holds mock switches plus deliberately broken ones — unreachable, missing
credential, legacy `cisco` vendor, Juniper with no PyEZ installed. A local run is
*expected* to be `partial`; that keeps every failure path visible.

```bash
NAS_MOCK_DRIFT=7 nas sync run   # changes mock VLAN sets -> real creates/updates/removals
nas sync run                    # revert -> reactivations + removals
```

Hostname markers inject driver failures: `-unreachable`, `-badauth`, `-garbled`.

---

## Next: Milestone 4 — production hardening

1. **Audit log.** The `audit_log` table was designed in Milestone 1 and never built.
   Every sync trigger and every authenticated call should land in it.
2. **Rate limiting** on `/api/v1`.
3. **Nginx** in front of uvicorn on App-Server, then set
   `NAS_TRUST_PROXY_HEADERS=true` — and not before, or `X-Forwarded-For` becomes
   spoofable past the IP allowlist.
4. **Deploy ClientManager's side properly.** It currently reaches NAS through an SSH
   tunnel from a laptop. When ClientManager is deployed, add its host to
   `NAS_ALLOWED_IP_RANGES` as a `/32`.
5. **Revoke the old `crm` API key** in the staging database once nothing uses it:
   `nas apikey list` then `nas apikey revoke crm`. The active key is `clientmanager`.
6. **Security review** of the whole Phase 1 surface.

## Known gaps, deliberately deferred

- **Trunk membership.** Only VLAN 1 shows ports; the other 68 are trunk-carried, and
  `show vlan brief` reports access ports only. Availability lookup is unaffected and
  accurate — but "where is this VLAN used" is sparse. Fixing needs
  `show interfaces trunk`, and note the cost: interface signatures change, so the next
  sync reports every Cisco VLAN as `updated` exactly once. Not a bug; expect it.
- **Nexus and Juniper drivers have never met real hardware.** Only the Catalyst path has.
  Both are covered by fixtures and `httpx.MockTransport`, which is not the same thing.
- **~96 SSH logins/day** to a production switch from the 15-minute schedule. If infra's
  AAA alerting objects, raise the interval rather than disabling the scheduler.
- **`pbx_backups` plaintext `ssh_password`** in ClientManager. NAS's credential design is
  the replacement pattern, now proven across three platforms.
- **The Django package is still `anganicrm`.** Renaming it touches
  `DJANGO_SETTINGS_MODULE`, systemd units and the deploy pipeline — a separate, riskier
  change, deliberately not bundled with the prose rename.

## Things that have bitten us

Four bugs so far passed both `ruff` and `mypy --strict` and were caught only by running
the code. Two patterns worth carrying forward:

- **Assert identity, not counts.** A leaked loop variable made every reconciliation plan
  entry reference the wrong record; every count was correct.
- **Use recorded output, not hand-written fixtures.** `parse_version` worked on a
  single-line banner and failed on the wrapped output real devices emit. The real 3650
  output now lives in `tests/fixtures/`.

Also: **a local test run is not automatically equivalent to CI.** A local `.env` with
`DEBUG=True` masked a staticfiles-manifest failure that turned CI red. `settings_ci` now
pins `DEBUG=False` so the two match.

And: **editing with `sed` after `ruff format` has run** is how two of those bugs were
introduced — a pattern that no longer matches fails silently and leaves half a rename
behind. Prefer targeted edits against current file contents.

## Reading order for a cold start

1. This file
2. [architecture.md](architecture.md) — topology diagram, layering, where safety lives
3. [decisions.md](decisions.md) — why things are as they are, including the Cisco pivot
4. [deployment.md](deployment.md) — matches what is actually running
5. [testing.md](testing.md) — what "done" means
