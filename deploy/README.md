# Production deployment

This directory has two deployment modes. `compose.production.yml` is the original dedicated-Caddy contract: it intentionally exposes only its Caddy on `80` and `443`; `backend`, PostgreSQL/TimescaleDB, and Redis have no host port. Docker's `private` network is internal, and Caddy is assigned `172.30.0.2` so the backend can trust forwarded and authenticated-principal headers only from that proxy.

`compose.shared-host-caddy.yml` is for a VPS that already has a host Caddy owning public `80`/`443`. It contains no Caddy service and publishes only `backend` as `127.0.0.1:18000`; PostgreSQL and Redis remain unexposed on the internal Docker network. The host Caddy must be configured from the tracked `caddy/Caddyfile.shared-host.example` template. It terminates TLS, returns unauthenticated `ok` on `/healthz`, enforces Basic Auth for every other route, actively checks `/health/live`, and overwrites `X-Authenticated-User` plus all `X-Forwarded-*` headers before proxying. The backend trusts only Docker's fixed private-network gateway (`172.30.0.1`), which is the source of the host-loopback forwarding path.

For shared-host mode, copy `release.shared-host-caddy.env.example` to the protected release path. Create only `app_secret_key`, `postgres_password`, and `redis_password` under `deploy/secrets`; the Basic Auth username and bcrypt hash belong solely to a root-readable host-Caddy EnvironmentFile outside this repository. Do not start `compose.production.yml`, run a second public Caddy, or reload host Caddy until the DNS record, host-Caddy diff, environment file, and validation/reload plan have been explicitly approved.

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

Do not publish database, Redis, or backend ports with ad-hoc Docker commands. Keep SSH restricted to operator IPs at the provider firewall where possible. The deployment supplies the external backend image with its configured trusted actor header, trusted proxy address, origin, and secret-file paths; this repository verifies the ingress and network boundary without making claims about backend implementation internals.

## Secrets and first release

Create the untracked files below from their `.example` counterparts, set owner to the deployment user, and use mode `0600`. Never place values in a release file, Git, URLs, logs, or command history.

```bash
cd /srv/trading-engine
for name in app_secret_key caddy_basic_auth_hash postgres_password redis_password; do
  install -m 600 /dev/null "deploy/secrets/$name"
done
openssl rand -base64 48 > deploy/secrets/app_secret_key
openssl rand -base64 36 > deploy/secrets/postgres_password
openssl rand -base64 36 > deploy/secrets/redis_password
docker run --rm -it caddy:2.10.2-alpine@sha256:4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d caddy hash-password > deploy/secrets/caddy_basic_auth_hash
```

Run these commands only on the VPS: each command overwrites the newly created empty secret file with a fresh value. Store the Basic Auth plaintext in an approved password manager; only its bcrypt hash belongs in `deploy/secrets/caddy_basic_auth_hash`.

Copy `release.env.example` to a protected location and replace `BACKEND_IMAGE` with an immutable registry digest. Keep the Caddy, TimescaleDB, and Redis image values pinned as shown. Deploy only after the image implements `GET /health/live` on port `8000` without authentication.

```bash
scripts/release.sh deploy /etc/trading-engine/release.env
```

The deploy script validates untracked `0600` secrets, validates the rendered Compose file, pulls images, waits for health checks, and runs an HTTPS smoke check. The authenticated proxy-to-backend check is mandatory: place the Basic Auth plaintext in a separate `0600` file outside the repository and provide its path without putting the password in an environment variable or command argument:

```bash
SMOKE_BASIC_AUTH_PASSWORD_FILE=/etc/trading-engine/smoke-basic-auth-password \
  scripts/release.sh deploy /etc/trading-engine/release.env
```

The smoke script supplies the password to curl through protected standard input rather than a process argument. It also sends forged `X-Authenticated-User` and `X-Forwarded-*` headers at the public boundary: the unauthenticated request must remain `401`, and the authenticated request must still reach the backend. Before proxying, Caddy’s `reverse_proxy` `header_up` assignments overwrite those forged values with the authenticated Basic Auth identity and request-derived host, client address, and scheme. The deterministic `tests/trusted-header-contract.sh` check proves those assignments are in the adapted proxy’s request-header `set` map (or in the proxy block when the adapter is unavailable); it does not infer backend internals or require an identity-echo endpoint.

The public `GET /healthz` endpoint intentionally returns `ok` without Basic Auth so external liveness checks can observe it. Dashboard and backend routes remain behind Basic Auth, including the backend's `/health/live` route when accessed through Caddy.

Caddy manages certificates and writes JSON access logs to its persistent volume with 10 rotated files retained for up to 30 days at 100 MiB each. Docker retains each service's stdout/stderr logs with the `json-file` driver, capped at five 10 MiB files. These limits rotate logs without removing the persistent Caddy, PostgreSQL, or Redis data volumes.

## Rollback, backup, and restore verification

Each successful deploy snapshots its release environment under the ignored `deploy/releases/` directory. A successful rollback atomically swaps the active and prior release metadata, so the command can also return to the release that was active before the rollback. If rollback startup or its smoke check fails, the script restores and smoke-checks the recorded active release without changing metadata. Roll back to the prior successful snapshot with:

```bash
SMOKE_BASIC_AUTH_PASSWORD_FILE=/etc/trading-engine/smoke-basic-auth-password \
  scripts/release.sh rollback
```

Take a custom-format PostgreSQL backup and verify it by restoring into an ephemeral isolated TimescaleDB container. The verification does not modify production data; it generates the ephemeral database password in a temporary `0600` file mounted through PostgreSQL's `_FILE` interface, so the password is not placed in Docker arguments or environment.

```bash
backup=$(scripts/backup-postgres.sh /etc/trading-engine/release.env /var/backups/trading-engine | sed -n 's/^created backup: //p')
scripts/verify-backup-restore.sh /etc/trading-engine/release.env "$backup"
```

Run backup plus restore verification on a scheduled host timer, retain encrypted off-host copies according to the operator's recovery policy, and periodically rehearse `scripts/release.sh rollback` with a valid smoke check.
