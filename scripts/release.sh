#!/usr/bin/env bash
set -euo pipefail

repository_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
compose_file="$repository_root/deploy/compose.production.yml"
release_directory="$repository_root/deploy/releases"
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

deploy() {
  local source_file=$1 snapshot previous_snapshot
  validate_release "$source_file"
  mkdir -p "$release_directory"
  umask 077
  snapshot="$release_directory/release-$(date -u +%Y%m%dT%H%M%SZ)-$RANDOM.env"
  cp "$source_file" "$snapshot"
  previous_snapshot=''
  [[ -f "$release_directory/current" ]] && previous_snapshot=$(<"$release_directory/current")

  docker compose --env-file "$snapshot" -f "$compose_file" config --quiet
  docker compose --env-file "$snapshot" -f "$compose_file" pull
  docker compose --env-file "$snapshot" -f "$compose_file" up --detach --wait --remove-orphans
  "$repository_root/scripts/smoke-release.sh" "$snapshot"

  [[ -n "$previous_snapshot" ]] && printf '%s\n' "$previous_snapshot" >"$release_directory/previous"
  printf '%s\n' "$snapshot" >"$release_directory/current"
  printf 'release deployed: %s\n' "$snapshot"
}

rollback() {
  local snapshot
  [[ -f "$release_directory/previous" ]] || { printf 'no previous release is recorded\n' >&2; exit 1; }
  snapshot=$(<"$release_directory/previous")
  [[ -f "$snapshot" ]] || { printf 'previous release file is missing: %s\n' "$snapshot" >&2; exit 1; }
  docker compose --env-file "$snapshot" -f "$compose_file" config --quiet
  docker compose --env-file "$snapshot" -f "$compose_file" pull
  docker compose --env-file "$snapshot" -f "$compose_file" up --detach --wait --remove-orphans
  "$repository_root/scripts/smoke-release.sh" "$snapshot"
  printf '%s\n' "$snapshot" >"$release_directory/current"
  printf 'rolled back to: %s\n' "$snapshot"
}

case "$command" in
  deploy) [[ $# == 2 ]] || usage; deploy "$2" ;;
  rollback) [[ $# == 1 ]] || usage; rollback ;;
  *) usage ;;
esac
