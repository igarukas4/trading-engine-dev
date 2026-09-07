#!/usr/bin/env bash
set -euo pipefail

repository_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
compose_file="$repository_root/deploy/compose.production.yml"
caddyfile="$repository_root/deploy/caddy/Caddyfile"

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

require_file() {
  [[ -f "$1" ]] || fail "missing ${1#"$repository_root/"}"
}

require_file "$compose_file"
require_file "$caddyfile"
require_file "$repository_root/scripts/release.sh"
require_file "$repository_root/scripts/smoke-release.sh"
require_file "$repository_root/scripts/backup-postgres.sh"
require_file "$repository_root/scripts/verify-backup-restore.sh"

for secret in postgres_password redis_password app_secret_key caddy_basic_auth_hash; do
  require_file "$repository_root/deploy/secrets/${secret}.example"
done

grep -Fq -- '- "80:80"' "$compose_file" || fail 'Caddy must be the only HTTP publisher'
grep -Fq -- '- "443:443"' "$compose_file" || fail 'Caddy must be the only HTTPS publisher'
for service in backend postgres redis; do
  service_block=$(sed -n "/^  ${service}:$/,/^  [a-z].*:$/{p}" "$compose_file")
  [[ "$service_block" != *'published:'* ]] || fail "${service} must not publish a host port"
done

grep -Fq 'timescale/timescaledb:' "$compose_file" || fail 'TimescaleDB image is required'
grep -Fq 'postgres_data:' "$compose_file" || fail 'PostgreSQL must use persistent storage'
grep -Fq 'redis_data:' "$compose_file" || fail 'Redis must use persistent storage'
grep -Fq 'restart: unless-stopped' "$compose_file" || fail 'services must be supervised by Compose'
grep -Fq 'healthcheck:' "$compose_file" || fail 'services must define health checks'
grep -Fq 'internal: true' "$compose_file" || fail 'private service network must be internal'

grep -Fq 'basic_auth' "$caddyfile" || fail 'dashboard must require Caddy Basic Auth'
grep -Fq 'header_up -X-Authenticated-User {http.auth.user.id}' "$caddyfile" || fail 'Caddy must overwrite trusted actor header'
grep -Fq 'header_up -X-Forwarded-For {remote_host}' "$caddyfile" || fail 'Caddy must overwrite forwarded client address'
grep -Fq 'header_up -X-Forwarded-Proto {scheme}' "$caddyfile" || fail 'Caddy must overwrite forwarded scheme'
grep -Fq 'health_uri /health/live' "$caddyfile" || fail 'Caddy must actively check backend liveness'

printf 'deployment contract passed\n'
