# Deployment

Three modes: local development, containerised (parity check), and staging/production behind
Nginx with systemd.

The staging/production path deliberately mirrors how the CRM already deploys — venv, systemd,
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
nas apikey create --name crm --scopes switches:read,vlans:read,sync:read
nas serve --reload
```

`nas serve` binds `127.0.0.1` by default. Exposing the service is a deliberate act
(`--host 0.0.0.0`).

**Why port 5434.** 5432 is the CRM's PostgreSQL. 5433 was already taken by an unrelated
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

Give NAS its own database and role. The CRM's role must have no access to it.

```sql
CREATE ROLE nas WITH LOGIN PASSWORD '<strong-password>';
CREATE DATABASE nas OWNER nas;
REVOKE ALL ON DATABASE nas FROM PUBLIC;
```

### 2. Application user and checkout

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin nas
sudo -u nas git clone <repo-url> /opt/nas
cd /opt/nas
sudo -u nas python3 -m venv .venv
sudo -u nas .venv/bin/pip install .
```

### 3. Configuration

```bash
sudo -u nas cp .env.example /opt/nas/.env
sudo chmod 600 /opt/nas/.env
```

Staging values:

```ini
NAS_ENVIRONMENT=staging
NAS_LOG_FORMAT=json
NAS_LOG_LEVEL=INFO
NAS_DATABASE_URL=postgresql+asyncpg://nas:<password>@localhost:5432/nas

# The CRM host, as a /32. Required — the service refuses to start without it.
NAS_ALLOWED_IP_RANGES=10.20.30.40/32

# Only true once Nginx is actually in front and setting X-Forwarded-For.
NAS_TRUST_PROXY_HEADERS=true

NAS_DOCS_ENABLED=false
NAS_CREDENTIALS_FILE=/etc/nas/credentials.yaml
```

### 4. Credentials

```bash
sudo mkdir -p /etc/nas
sudo cp credentials.example.yaml /etc/nas/credentials.yaml
sudo chown nas:nas /etc/nas/credentials.yaml
sudo chmod 600 /etc/nas/credentials.yaml
sudo -u nas /opt/nas/.venv/bin/nas credentials check
```

NAS refuses to start in staging/production if this file is readable by group or other.

### 5. Schema, key, switches

```bash
cd /opt/nas
sudo -u nas .venv/bin/nas db upgrade
sudo -u nas .venv/bin/nas apikey create --name crm \
     --scopes switches:read,vlans:read,sync:read --expires-days 365
sudo -u nas .venv/bin/nas switch add --name adc-core-sw1 --hostname 10.20.0.11 \
     --vendor juniper --credential-ref juniper-core --site "ADC NBO" --environment production
sudo -u nas .venv/bin/nas credentials check
```

Copy the printed key into the CRM's `.env` as `NAS_API_KEY`. It is shown once.

### 6. systemd

`/etc/systemd/system/nas.service`:

```ini
[Unit]
Description=Network Automation Service
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=exec
User=nas
Group=nas
WorkingDirectory=/opt/nas
EnvironmentFile=/opt/nas/.env
ExecStart=/opt/nas/.venv/bin/uvicorn nas.main:create_app --factory \
          --host 127.0.0.1 --port 8000 --no-access-log
Restart=on-failure
RestartSec=5s

# Hardening. NAS holds the credentials that reach production switches.
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
ReadOnlyPaths=/etc/nas
CapabilityBoundingSet=

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now nas.service
systemctl status nas.service
journalctl -u nas.service -f
```

Binding `127.0.0.1` means only Nginx on the same host can reach uvicorn directly.

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
cd /opt/nas
sudo -u nas git pull
sudo -u nas .venv/bin/pip install .
sudo -u nas .venv/bin/nas db upgrade
sudo systemctl restart nas.service
curl -sf https://nas.internal/ready
```

Run `nas db upgrade` before the restart. Migrations are additive in Phase 1, so a brief version
skew during the restart is harmless.

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

**Key rotation.** Create the new key, deploy it to the CRM, confirm traffic, then revoke the old
one. Both work simultaneously, so there is no outage window.

```bash
nas apikey create --name crm-2026q4 --scopes switches:read,vlans:read,sync:read
# ... update the CRM, confirm ...
nas apikey revoke crm
```

**Backups.** NAS's database is a cache of switch state plus the API key table; a full sync
rebuilds the former. The genuinely irreplaceable file is `/etc/nas/credentials.yaml` — back it
up encrypted, separately from the database.
