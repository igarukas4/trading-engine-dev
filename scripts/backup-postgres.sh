#!/usr/bin/env bash
set -euo pipefail

repository_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
compose_file="$repository_root/deploy/compose.production.yml"
release_file=${1:?"usage: backup-postgres.sh RELEASE_ENV_FILE [BACKUP_DIRECTORY]"}
backup_directory=${2:-/var/backups/trading-engine}

[[ -f "$release_file" ]] || { printf 'release environment not found: %s\n' "$release_file" >&2; exit 1; }
install -d -m 700 "$backup_directory"
backup_file="$backup_directory/postgres-$(date -u +%Y%m%dT%H%M%SZ).dump"

umask 077
docker compose --env-file "$release_file" -f "$compose_file" exec -T postgres pg_dump -U trading_engine -d trading_engine --format=custom >"$backup_file"
sha256sum "$backup_file" >"${backup_file}.sha256"
printf 'created backup: %s\n' "$backup_file"
