#!/usr/bin/env bash
set -euo pipefail

repository_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
compose_file="$repository_root/deploy/compose.production.yml"
release_file=${1:?"usage: smoke-release.sh RELEASE_ENV_FILE"}

[[ -f "$release_file" ]] || { printf 'release environment not found: %s\n' "$release_file" >&2; exit 1; }

read_release_env() {
  local line key value
  while IFS= read -r line || [[ -n "$line" ]]; do
    line=${line%$'\r'}
    [[ -z "$line" || "$line" == \#* ]] && continue
    [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || {
      printf 'invalid release environment line: %s\n' "$line" >&2
      exit 1
    }
    key=${BASH_REMATCH[1]}
    value=${BASH_REMATCH[2]}
    if [[ "$value" =~ ^\"(.*)\"$ || "$value" =~ ^\'(.*)\'$ ]]; then
      value=${BASH_REMATCH[1]}
    fi
    case "$key" in
      DOMAIN) DOMAIN=$value ;;
      CADDY_BASIC_AUTH_USER) CADDY_BASIC_AUTH_USER=$value ;;
    esac
  done <"$release_file"
}

read_release_env

: "${DOMAIN:?DOMAIN is required}"
curl --fail --silent --show-error --resolve "${DOMAIN}:443:127.0.0.1" "https://${DOMAIN}/healthz" | grep -qx 'ok'

unauthenticated_status=$(curl --silent --output /dev/null --write-out '%{http_code}' --resolve "${DOMAIN}:443:127.0.0.1" "https://${DOMAIN}/health/live")
[[ "$unauthenticated_status" == '401' ]] || { printf 'expected unauthenticated backend request to return 401, got %s\n' "$unauthenticated_status" >&2; exit 1; }

if [[ -n "${SMOKE_BASIC_AUTH_PASSWORD:-}" ]]; then
  : "${CADDY_BASIC_AUTH_USER:?CADDY_BASIC_AUTH_USER is required with SMOKE_BASIC_AUTH_PASSWORD}"
  curl --fail --silent --show-error --user "${CADDY_BASIC_AUTH_USER}:${SMOKE_BASIC_AUTH_PASSWORD}" --resolve "${DOMAIN}:443:127.0.0.1" "https://${DOMAIN}/health/live" >/dev/null
fi

if docker compose --env-file "$release_file" -f "$compose_file" port backend 8000 >/dev/null 2>&1; then
  printf 'backend port is directly published\n' >&2
  exit 1
fi

printf 'smoke release passed for %s\n' "$DOMAIN"
