#!/usr/bin/env bash
set -euo pipefail

repository_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
compose_file="$repository_root/deploy/compose.production.yml"
release_file=${1:?"usage: backup-postgres.sh RELEASE_ENV_FILE [BACKUP_DIRECTORY]"}
backup_directory=${2:-/var/backups/trading-engine}
release_environment_keys=(
  BACKEND_IMAGE
  CADDY_IMAGE
  TIMESCALEDB_IMAGE
  REDIS_IMAGE
  DOMAIN
  ACME_EMAIL
  CADDY_BASIC_AUTH_USER
)

run_sanitized() {
  local key
  local -a sanitized_environment=()
  for key in "${release_environment_keys[@]}"; do
    sanitized_environment+=(-u "$key")
  done
  env "${sanitized_environment[@]}" "$@"
}

[[ -f "$release_file" ]] || { printf 'release environment not found: %s\n' "$release_file" >&2; exit 1; }
install -d -m 700 "$backup_directory"
backup_file="$backup_directory/postgres-$(date -u +%Y%m%dT%H%M%SZ).dump"

umask 077
run_sanitized docker compose -p trading-engine --env-file "$release_file" -f "$compose_file" exec -T postgres sh -ec '
  set -eu
  pgpass_file=$(mktemp)
  chmod 600 "$pgpass_file"
  trap '\''rm -f "$pgpass_file"'\'' EXIT
  printf "*:*:*:trading_engine:%s\\n" "$(cat /run/secrets/postgres_password)" >"$pgpass_file"
  export PGPASSFILE="$pgpass_file"
  pg_dump -U trading_engine -d trading_engine --format=custom
' >"$backup_file"
sha256sum "$backup_file" >"${backup_file}.sha256"
printf 'created backup: %s\n' "$backup_file"
