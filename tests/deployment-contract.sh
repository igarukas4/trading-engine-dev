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
require_file "$repository_root/tests/smoke-release-regression.sh"
require_file "$repository_root/tests/trusted-header-contract.sh"

for secret in postgres_password redis_password app_secret_key caddy_basic_auth_hash; do
  require_file "$repository_root/deploy/secrets/${secret}.example"
done

grep -Fq -- '- "80:80"' "$compose_file" || fail 'Caddy must be the only HTTP publisher'
grep -Fq -- '- "443:443"' "$compose_file" || fail 'Caddy must be the only HTTPS publisher'
for service in backend postgres redis; do
  service_block=$(sed -n "/^  ${service}:$/,/^  [a-z].*:$/{p}" "$compose_file")
  [[ "$service_block" != *'published:'* ]] || fail "${service} must not publish a host port"
done

grep -Fq 'TIMESCALEDB_IMAGE:' "$compose_file" || fail 'TimescaleDB image is required'
grep -Fq 'CADDY_IMAGE:' "$compose_file" || fail 'Caddy image must be release-controlled'
grep -Fq 'REDIS_IMAGE:' "$compose_file" || fail 'Redis image must be release-controlled'
grep -Fq 'max-size: "10m"' "$compose_file" || fail 'Docker log size must be capped'
grep -Fq 'max-file: "5"' "$compose_file" || fail 'Docker log file count must be capped'
grep -Fq 'roll_size 100MiB' "$caddyfile" || fail 'Caddy access logs must rotate by size'
grep -Fq 'roll_keep 10' "$caddyfile" || fail 'Caddy access log file count must be capped'
grep -Fq 'roll_keep_for 720h' "$caddyfile" || fail 'Caddy access log retention must be capped by age'
grep -Fq 'postgres_data:' "$compose_file" || fail 'PostgreSQL must use persistent storage'
grep -Fq 'redis_data:' "$compose_file" || fail 'Redis must use persistent storage'
grep -Fq 'restart: unless-stopped' "$compose_file" || fail 'services must be supervised by Compose'
grep -Fq 'healthcheck:' "$compose_file" || fail 'services must define health checks'
grep -Fq 'internal: true' "$compose_file" || fail 'private service network must be internal'
! grep -Fq -- '--requirepass $$(cat /run/secrets/redis_password)' "$compose_file" || fail 'Redis password must not be expanded into the Redis process arguments'
grep -Fq 'REDISCLI_AUTH=' "$compose_file" || fail 'Redis healthcheck must authenticate through REDISCLI_AUTH'
! grep -Fq -- 'redis-cli --no-auth-warning -a' "$compose_file" || fail 'Redis password must not be passed in redis-cli arguments'
grep -Fq 'exec redis-server /tmp/redis.conf' "$compose_file" || fail 'Redis must read its password from a generated protected config file'

grep -Fq 'basic_auth' "$caddyfile" || fail 'dashboard must require Caddy Basic Auth'
grep -Fq 'header_up X-Authenticated-User {http.auth.user.id}' "$caddyfile" || fail 'Caddy must set the trusted actor header'
! grep -Fq 'header_up -X-Authenticated-User' "$caddyfile" || fail 'Caddy must not delete the trusted actor header'
grep -Fq 'header_up X-Forwarded-For {remote_host}' "$caddyfile" || fail 'Caddy must overwrite forwarded client address'
grep -Fq 'header_up X-Forwarded-Host {host}' "$caddyfile" || fail 'Caddy must overwrite forwarded host'
grep -Fq 'header_up X-Forwarded-Proto {scheme}' "$caddyfile" || fail 'Caddy must overwrite forwarded scheme'
! grep -Fq 'header_up -X-Forwarded-' "$caddyfile" || fail 'Caddy must not delete forwarded headers'
grep -Fq 'health_uri /health/live' "$caddyfile" || fail 'Caddy must actively check backend liveness'

readme="$repository_root/deploy/README.md"
release_example="$repository_root/deploy/release.env.example"
for image_key in BACKEND_IMAGE CADDY_IMAGE TIMESCALEDB_IMAGE REDIS_IMAGE; do
  grep -Eq "^${image_key}=.+@sha256:" "$release_example" || fail "$image_key must be present in the release example"
done
grep -Fq 'openssl rand -base64 48 > deploy/secrets/app_secret_key' "$readme" || fail 'first deploy instructions must generate the application secret'
grep -Fq 'openssl rand -base64 36 > deploy/secrets/postgres_password' "$readme" || fail 'first deploy instructions must generate the PostgreSQL password'
grep -Fq 'openssl rand -base64 36 > deploy/secrets/redis_password' "$readme" || fail 'first deploy instructions must generate the Redis password'
grep -Fqx '$2a$14$replace-with-a-bcrypt-hash' "$repository_root/deploy/secrets/caddy_basic_auth_hash.example" || fail 'Caddy auth example must contain only a bcrypt hash placeholder'

restore_script="$repository_root/scripts/verify-backup-restore.sh"
grep -Fq 'sha256sum --check --status "$checksum_file"' "$restore_script" || fail 'restore verification must check the backup checksum'
grep -Fq 'docker network create --internal "$network_name"' "$restore_script" || fail 'restore verification must create an isolated network'
grep -Fq 'pg_restore -U postgres -d postgres --clean --if-exists --no-owner --exit-on-error' "$restore_script" || fail 'restore verification must ignore dump ownership while preserving restore errors'
! grep -Fq 'trading-engine_private' "$restore_script" || fail 'restore verification must not use the live private network'
grep -Fq 'POSTGRES_PASSWORD_FILE=/run/secrets/postgres_password' "$restore_script" || fail 'restore verification must use the Postgres password file interface'
grep -Fq -- '--mount "type=bind,src=$restore_password_file,dst=/run/secrets/postgres_password,readonly"' "$restore_script" || fail 'restore verification must mount a protected temporary password file'
grep -Fq 'PGPASSFILE=' "$restore_script" || fail 'restore verification must authenticate from a container-local password file'
! grep -Fq 'PGPASSWORD="$POSTGRES_PASSWORD"' "$restore_script" || fail 'restore verification must not read an unset password environment variable'

backup_script="$repository_root/scripts/backup-postgres.sh"
grep -Fq 'umask 077' "$backup_script" || fail 'backup output must be created with a restrictive umask'
grep -Fq 'pg_dump -U trading_engine -d trading_engine --format=custom >"$backup_file"' "$backup_script" || fail 'backup must redirect only after restrictive umask is set'

! grep -Fq 'source "$release_file"' "$repository_root/scripts/smoke-release.sh" || fail 'smoke check must not execute the release environment'
grep -Fq 'read_release_env' "$repository_root/scripts/smoke-release.sh" || fail 'smoke check must parse the release environment'
grep -Fq 'SMOKE_BASIC_AUTH_PASSWORD_FILE' "$repository_root/scripts/smoke-release.sh" || fail 'smoke check must require a protected Basic Auth password file'
! grep -Fq 'SMOKE_BASIC_AUTH_PASSWORD:-' "$repository_root/scripts/smoke-release.sh" || fail 'authenticated smoke check must not be optional'
! grep -Fq -- '--user "${CADDY_BASIC_AUTH_USER}:${SMOKE_BASIC_AUTH_PASSWORD}"' "$repository_root/scripts/smoke-release.sh" || fail 'smoke check must not expose Basic Auth credentials in curl arguments'
grep -Fq 'curl --config -' "$repository_root/scripts/smoke-release.sh" || fail 'smoke check must provide mandatory Basic Auth through curl stdin configuration'

release_script="$repository_root/scripts/release.sh"
grep -Fq 'rollback failed; restoring the recorded active release' "$release_script" || fail 'failed rollback must restore the active release'
grep -Fq '"$smoke_script" "$formerly_current"' "$release_script" || fail 'failed rollback recovery must smoke check the active release'

! grep -Fq -- 'hash-password --plaintext' "$readme" || fail 'README must not pass the Basic Auth password as a process argument'
grep -Fq 'docker run --rm -it caddy:2.10.2-alpine@sha256:' "$readme" || fail 'README must use a digest-pinned Caddy image for password generation'

"$repository_root/tests/trusted-header-contract.sh" || fail 'trusted-header contract check failed'

printf 'deployment contract passed\n'
