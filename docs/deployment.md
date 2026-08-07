# Deployment

Three modes: local development, containerised (parity check), and staging/production behind
Nginx with systemd.

The staging/production path deliberately mirrors how ClientManager already deploys — venv, systemd,
Nginx — so there is one operational model to learn rather than two.

---

## Local development

```bash
docker compose up -d                     # PostgreSQL on 127.0.0.1:5434

python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
export PATH="$PWD/.venv/bin:$PATH"

cp .env.example .env
cp credentials.example.yaml credentials.yaml && chmod 600 credentials.yaml

nas db upgrade
nas apikey create --name clientmanager --scopes switches:read,vlans:read,sync:read
nas serve --reload
```

`nas serve` binds `127.0.0.1` by default. Exposing the service is a deliberate act
(`--host 0.0.0.0`).

**Why port 5434.** 5432 is ClientManager's PostgreSQL. 5433 was already taken by an unrelated
container on the development machine. Keeping NAS on 5434 means all three coexist.

---

## Containerised (parity check)

```bash
NAS_UID=$(id -u) NAS_GID=$(id -g) docker compose --profile app up --build
```

`NAS_UID`/`NAS_GID` matter: `credentials.yaml` is `0600` and owned by your host user, so the
container must run as that UID to read it. Loosening the file to `0644` to satisfy the container
would defeat the protection the mode provides. Either way the process is unprivileged.

The app container runs `alembic upgrade head` before starting uvicorn, and mounts
`credentials.yaml` read-only at `/run/secrets/nas-credentials.yaml`.

Build a release image:

```bash
docker build -t nas:0.1.0 .
docker run --rm nas:0.1.0 id      # uid=1001(nas) — never root
```

---

## Staging / production

### 1. PostgreSQL

Give NAS its own database and role. ClientManager's role must have no access to it.

**Generate the password with a URL-safe alphabet:**

```bash
openssl rand -hex 32        # 256 bits, hex only
```

Not `openssl rand -base64`. The password ends up inside `NAS_DATABASE_URL`, and base64
can emit `/`, `+` and `=`. A `/` terminates the password field in a connection URI and
starts the path, so the password is silently truncated and authentication fails with a
misleading "password authentication failed" — the credential is right, the parsing is
not. Hex avoids the problem entirely and needs no percent-encoding.

```sql
CREATE ROLE nas WITH LOGIN PASSWORD '<hex-password>';
CREATE DATABASE nas OWNER nas;
REVOKE ALL ON DATABASE nas FROM PUBLIC;
```

Verify the revoke took effect — `\l nas` should show `nas=CTc/nas` and no `=Tc/` entry
for PUBLIC, meaning no other role can even connect.

**When testing the connection, avoid the URI form**, so escaping cannot be the variable:

```bash
read -rs -p "nas password: " PGPASSWORD; export PGPASSWORD; echo
psql -h <db-private-ip> -U nas -d nas -c "select current_user, current_database()"
unset PGPASSWORD
```

`read -rs` also keeps the password out of shell history.

### 1b. If the database is on a separate host

Bind PostgreSQL to the private server-to-server interface only — never `*`, and never the
interface that carries switch or general LAN traffic:

```conf
# /etc/postgresql/16/main/conf.d/10-nas.conf   (mode 0640, owner postgres)
listen_addresses = '<db-private-ip>'
```

A drop-in under `conf.d` is preferable to editing `postgresql.conf`: package upgrades
never conflict with it, and the change is visible in one short file.

`pg_hba.conf` has no include mechanism, so it is edited directly. Use a `/32` for the
app server rather than the whole subnet:

```conf
host    nas    nas    <app-private-ip>/32    scram-sha-256
```

Then confirm what the server actually loaded, rather than what the file appears to say:

```bash
sudo systemctl restart postgresql@16-main
sudo -u postgres psql -c "SHOW listen_addresses"
sudo -u postgres psql -c "
select line_number, type, database, user_name, address, auth_method, error
from pg_hba_file_rules order by line_number;"
```

`error` must be null on every row, and no broader rule may appear above the `nas` rule —
PostgreSQL uses the first match.

Finally, prove the segmentation holds. From the app server:

```bash
nc -zv <db-private-ip> 5432      # succeeds
nc -zv <db-lan-ip>     5432      # must be refused
```

Run both **from the app server**. Run from the database host they prove only that it is
listening locally, and the output looks identical either way.

Note that `nc` tests TCP reachability only; `pg_hba` and the password are
authentication-layer and are first exercised by `nas db upgrade`.

### 2. Application user and checkout

```bash
sudo useradd --system --create-home --home-dir /opt/nas --shell /usr/sbin/nologin nas
sudo -u nas git clone https://github.com/gilbert-mutai/NAS.git /opt/nas/app
sudo -u nas sh -c 'cd /opt/nas/app && python3 -m venv .venv && .venv/bin/pip install ".[cisco]"'
```

Two things that trip people up:

- **`/opt/nas` is mode 0700 and owned by `nas`.** Your admin account cannot `cd` into
  it, so `cd /opt/nas/app && ...` fails before the rest of the line runs. Do the `cd`
  *inside* the `nas` shell, as above, and use `sudo ls` to inspect.
- **Install the extra for your platform**: `.[cisco]` for Catalyst (netmiko),
  `.[juniper]` for PyEZ, `.[devices]` for both. Nexus needs no extra — NX-API is plain
  HTTPS. Installing bare `.` leaves every Cisco/Juniper switch failing with a
  "not installed" DriverDependencyError, which is recorded per switch rather than
  crashing the run.

Also install `python3.12-venv` if `python3 -m venv` complains about `ensurepip`.

### 3. Configuration

`.env` lives beside the code, because pydantic-settings reads it relative to the
process working directory:

```bash
sudo -u nas tee /opt/nas/app/.env >/dev/null <<'EOF'
NAS_ENVIRONMENT=staging
NAS_LOG_FORMAT=json
NAS_LOG_LEVEL=INFO
NAS_DATABASE_URL=postgresql+asyncpg://nas:<hex-password>@<db-private-ip>:5432/nas
NAS_CREDENTIALS_FILE=/opt/nas/credentials.yaml

# Required — staging refuses to start without an allowlist.
# 127.0.0.1/32 covers an SSH tunnel used for development; add ClientManager's host
# as a /32 when that is deployed.
NAS_ALLOWED_IP_RANGES=127.0.0.1/32

# Only true once Nginx is actually in front and setting X-Forwarded-For.
NAS_TRUST_PROXY_HEADERS=false

NAS_SYNC_ENABLED=true
NAS_SYNC_INTERVAL_SECONDS=900
EOF
sudo chmod 600 /opt/nas/app/.env
```

### 4. Credentials

```bash
sudo -u nas tee /opt/nas/credentials.yaml >/dev/null <<'EOF'
credentials:
  westpoint-catalyst:
    username: nas-readonly
    auth_method: password
    password: <switch password>
EOF
sudo chmod 600 /opt/nas/credentials.yaml
sudo -u nas sh -c 'cd /opt/nas/app && .venv/bin/nas credentials check'
```

NAS refuses to start in staging/production if this file is readable by group or other.

**Create a dedicated read-only account on each switch** rather than reusing a personal
login. On IOS:

```
username nas-readonly privilege 1 secret <password>
```

Privilege 1 is sufficient — Phase 1 only runs `show` commands — and it means a
credential leaked from the app server cannot change device configuration. It also keeps
NAS's activity distinguishable from a human's in the switch's own audit log.

### 5. Schema, key, switches

```bash
sudo -u nas sh -c 'cd /opt/nas/app && .venv/bin/nas db upgrade'

sudo -u nas sh -c 'cd /opt/nas/app && .venv/bin/nas apikey create --name clientmanager \
     --scopes switches:read,vlans:read,sync:read,sync:write'

# Catalyst / IOS-XE — SSH, port 22
sudo -u nas sh -c 'cd /opt/nas/app && .venv/bin/nas switch add \
     --name switch-01.westpoint --hostname 192.168.95.237 \
     --vendor cisco_iosxe --credential-ref westpoint-catalyst \
     --site "Westpoint" --environment production'

sudo -u nas sh -c 'cd /opt/nas/app && .venv/bin/nas credentials check'
sudo -u nas sh -c 'cd /opt/nas/app && .venv/bin/nas sync run'
```

Copy the printed key into ClientManager's `.env` as `NAS_API_KEY`. It is shown once.

Drop `sync:write` from the scopes if ClientManager should not offer a "Sync Now" button.

**Nexus needs `--port 443`** and `feature nxapi` on the device. Registering one on port
22 fails with the exact re-registration command rather than silently connecting
elsewhere.

`nas db upgrade` is the first thing that actually authenticates to PostgreSQL, so it is
where a wrong password or a missing `pg_hba` rule surfaces.

### 6. systemd

`/etc/systemd/system/nas.service`:

```ini
[Unit]
Description=Network Automation Service
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
User=nas
Group=nas
WorkingDirectory=/opt/nas/app
ExecStart=/opt/nas/app/.venv/bin/uvicorn nas.main:create_app --factory \
          --host 127.0.0.1 --port 8000 --no-access-log
Restart=on-failure
RestartSec=5s

# Hardening. NAS holds the only credentials that reach production switches.
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
MemoryDenyWriteExecute=true
CapabilityBoundingSet=

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now nas.service
systemctl --no-pager status nas.service
curl -s localhost:8000/health; echo
journalctl -u nas.service -f
```

No `EnvironmentFile` — pydantic-settings reads `.env` from `WorkingDirectory`, so adding
one would create two sources of truth for the same values. `ProtectSystem=strict` makes
the filesystem read-only for the process, which is fine: NAS writes nothing to disk and
logs to the journal via stdout.

`postgresql.service` is deliberately absent from `After=` — the database is on another
host. Add it back only for a single-host deployment.

Binding `127.0.0.1` means nothing on the network can reach uvicorn: only Nginx on the
same host, or an SSH tunnel.

**Check the scheduler is running** a little after start:

```bash
journalctl -u nas.service | grep -E 'scheduler_started|scheduled_sync'
```

### 7. Nginx

```nginx
upstream nas_backend {
    server 127.0.0.1:8000;
    keepalive 16;
}

server {
    listen 443 ssl http2;
    server_name nas.internal;

    ssl_certificate     /etc/ssl/certs/nas.crt;
    ssl_certificate_key /etc/ssl/private/nas.key;
    ssl_protocols       TLSv1.2 TLSv1.3;

    # Defence in depth: NAS enforces its own allowlist, but stopping
    # unauthorised traffic at the edge is cheaper.
    allow 10.20.30.40;
    deny  all;

    location / {
        proxy_pass http://nas_backend;
        proxy_http_version 1.1;

        # Appends the real peer as the LAST entry. NAS reads the last hop,
        # which is why prepended values cannot spoof the allowlist.
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Host $host;

        proxy_connect_timeout 5s;
        proxy_read_timeout    30s;
    }

    location = /health { proxy_pass http://nas_backend; access_log off; }
    location = /live   { proxy_pass http://nas_backend; access_log off; }
    location = /ready  { proxy_pass http://nas_backend; access_log off; }
}
```

Set `NAS_TRUST_PROXY_HEADERS=true` **only** after this is in place. Enabling it without a real
proxy in front would let any caller spoof its source address.

### 8. Verify

```bash
curl https://nas.internal/health
curl -H "X-API-Key: $KEY" https://nas.internal/api/v1/switches

# From a non-allowlisted host — expect 403 IP_NOT_ALLOWED
curl -H "X-API-Key: $KEY" https://nas.internal/api/v1/switches
```

---

## Upgrades

```bash
# The checkout is /opt/nas/app; /opt/nas is mode 0700 owned by `nas`, so an admin
# cannot cd into it. Run as `nas` and cd *inside* that shell — every line here does.
sudo -u nas sh -c 'cd /opt/nas/app && git pull'
sudo -u nas sh -c 'cd /opt/nas/app && .venv/bin/pip install .'
sudo -u nas sh -c 'cd /opt/nas/app && .venv/bin/nas db current'   # what is applied now
sudo -u nas sh -c 'cd /opt/nas/app && .venv/bin/nas db upgrade'
sudo systemctl restart nas.service

# Give it ~5s. Startup takes about three seconds, and a check that races it fails
# misleadingly. -f hides the body, so drop it when diagnosing.
sleep 5 && curl -s -w '\nHTTP %{http_code}\n' http://127.0.0.1:8000/ready
```

Run `nas db upgrade` before the restart. Migrations are additive in Phase 1, so a brief version
skew during the restart is harmless.

`nas` is not on an admin's `PATH` — it lives in the venv at `/opt/nas/app/.venv/bin/nas`. A bare
`nas db upgrade` gives "command not found".

**Deploying is two steps, and the first is on GitHub.** `/opt/nas/app` tracks `master`, so work
merged only into `nas-gilbert` is not on the server no matter how many times you pull. Confirm the
commit you want is actually there before upgrading:

```bash
sudo -u nas sh -c 'cd /opt/nas/app && git fetch -q &&
  git merge-base --is-ancestor <sha> origin/master && echo "IN master" || echo "NOT in master"'
```

### Upgrading to the audit-log release (`0003_audit_log`)

`nas db upgrade` creates the `audit_log` table. That is the whole deployment step — nothing else
is required, and nothing breaks if you forget the rest:

- **The `X-Actor` header is optional.** A ClientManager that does not send it produces audit
  entries with `actor: null`, attributed to the API key alone. No call fails.
- **The existing `clientmanager` key keeps working unchanged.** It does not gain `audit:read`,
  which is deliberate — that scope lets a caller enumerate who triggered what, and ClientManager
  does not need it.

**CLI syncs are attributed too.** `nas sync run` records `SUDO_USER` (falling back to
the login name) as the actor with `source_ip = 'cli'`. So run it the documented way —
`sudo -u nas sh -c '...'` — and the trail names you rather than the service account.

To read the trail through the API, mint a separate operator key:

```bash
sudo -u nas sh -c 'cd /opt/nas/app && .venv/bin/nas apikey create --name audit-operator \
     --scopes audit:read'
```

Or read it directly, which needs no key:

```sql
SELECT occurred_at, action, outcome, actor, api_key_name, target_id
FROM audit_log ORDER BY occurred_at DESC LIMIT 20;
```

**If the trail looks empty after a sync**, check the service log for `audit_write_failed`. An
audit write that fails does **not** fail the sync — by then the switches have been polled, so
raising would report failure for work that succeeded — and the entry is logged inline instead:

```bash
journalctl -u nas.service | grep audit_write_failed
```

## Rollback

```bash
sudo -u nas git checkout <previous-tag>
sudo -u nas .venv/bin/pip install .
sudo -u nas .venv/bin/nas db downgrade -1   # only if the release added a migration
sudo systemctl restart nas.service
```

Every migration has a tested `downgrade()` — the integration suite runs `downgrade base` →
`upgrade head` on each session, so a broken downgrade fails CI rather than production.

## Operational notes

**Monitoring.** Point uptime checks at `/health`. Treat `/ready` returning `503` as de-pool,
not restart. Logs are JSON on stdout, captured by journald and ready for Loki without a parser.

**What to alert on:**

| Signal | Meaning |
|---|---|
| `/ready` → 503 | Database unreachable |
| `ip_rejected` events | Misconfigured consumer, or probing |
| `auth_failed` with `reason=hash_mismatch` | Wrong key, possibly an attack — logged at WARNING |
| `credential_provider_unavailable` at startup | Credential store broken; sync will fail |
| `credentials_file_permissive` | File permissions loosened |

**Key rotation.** Create the new key, deploy it to ClientManager, confirm traffic, then revoke the old
one. Both work simultaneously, so there is no outage window.

```bash
nas apikey create --name clientmanager-2026q4 --scopes switches:read,vlans:read,sync:read
# ... update ClientManager, confirm ...
nas apikey revoke clientmanager
```

**Backups.** NAS's database is a cache of switch state plus the API key table; a full sync
rebuilds the former. The genuinely irreplaceable file is `/etc/nas/credentials.yaml` — back it
up encrypted, separately from the database.
