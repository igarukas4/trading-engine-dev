#!/usr/bin/env bash
set -euo pipefail

repository_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
compose_file="$repository_root/deploy/compose.production.yml"
release_file=${1:?"usage: verify-backup-restore.sh RELEASE_ENV_FILE BACKUP_FILE"}
backup_file=${2:?"usage: verify-backup-restore.sh RELEASE_ENV_FILE BACKUP_FILE"}

[[ -f "$release_file" ]] || { printf 'release environment not found: %s\n' "$release_file" >&2; exit 1; }
[[ -s "$backup_file" ]] || { printf 'backup not found or empty: %s\n' "$backup_file" >&2; exit 1; }
command -v openssl >/dev/null || { printf 'openssl is required to generate an ephemeral restore password\n' >&2; exit 1; }

container_name="trading-engine-restore-$RANDOM"
password=$(openssl rand -base64 36)
network_name=trading-engine_private
cleanup() { docker rm -f "$container_name" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker network inspect "$network_name" >/dev/null
docker run --detach --name "$container_name" --network "$network_name" -e POSTGRES_PASSWORD="$password" timescale/timescaledb:2.17.2-pg16 >/dev/null
for _ in {1..30}; do
  docker exec "$container_name" pg_isready -U postgres >/dev/null 2>&1 && break
  sleep 1
done
docker exec "$container_name" pg_isready -U postgres >/dev/null
docker cp "$backup_file" "$container_name:/tmp/backup.dump"
docker exec "$container_name" sh -ec 'PGPASSWORD="$POSTGRES_PASSWORD" pg_restore -U postgres -d postgres --clean --if-exists --exit-on-error /tmp/backup.dump'
docker exec "$container_name" psql -U postgres -d postgres -Atqc "select 1" | grep -qx '1'
printf 'backup restore verification passed: %s\n' "$backup_file"
