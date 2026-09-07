# Production deployment

This directory is the D1 deployment contract for one Lighthouse VPS. It intentionally exposes only Caddy on `80` and `443`; `backend`, PostgreSQL/TimescaleDB, and Redis have no host port. Docker's `private` network is internal, and Caddy is assigned `172.30.0.2` so the backend can trust forwarded and authenticated-principal headers only from that proxy.

## VPS preparation

Install Docker Engine plus the Compose plugin, point the dashboard DNS record at the VPS, and allow only SSH, HTTP, and HTTPS at the cloud firewall and host firewall. For example, after ensuring an existing SSH session works:

```bash
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw enable
```

Do not publish database, Redis, or backend ports with ad-hoc Docker commands. Keep SSH restricted to operator IPs at the provider firewall where possible. The backend image must reject `X-Authenticated-User` and forwarded headers unless the peer is `172.30.0.2`, enforce `PUBLIC_ORIGIN` for state-changing requests and WebSocket origins, and read the `*_FILE` secret paths supplied by Compose.

## Secrets and first release

Create the untracked files below from their `.example` counterparts, set owner to the deployment user, and use mode `0600`. Never place values in a release file, Git, URLs, logs, or command history.

```bash
cd /srv/trading-engine
for name in app_secret_key caddy_basic_auth_hash postgres_password redis_password; do
  install -m 600 /dev/null "deploy/secrets/$name"
done
docker run --rm caddy:2.10.2-alpine caddy hash-password --plaintext 'choose-a-long-password' > deploy/secrets/caddy_basic_auth_hash
```

Copy `release.env.example` to a protected location and replace `BACKEND_IMAGE` with an immutable registry digest. Deploy only after the image implements `GET /health/live` on port `8000` without authentication.

```bash
scripts/release.sh deploy /etc/trading-engine/release.env
```

The deploy script validates untracked `0600` secrets, validates the rendered Compose file, pulls images, waits for health checks, and runs an HTTPS smoke check. Set `SMOKE_BASIC_AUTH_PASSWORD` only in the shell running the script to additionally verify the authenticated backend path. Caddy manages certificates and writes JSON access logs to its persistent volume; Docker retains service logs via its configured logging driver.

## Rollback, backup, and restore verification

Each successful deploy snapshots its release environment under the ignored `deploy/releases/` directory. Roll back to the prior successful snapshot with:

```bash
scripts/release.sh rollback
```

Take a custom-format PostgreSQL backup and verify it by restoring into an ephemeral isolated TimescaleDB container. The verification does not modify production data.

```bash
backup=$(scripts/backup-postgres.sh /etc/trading-engine/release.env /var/backups/trading-engine | sed -n 's/^created backup: //p')
scripts/verify-backup-restore.sh /etc/trading-engine/release.env "$backup"
```

Run backup plus restore verification on a scheduled host timer, retain encrypted off-host copies according to the operator's recovery policy, and periodically rehearse `scripts/release.sh rollback` with a valid smoke check.
