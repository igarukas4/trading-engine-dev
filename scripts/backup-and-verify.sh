#!/usr/bin/env bash
set -euo pipefail

repository_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
release_file=${1:?"usage: backup-and-verify.sh RELEASE_ENV_FILE BACKUP_DIRECTORY RETENTION_DAYS"}
backup_directory=${2:?"usage: backup-and-verify.sh RELEASE_ENV_FILE BACKUP_DIRECTORY RETENTION_DAYS"}
retention_days=${3:?"usage: backup-and-verify.sh RELEASE_ENV_FILE BACKUP_DIRECTORY RETENTION_DAYS"}

[[ "$retention_days" =~ ^[1-9][0-9]*$ ]] || {
  printf 'RETENTION_DAYS must be a positive integer\n' >&2
  exit 2
}

install -d -m 700 "$backup_directory"
exec 9>"$backup_directory/.backup-and-verify.lock"
flock -n 9 || {
  printf 'backup and restore verification is already running\n' >&2
  exit 0
}

backup_output=$("$repository_root/scripts/backup-postgres.sh" "$release_file" "$backup_directory")
printf '%s\n' "$backup_output"
backup_file=$(printf '%s\n' "$backup_output" | sed -n 's/^created backup: //p' | tail -n 1)
[[ -n "$backup_file" ]] || {
  printf 'backup script did not report an artifact path\n' >&2
  exit 1
}

"$repository_root/scripts/verify-backup-restore.sh" "$release_file" "$backup_file"
find "$backup_directory" -maxdepth 1 -type f \( -name 'postgres-*.dump' -o -name 'postgres-*.dump.sha256' \) \
  -mtime +"$retention_days" -print -delete
