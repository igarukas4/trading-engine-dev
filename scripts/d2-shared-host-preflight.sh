#!/usr/bin/env bash
#
# Human-operated D2 evidence wizard for the shared-host Caddy deployment.
# It never prints secret values and never performs broker or MT5 actions.

set -euo pipefail

if [[ -t 1 ]] && command -v tput >/dev/null 2>&1 && [[ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]]; then
  BOLD=$(tput bold); DIM=$(tput dim); RESET=$(tput sgr0)
  BLUE=$(tput setaf 4); GREEN=$(tput setaf 2); YELLOW=$(tput setaf 3)
else
  BOLD=""; DIM=""; RESET=""; BLUE=""; GREEN=""; YELLOW=""
fi

TOTAL_STAGES=7
_STAGE_INDEX=0
repository_root=${REPOSITORY_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
report_file=""

clear_screen() {
  [[ -t 1 ]] || return 0
  if command -v tput >/dev/null 2>&1; then tput clear; else printf '\033[2J\033[3J\033[H'; fi
}

pause() {
  printf '  %s%s%s ' "$DIM" "${1:-Press Enter to continue}" "$RESET"
  read -r _ || true
}

confirm() {
  local reply=""
  printf '  %s? %s [y/N] ' "$YELLOW" "$1"
  read -r reply || true
  [[ "$reply" =~ ^[Yy] ]]
}

stage() {
  clear_screen
  _STAGE_INDEX=$((_STAGE_INDEX + 1))
  printf '\n%s%s  Stage %s/%s: %s%s\n\n' "$BOLD" "$BLUE" "$_STAGE_INDEX" "$TOTAL_STAGES" "$1" "$RESET"
}

ask() {
  local prompt=$1 value=""
  printf '  %s ' "$prompt" >&2
  read -r value || true
  printf '%s' "$value"
}

report() {
  printf '%s\n' "$1" | tee -a "$report_file"
}

result() {
  local status=$1 label=$2
  report "- ${label}: ${status}"
}

release_value() {
  local key=$1
  sed -n "s/^${key}=//p" "$release_file" | tail -n 1
}

check_command() {
  local label=$1
  shift
  if "$@" >/dev/null 2>&1; then
    result PASS "$label"
  else
    result FAIL "$label"
  fi
}

banner() {
  clear_screen
  printf '\n%s%s  Trading Engine V0 D2 shared-host preflight%s\n\n' "$BOLD" "$BLUE" "$RESET"
  printf '  This wizard records safe operational evidence. It does not deploy, restart services,\n'
  printf '  modify Caddy, create a BrokerAccount, connect MT5, or submit an order.\n'
  printf '  Do not type passwords, tokens, hashes, or MT5 credentials into this wizard.\n\n'
  pause "Press Enter to begin."
}

banner

stage "Choose the protected release file"
printf '  Enter the absolute path to the existing shared-host release file. This path is safe to record; its contents are not printed.\n'
release_file=$(ask "Release file path:")
[[ -n "$release_file" && -f "$release_file" ]] || { printf 'Release file not found. Nothing was changed.\n' >&2; exit 2; }
default_report="/tmp/d2-preflight-$(date -u +%Y%m%dT%H%M%SZ).md"
report_file=$(ask "Evidence report path [${default_report}]:")
report_file=${report_file:-$default_report}
umask 077
: >"$report_file"
chmod 600 "$report_file"
report "# D2 shared-host preflight evidence"
report ""
report "- Collected at UTC: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
report "- Repository path: ${repository_root}"
report "- Release file path: ${release_file}"
report "- Scope: infrastructure only. No BrokerAccount, connector, MT5, or order action occurred."

stage "Confirm shared-host configuration"
deployment_mode=$(release_value DEPLOYMENT_MODE)
domain=$(release_value DOMAIN)
caddy_user=$(release_value CADDY_BASIC_AUTH_USER)
backend_image=$(release_value BACKEND_IMAGE)
frontend_image=$(release_value FRONTEND_IMAGE)
postgres_image=$(release_value TIMESCALEDB_IMAGE)
redis_image=$(release_value REDIS_IMAGE)
report ""
report "## Release identity"
report "- Deployment mode: ${deployment_mode:-missing}"
report "- Domain: ${domain:-missing}"
report "- Backend image: ${backend_image:-missing}"
report "- Frontend image: ${frontend_image:-missing}"
report "- TimescaleDB image: ${postgres_image:-missing}"
report "- Redis image: ${redis_image:-missing}"
[[ "$deployment_mode" == shared-host-caddy ]] && result PASS "Deployment mode is shared-host-caddy" || result FAIL "Deployment mode is shared-host-caddy"
[[ -n "$domain" && -n "$caddy_user" ]] && result PASS "Required public release fields are present" || result FAIL "Required public release fields are present"
for image_key in BACKEND_IMAGE FRONTEND_IMAGE TIMESCALEDB_IMAGE REDIS_IMAGE; do
  image_value=$(release_value "$image_key")
  [[ "$image_value" =~ ^[^[:space:]]+@sha256:[a-f0-9]{64}$ ]] && result PASS "${image_key} has immutable digest" || result FAIL "${image_key} has immutable digest"
done
check_command "Rendered shared-host Compose configuration validates" docker compose -p trading-engine --env-file "$release_file" -f "$repository_root/deploy/compose.shared-host-caddy.yml" config --quiet

stage "Check host services and network boundary"
report ""
report "## Host and network"
report "- Docker version: $(docker --version 2>/dev/null || printf unavailable)"
report "- Docker Compose version: $(docker compose version 2>/dev/null || printf unavailable)"
check_command "Docker is available" docker info
check_command "Host Caddy service is active" systemctl is-active --quiet caddy
if command -v ss >/dev/null 2>&1; then
  report "- Public listener summary (80/443): $(ss -ltn '( sport = :80 or sport = :443 )' 2>/dev/null | tail -n +2 | tr '\n' ';' || true)"
  report "- Loopback listener summary (13000/18000): $(ss -ltn '( sport = :13000 or sport = :18000 )' 2>/dev/null | tail -n +2 | tr '\n' ';' || true)"
else
  result NOT_RUN "Listener inspection, ss is unavailable"
fi
check_command "Frontend is published only to loopback" bash -c 'test "$(docker compose -p trading-engine --env-file "$1" -f "$2" port frontend 3000 2>/dev/null)" = 127.0.0.1:13000' _ "$release_file" "$repository_root/deploy/compose.shared-host-caddy.yml"
check_command "Backend is published only to loopback" bash -c 'test "$(docker compose -p trading-engine --env-file "$1" -f "$2" port backend 8000 2>/dev/null)" = 127.0.0.1:18000' _ "$release_file" "$repository_root/deploy/compose.shared-host-caddy.yml"

stage "Check service health and capacity"
report ""
report "## Runtime health"
compose_status=$(docker compose -p trading-engine --env-file "$release_file" -f "$repository_root/deploy/compose.shared-host-caddy.yml" ps --format '{{.Name}} {{.State}} {{.Health}}' 2>/dev/null || true)
[[ -n "$compose_status" ]] && report "- Compose status: $(printf '%s' "$compose_status" | tr '\n' ';')" || result FAIL "Compose status is available"
if [[ -n "$domain" ]]; then
  check_command "Local host-Caddy health endpoint returns ok" bash -c 'curl --fail --silent --show-error --resolve "$1:443:127.0.0.1" "https://$1/healthz" | grep -qx ok' _ "$domain"
else
  result NOT_RUN "Health endpoint check, DOMAIN is missing"
fi
report "- Root filesystem: $(df -h / | tail -n 1)"
if command -v free >/dev/null 2>&1; then report "- Memory summary: $(free -h | awk '/^Mem:/ {print $2 " total, " $3 " used, " $7 " available"}')"; fi

stage "Check secret permissions without reading secrets"
report ""
report "## Secret-file permissions"
for secret_name in app_secret_key postgres_password redis_password; do
  secret_file="$repository_root/deploy/secrets/$secret_name"
  if [[ -s "$secret_file" && $(stat -c '%a' "$secret_file") == 600 ]]; then
    report "- ${secret_name}: PASS, non-empty, owner $(stat -c '%U' "$secret_file"), mode 600"
  else
    report "- ${secret_name}: FAIL, file missing, empty, or mode is not 600"
  fi
done
report "- Host-Caddy Basic Auth EnvironmentFile: MANUAL CHECK. Confirm it is outside this repository, root-readable only, mode 600. Do not disclose its path or contents."

stage "Record smoke, backup, and restore evidence"
report ""
report "## Smoke, backup, and restore"
printf '  The authenticated smoke check needs the local Basic Auth password file. It is not safe to enter that password here.\n'
printf '  In another terminal, run the command from deploy/README.md using the protected password-file path. Return here only after it succeeds or fails.\n'
pause "Press Enter after recording the authenticated smoke result."
smoke_status=$(ask "Authenticated smoke result [PASS/FAIL/NOT_RUN]:")
report "- Authenticated smoke: ${smoke_status:-NOT_RECORDED}"
printf '  Backup and restore create a backup file and an ephemeral database container. The next action runs only if you approve it.\n'
if confirm "Run the repository backup and isolated restore verification now"; then
  backup_directory=$(ask "Backup directory [/var/backups/trading-engine]:")
  backup_directory=${backup_directory:-/var/backups/trading-engine}
  if backup_output=$("$repository_root/scripts/backup-postgres.sh" "$release_file" "$backup_directory" 2>&1); then
    backup_file=$(printf '%s\n' "$backup_output" | sed -n 's/^created backup: //p' | tail -n 1)
    report "- Backup creation: PASS"
    report "- Backup artifact: ${backup_file:-not reported}"
    if [[ -n "$backup_file" ]] && "$repository_root/scripts/verify-backup-restore.sh" "$release_file" "$backup_file" >/dev/null 2>&1; then
      report "- Isolated restore verification: PASS"
    else
      report "- Isolated restore verification: FAIL"
    fi
  else
    report "- Backup creation: FAIL"
  fi
else
  report "- Backup creation and restore verification: NOT_RUN"
fi
report "- Rollback rehearsal: MANUAL CHECK. Record PASS only after an approved rehearsal with a previous successful release and successful smoke check."

stage "Finish and share the report"
report ""
report "## Operator confirmation"
report "- Firewall/DNS: MANUAL CHECK. Confirm only operator-restricted SSH, 80, and 443 are public. Do not publish PostgreSQL, Redis, or backend ports."
report "- Alert routing: MANUAL CHECK. Record the test-alert time and result without tokens or recipient details."
report "- DEMO connector and account drills: NOT_STARTED. Keep the account STOPPED and MANUAL until a separate preflight passes."
printf '\n  Evidence file created: %s\n' "$report_file"
printf '  Review it once. Remove any accidental sensitive hostname or path detail you do not want to share, then send its text to me.\n\n'
