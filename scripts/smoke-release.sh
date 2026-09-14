#!/usr/bin/env bash
set -euo pipefail

repository_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
release_file=${1:?"usage: smoke-release.sh RELEASE_ENV_FILE"}
smoke_password_file=${SMOKE_BASIC_AUTH_PASSWORD_FILE:?set SMOKE_BASIC_AUTH_PASSWORD_FILE to a protected Basic Auth password file}
release_environment_keys=(
  DEPLOYMENT_MODE
  BACKEND_IMAGE
  FRONTEND_IMAGE
  CADDY_IMAGE
  TIMESCALEDB_IMAGE
  REDIS_IMAGE
  DOMAIN
  ACME_EMAIL
  CADDY_BASIC_AUTH_USER
)

deployment_mode=dedicated-caddy

run_sanitized() {
  local key
  local -a sanitized_environment=()
  for key in "${release_environment_keys[@]}"; do
    sanitized_environment+=(-u "$key")
  done
  env "${sanitized_environment[@]}" "$@"
}

[[ -f "$release_file" ]] || { printf 'release environment not found: %s\n' "$release_file" >&2; exit 1; }
[[ -s "$smoke_password_file" ]] || { printf 'smoke Basic Auth password file not found or empty: %s\n' "$smoke_password_file" >&2; exit 1; }
[[ $(stat -c '%a' "$smoke_password_file") == '600' ]] || { printf 'smoke Basic Auth password file must have mode 600: %s\n' "$smoke_password_file" >&2; exit 1; }

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
      DEPLOYMENT_MODE) deployment_mode=$value ;;
      DOMAIN) DOMAIN=$value ;;
      CADDY_BASIC_AUTH_USER) CADDY_BASIC_AUTH_USER=$value ;;
    esac
  done <"$release_file"
}

read_release_env

: "${DOMAIN:?DOMAIN is required}"
case "$deployment_mode" in
  dedicated-caddy) : "${CADDY_BASIC_AUTH_USER:?CADDY_BASIC_AUTH_USER is required}" ;;
  shared-host-caddy) : "${CADDY_BASIC_AUTH_USER:?CADDY_BASIC_AUTH_USER is required}" ;;
  *) printf 'unsupported DEPLOYMENT_MODE: %s\n' "$deployment_mode" >&2; exit 1 ;;
esac
curl --fail --silent --show-error --resolve "${DOMAIN}:443:127.0.0.1" "https://${DOMAIN}/healthz" | grep -qx 'ok'

unauthenticated_status=''
for _ in {1..20}; do
  unauthenticated_status=$(curl --silent --output /dev/null --write-out '%{http_code}' --resolve "${DOMAIN}:443:127.0.0.1" "https://${DOMAIN}/health/live")
  [[ "$unauthenticated_status" == '401' ]] && break
  sleep 1
done
[[ "$unauthenticated_status" == '401' ]] || { printf 'expected unauthenticated backend request to return 401, got %s\n' "$unauthenticated_status" >&2; exit 1; }

forged_header_status=$(curl --silent --output /dev/null --write-out '%{http_code}' \
  --header 'X-Authenticated-User: forged-smoke-actor' \
  --header 'X-Forwarded-For: 198.51.100.23' \
  --header 'X-Forwarded-Host: forged.example' \
  --header 'X-Forwarded-Proto: http' \
  --resolve "${DOMAIN}:443:127.0.0.1" "https://${DOMAIN}/health/live")
[[ "$forged_header_status" == '401' ]] || { printf 'forged trusted headers bypassed the public authentication boundary with status %s\n' "$forged_header_status" >&2; exit 1; }

curl_config_escape() {
  local value=$1
  value=${value//\\/\\\\}
  value=${value//\"/\\\"}
  printf '%s' "$value"
}
smoke_password=$(<"$smoke_password_file")
curl_configuration=$(printf 'user = "%s:%s"\n' "$(curl_config_escape "$CADDY_BASIC_AUTH_USER")" "$(curl_config_escape "$smoke_password")")
printf '%s' "$curl_configuration" | \
  curl --config - --fail --silent --show-error \
    --header 'X-Authenticated-User: forged-smoke-actor' \
    --header 'X-Forwarded-For: 198.51.100.23' \
    --header 'X-Forwarded-Host: forged.example' \
    --header 'X-Forwarded-Proto: http' \
    --resolve "${DOMAIN}:443:127.0.0.1" "https://${DOMAIN}/health/live" >/dev/null
printf '%s' "$curl_configuration" | \
  curl --config - --fail --silent --show-error \
    --resolve "${DOMAIN}:443:127.0.0.1" "https://${DOMAIN}/" | grep -q 'Dashboard'
unset curl_configuration smoke_password

if [[ "$deployment_mode" == dedicated-caddy ]]; then
  compose_file="$repository_root/deploy/compose.production.yml"
  if run_sanitized docker compose -p trading-engine --env-file "$release_file" -f "$compose_file" port backend 8000 >/dev/null 2>&1; then
    printf 'backend port is directly published\n' >&2
    exit 1
  fi
else
  compose_file="$repository_root/deploy/compose.shared-host-caddy.yml"
  backend_port=$(run_sanitized docker compose -p trading-engine --env-file "$release_file" -f "$compose_file" port backend 8000)
  [[ "$backend_port" == 127.0.0.1:* ]] || { printf 'backend must publish only to loopback, got %s\n' "$backend_port" >&2; exit 1; }
  frontend_port=$(run_sanitized docker compose -p trading-engine --env-file "$release_file" -f "$compose_file" port frontend 3000)
  [[ "$frontend_port" == 127.0.0.1:* ]] || { printf 'frontend must publish only to loopback, got %s\n' "$frontend_port" >&2; exit 1; }
fi

printf 'smoke release passed for %s\n' "$DOMAIN"
