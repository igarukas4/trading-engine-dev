#!/usr/bin/env bash
set -euo pipefail

repository_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
compose_file="$repository_root/deploy/compose.production.yml"
release_file=${1:?"usage: verify-backup-restore.sh RELEASE_ENV_FILE BACKUP_FILE"}
backup_file=${2:?"usage: verify-backup-restore.sh RELEASE_ENV_FILE BACKUP_FILE"}

[[ -f "$release_file" ]] || { printf 'release environment not found: %s\n' "$release_file" >&2; exit 1; }
[[ -s "$backup_file" ]] || { printf 'backup not found or empty: %s\n' "$backup_file" >&2; exit 1; }
checksum_file="${backup_file}.sha256"
[[ -s "$checksum_file" ]] || { printf 'backup checksum not found or empty: %s\n' "$checksum_file" >&2; exit 1; }
sha256sum --check --status "$checksum_file" || { printf 'backup checksum verification failed: %s\n' "$backup_file" >&2; exit 1; }
command -v openssl >/dev/null || { printf 'openssl is required to generate an ephemeral restore password\n' >&2; exit 1; }
timescaledb_image=$(sed -n 's/^TIMESCALEDB_IMAGE=//p' "$release_file")
[[ "$timescaledb_image" =~ ^[^[:space:]]+@sha256:[a-f0-9]{64}$ ]] || {
  printf 'TIMESCALEDB_IMAGE must be pinned to a sha256 digest\n' >&2
  exit 1
}

container_name="trading-engine-restore-$RANDOM"
network_name="trading-engine-restore-network-$RANDOM"
umask 077
restore_password_file=$(mktemp "$(dirname "$backup_file")/.restore-password.XXXXXX")
openssl rand -base64 36 >"$restore_password_file"
timescaledb_restore_started=false
cleanup() {
  if [[ "$timescaledb_restore_started" == true ]]; then
    docker exec "$container_name" psql -X -v ON_ERROR_STOP=1 -U postgres -d postgres \
      -c 'SELECT timescaledb_post_restore();' >/dev/null 2>&1 || true
  fi
  docker rm -f "$container_name" >/dev/null 2>&1 || true
  docker network rm "$network_name" >/dev/null 2>&1 || true
  rm -f "$restore_password_file"
}
trap cleanup EXIT

docker network create --internal "$network_name" >/dev/null
docker run --detach --name "$container_name" --network "$network_name" \
  --mount "type=bind,src=$restore_password_file,dst=/run/secrets/postgres_password,readonly" \
  -e POSTGRES_PASSWORD_FILE=/run/secrets/postgres_password \
  "$timescaledb_image" >/dev/null
for _ in {1..30}; do
  if docker exec "$container_name" pg_isready -U postgres >/dev/null 2>&1; then
    # The image briefly accepts connections during initialization, then
    # restarts PostgreSQL before it is ready for a restore.
    sleep 2
    docker exec "$container_name" pg_isready -U postgres >/dev/null 2>&1 && break
  fi
  sleep 1
done
docker exec "$container_name" pg_isready -U postgres >/dev/null
docker exec "$container_name" psql -X -v ON_ERROR_STOP=1 -U postgres -d postgres \
  -c 'CREATE EXTENSION IF NOT EXISTS timescaledb;' >/dev/null
docker exec "$container_name" psql -X -v ON_ERROR_STOP=1 -U postgres -d postgres \
  -c 'SELECT timescaledb_pre_restore();' >/dev/null
timescaledb_restore_started=true
docker cp "$backup_file" "$container_name:/tmp/backup.dump"
docker exec "$container_name" sh -ec '
  set -eu
  pgpass_file=$(mktemp)
  chmod 600 "$pgpass_file"
  trap '\''rm -f "$pgpass_file"'\'' EXIT
  printf "*:*:*:postgres:%s\\n" "$(cat /run/secrets/postgres_password)" >"$pgpass_file"
  export PGPASSFILE="$pgpass_file"
  # The target is a new ephemeral database. --clean would attempt to drop the
  # preloaded TimescaleDB extension and terminate the restore session.
  pg_restore -U postgres -d postgres --no-owner --exit-on-error /tmp/backup.dump
'
docker exec "$container_name" psql -X -v ON_ERROR_STOP=1 -U postgres -d postgres \
  -c 'SELECT timescaledb_post_restore();' >/dev/null
timescaledb_restore_started=false
docker exec "$container_name" psql -U postgres -d postgres -Atqc "select 1" | grep -qx "1"
printf 'backup restore verification passed: %s\n' "$backup_file"
