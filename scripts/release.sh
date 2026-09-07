#!/usr/bin/env bash
set -euo pipefail

repository_root=${REPOSITORY_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
compose_file="$repository_root/deploy/compose.production.yml"
release_directory="$repository_root/deploy/releases"
release_metadata="$release_directory/state"
smoke_script=${SMOKE_SCRIPT:-"$repository_root/scripts/smoke-release.sh"}
command=${1:-}

usage() {
  printf 'usage: %s deploy RELEASE_ENV_FILE | rollback\n' "${0##*/}" >&2
  exit 2
}

validate_release() {
  local release_file=$1
  [[ -f "$release_file" ]] || { printf 'release environment not found: %s\n' "$release_file" >&2; exit 1; }
  grep -Eq '^BACKEND_IMAGE=.*@sha256:[a-f0-9]{64}$' "$release_file" || { printf 'BACKEND_IMAGE must be pinned to a sha256 digest\n' >&2; exit 1; }
  for key in DOMAIN ACME_EMAIL CADDY_BASIC_AUTH_USER; do
    grep -Eq "^${key}=.+" "$release_file" || { printf '%s is required\n' "$key" >&2; exit 1; }
  done
  for secret in app_secret_key caddy_basic_auth_hash postgres_password redis_password; do
    local secret_file="$repository_root/deploy/secrets/$secret"
    [[ -s "$secret_file" ]] || { printf 'missing or empty secret: %s\n' "$secret_file" >&2; exit 1; }
    [[ $(stat -c '%a' "$secret_file") == '600' ]] || { printf 'secret must have mode 600: %s\n' "$secret_file" >&2; exit 1; }
    ! git -C "$repository_root" ls-files --error-unmatch "${secret_file#"$repository_root/"}" >/dev/null 2>&1 || { printf 'refusing tracked secret: %s\n' "$secret_file" >&2; exit 1; }
  done
}

current_snapshot=''
previous_snapshot=''

read_release_metadata() {
  local key value
  current_snapshot=''
  previous_snapshot=''
  [[ -f "$release_metadata" ]] || return 0

  while IFS='=' read -r key value || [[ -n "$key" ]]; do
    case "$key" in
      CURRENT) current_snapshot=$value ;;
      PREVIOUS) previous_snapshot=$value ;;
      *) printf 'invalid release metadata entry: %s\n' "$key" >&2; exit 1 ;;
    esac
  done <"$release_metadata"

  [[ -n "$current_snapshot" ]] || { printf 'release metadata has no current snapshot\n' >&2; exit 1; }
}

write_release_metadata() {
  local current=$1 previous=$2 temporary_metadata
  temporary_metadata=$(mktemp "$release_directory/.state.XXXXXX")
  {
    printf 'CURRENT=%s\n' "$current"
    printf 'PREVIOUS=%s\n' "$previous"
  } >"$temporary_metadata"
  mv -f "$temporary_metadata" "$release_metadata"
}

deploy() {
  local source_file=$1 snapshot
  validate_release "$source_file"
  mkdir -p "$release_directory"
  umask 077
  snapshot="$release_directory/release-$(date -u +%Y%m%dT%H%M%SZ)-$RANDOM.env"
  cp "$source_file" "$snapshot"
  read_release_metadata

  docker compose --env-file "$snapshot" -f "$compose_file" config --quiet
  docker compose --env-file "$snapshot" -f "$compose_file" pull
  if ! docker compose --env-file "$snapshot" -f "$compose_file" up --detach --wait --remove-orphans || ! "$smoke_script" "$snapshot"; then
    printf 'release failed; restoring the recorded active release\n' >&2
    if [[ -n "$current_snapshot" ]]; then
      [[ -f "$current_snapshot" ]] || { printf 'recorded active release file is missing: %s\n' "$current_snapshot" >&2; exit 1; }
      docker compose --env-file "$current_snapshot" -f "$compose_file" config --quiet
      docker compose --env-file "$current_snapshot" -f "$compose_file" pull
      docker compose --env-file "$current_snapshot" -f "$compose_file" up --detach --wait --remove-orphans
      "$smoke_script" "$current_snapshot"
    else
      docker compose --env-file "$snapshot" -f "$compose_file" down --remove-orphans
    fi
    exit 1
  fi

  write_release_metadata "$snapshot" "$current_snapshot"
  printf 'release deployed: %s\n' "$snapshot"
}

rollback() {
  local rollback_snapshot formerly_current
  read_release_metadata
  [[ -n "$previous_snapshot" ]] || { printf 'no previous release is recorded\n' >&2; exit 1; }
  rollback_snapshot=$previous_snapshot
  formerly_current=$current_snapshot
  [[ -f "$rollback_snapshot" ]] || { printf 'previous release file is missing: %s\n' "$rollback_snapshot" >&2; exit 1; }
  if ! docker compose --env-file "$rollback_snapshot" -f "$compose_file" config --quiet || \
     ! docker compose --env-file "$rollback_snapshot" -f "$compose_file" pull || \
     ! docker compose --env-file "$rollback_snapshot" -f "$compose_file" up --detach --wait --remove-orphans || \
     ! "$smoke_script" "$rollback_snapshot"; then
    printf 'rollback failed; restoring the recorded active release\n' >&2
    docker compose --env-file "$formerly_current" -f "$compose_file" config --quiet
    docker compose --env-file "$formerly_current" -f "$compose_file" pull
    docker compose --env-file "$formerly_current" -f "$compose_file" up --detach --wait --remove-orphans
    "$smoke_script" "$formerly_current"
    exit 1
  fi
  write_release_metadata "$rollback_snapshot" "$formerly_current"
  printf 'rolled back to: %s\n' "$rollback_snapshot"
}

case "$command" in
  deploy) [[ $# == 2 ]] || usage; deploy "$2" ;;
  rollback) [[ $# == 1 ]] || usage; rollback ;;
  *) usage ;;
esac
