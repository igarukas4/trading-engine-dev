#!/usr/bin/env bash
set -euo pipefail

repository_root=${REPOSITORY_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
compose_file="$repository_root/deploy/compose.production.yml"
release_directory="$repository_root/deploy/releases"
release_metadata="$release_directory/state"
smoke_script=${SMOKE_SCRIPT:-"$repository_root/scripts/smoke-release.sh"}
command=${1:-}
release_environment_keys=(
  BACKEND_IMAGE
  CADDY_IMAGE
  TIMESCALEDB_IMAGE
  REDIS_IMAGE
  DOMAIN
  ACME_EMAIL
  CADDY_BASIC_AUTH_USER
)

usage() {
  printf 'usage: %s deploy RELEASE_ENV_FILE | rollback\n' "${0##*/}" >&2
  exit 2
}

validate_release() {
  local release_file=$1
  local image_key line key
  declare -A release_keys_seen=()
  [[ -f "$release_file" ]] || { printf 'release environment not found: %s\n' "$release_file" >&2; exit 1; }

  while IFS= read -r line || [[ -n "$line" ]]; do
    line=${line%$'\r'}
    [[ -z "$line" || "$line" == \#* ]] && continue
    [[ "$line" =~ ^([a-zA-Z_][a-zA-Z0-9_]*)= ]] || {
      printf 'invalid release environment line\n' >&2
      exit 1
    }
    key=${BASH_REMATCH[1]}
    [[ -z "${release_keys_seen[$key]+seen}" ]] || {
      printf 'duplicate release environment key: %s\n' "$key" >&2
      exit 1
    }
    release_keys_seen[$key]=1
  done <"$release_file"

  for image_key in BACKEND_IMAGE CADDY_IMAGE TIMESCALEDB_IMAGE REDIS_IMAGE; do
    grep -Eq "^${image_key}=[^[:space:]]+@sha256:[a-f0-9]{64}$" "$release_file" || {
      printf '%s must be pinned to a sha256 digest\n' "$image_key" >&2
      exit 1
    }
  done
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

run_sanitized() {
  local key
  local -a sanitized_environment=()
  for key in "${release_environment_keys[@]}"; do
    sanitized_environment+=(-u "$key")
  done
  env "${sanitized_environment[@]}" "$@"
}

run_compose() {
  local release_file=$1
  shift
  run_sanitized docker compose --env-file "$release_file" -f "$compose_file" "$@"
}

run_smoke() {
  run_sanitized "$smoke_script" "$1"
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

  run_compose "$snapshot" config --quiet
  run_compose "$snapshot" pull
  if ! run_compose "$snapshot" up --detach --wait --remove-orphans || ! run_smoke "$snapshot"; then
    printf 'release failed; restoring the recorded active release\n' >&2
    if [[ -n "$current_snapshot" ]]; then
      [[ -f "$current_snapshot" ]] || { printf 'recorded active release file is missing: %s\n' "$current_snapshot" >&2; exit 1; }
      run_compose "$current_snapshot" config --quiet
      run_compose "$current_snapshot" pull
      run_compose "$current_snapshot" up --detach --wait --remove-orphans
      run_smoke "$current_snapshot"
    else
      run_compose "$snapshot" down --remove-orphans
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
  if ! run_compose "$rollback_snapshot" config --quiet || \
     ! run_compose "$rollback_snapshot" pull || \
     ! run_compose "$rollback_snapshot" up --detach --wait --remove-orphans || \
     ! run_smoke "$rollback_snapshot"; then
    printf 'rollback failed; restoring the recorded active release\n' >&2
    run_compose "$formerly_current" config --quiet
    run_compose "$formerly_current" pull
    run_compose "$formerly_current" up --detach --wait --remove-orphans
    run_smoke "$formerly_current"
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
