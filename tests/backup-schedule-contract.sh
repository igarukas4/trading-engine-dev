#!/usr/bin/env bash
set -euo pipefail

repository_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
backup_script="$repository_root/scripts/backup-and-verify.sh"
service_file="$repository_root/deploy/systemd/trading-engine-backup.service"
timer_file="$repository_root/deploy/systemd/trading-engine-backup.timer"

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

[[ -x "$backup_script" ]] || fail 'scheduled backup script must be executable'
[[ -f "$service_file" ]] || fail 'scheduled backup service must exist'
[[ -f "$timer_file" ]] || fail 'scheduled backup timer must exist'
grep -Fq 'flock -n 9' "$backup_script" || fail 'scheduled backup must prevent concurrent runs'
grep -Fq 'verify-backup-restore.sh' "$backup_script" || fail 'scheduled backup must verify every restore'
grep -Fq -- '-mtime +"$retention_days" -print -delete' "$backup_script" || fail 'scheduled backup must prune only after verification'
grep -Fq 'RETENTION_DAYS must be a positive integer' "$backup_script" || fail 'scheduled backup must validate retention'
grep -Fq 'UMask=0077' "$service_file" || fail 'scheduled backup service must use a restrictive umask'
grep -Fq 'NoNewPrivileges=true' "$service_file" || fail 'scheduled backup service must not gain privileges'
grep -Fq 'ProtectHome=read-only' "$service_file" || fail 'scheduled backup service must allow read-only access to Docker Compose plugins'
grep -Fq 'ReadWritePaths=/var/backups/trading-engine' "$service_file" || fail 'scheduled backup service must limit write paths'
grep -Fq 'OnCalendar=*-*-* 02:30:00 UTC' "$timer_file" || fail 'scheduled backup must run at a documented UTC time'
grep -Fq 'Persistent=true' "$timer_file" || fail 'scheduled backup must catch up after downtime'

printf 'backup schedule contract passed\n'
