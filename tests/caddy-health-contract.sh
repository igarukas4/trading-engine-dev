#!/usr/bin/env bash
set -euo pipefail

repository_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
caddyfile="$repository_root/deploy/caddy/Caddyfile"
test_directory=$(mktemp -d)
caddy_pid=''
backend_pid=''

cleanup() {
  [[ -z "$caddy_pid" ]] || kill "$caddy_pid" 2>/dev/null || true
  [[ -z "$backend_pid" ]] || kill "$backend_pid" 2>/dev/null || true
  wait "$caddy_pid" 2>/dev/null || true
  wait "$backend_pid" 2>/dev/null || true
  rm -rf "$test_directory"
}
trap cleanup EXIT

command -v caddy >/dev/null 2>&1 || {
  printf 'caddy is required for the Caddy health contract test\n' >&2
  exit 1
}

auth_hash=$(caddy hash-password --plaintext 'test-password')
DOMAIN=dashboard.example.com \
ACME_EMAIL=ops@example.com \
CADDY_BASIC_AUTH_USER=operator \
CADDY_BASIC_AUTH_HASH="$auth_hash" \
  caddy adapt --config "$caddyfile" --adapter caddyfile >"$test_directory/adapted.json"

ADAPTED_CONFIG="$test_directory/adapted.json" python3 - <<'PY'
import json
import os

with open(os.environ["ADAPTED_CONFIG"], encoding="utf-8") as adapted_file:
    config = json.load(adapted_file)


def contains_handler(value, handler):
    if isinstance(value, dict):
        return value.get("handler") == handler or any(
            contains_handler(child, handler) for child in value.values()
        )
    if isinstance(value, list):
        return any(contains_handler(child, handler) for child in value)
    return False


def dictionaries(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from dictionaries(child)
    elif isinstance(value, list):
        for child in value:
            yield from dictionaries(child)


health_routes = [
    route
    for route in dictionaries(config)
    if route.get("match") == [{"path": ["/healthz"]}]
]
assert len(health_routes) == 1, health_routes
health_route = health_routes[0]
assert contains_handler(health_route, "static_response"), health_route
assert not contains_handler(health_route, "authentication"), health_route

assert any(
    "match" not in route
    and contains_handler(route, "reverse_proxy")
    and contains_handler(route, "authentication")
    for route in dictionaries(config)
), "protected reverse proxy route is missing authentication"
PY

if ! command -v curl >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then
  printf 'caddy health contract passed (runtime check skipped: curl and python3 are required)\n'
  exit 0
fi

caddy_port=19873
backend_port=19874
mkdir -p "$test_directory/backend/health"
printf '%s\n' 'backend-ok' >"$test_directory/backend/dashboard"
printf '%s\n' 'live' >"$test_directory/backend/health/live"

python3 -m http.server "$backend_port" --bind 127.0.0.1 --directory "$test_directory/backend" \
  >"$test_directory/backend.log" 2>&1 &
backend_pid=$!

for _ in {1..50}; do
  backend_status=$(curl --silent --output /dev/null --write-out '%{http_code}' "http://127.0.0.1:$backend_port/health/live" || true)
  [[ "$backend_status" == '200' ]] && break
  sleep 0.1
done
[[ "$backend_status" == '200' ]] || {
  printf 'backend fixture did not become ready, got %s\n' "$backend_status" >&2
  exit 1
}

sed \
  -e "s|{\$DOMAIN}|http://127.0.0.1:$caddy_port|" \
  -e "s|backend:8000|127.0.0.1:$backend_port|" \
  -e "s|/var/log/caddy/access.json|$test_directory/access.json|" \
  "$caddyfile" >"$test_directory/runtime.Caddyfile"

XDG_CONFIG_HOME="$test_directory/config" \
XDG_DATA_HOME="$test_directory/data" \
DOMAIN="http://127.0.0.1:$caddy_port" \
ACME_EMAIL=ops@example.com \
CADDY_BASIC_AUTH_USER=operator \
CADDY_BASIC_AUTH_HASH="$auth_hash" \
  caddy run --config "$test_directory/runtime.Caddyfile" --adapter caddyfile \
  >"$test_directory/caddy.log" 2>&1 &
caddy_pid=$!

for _ in {1..50}; do
  status=$(curl --silent --output /dev/null --write-out '%{http_code}' "http://127.0.0.1:$caddy_port/healthz" || true)
  [[ "$status" != '000' ]] && break
  sleep 0.1
done

health_body="$test_directory/health.body"
health_status=$(curl --silent --output "$health_body" --write-out '%{http_code}' "http://127.0.0.1:$caddy_port/healthz" || true)
[[ "$health_status" == '200' ]] || {
  printf 'expected unauthenticated /healthz to return 200, got %s\n' "$health_status" >&2
  exit 1
}
grep -Fxq 'ok' "$health_body" || {
  printf 'expected /healthz body to be ok\n' >&2
  exit 1
}

unauthenticated_status=$(curl --silent --output /dev/null --write-out '%{http_code}' "http://127.0.0.1:$caddy_port/dashboard" || true)
[[ "$unauthenticated_status" == '401' ]] || {
  printf 'expected unauthenticated protected route to return 401, got %s\n' "$unauthenticated_status" >&2
  exit 1
}

authenticated_body="$test_directory/authenticated.body"
authenticated_status=$(curl --silent --user 'operator:test-password' --output "$authenticated_body" --write-out '%{http_code}' "http://127.0.0.1:$caddy_port/dashboard" || true)
[[ "$authenticated_status" == '200' ]] || {
  printf 'expected authenticated protected route to return 200, got %s\n' "$authenticated_status" >&2
  exit 1
}
grep -Fxq 'backend-ok' "$authenticated_body" || {
  printf 'authenticated request did not reach the backend fixture\n' >&2
  exit 1
}

printf 'caddy health contract passed\n'
