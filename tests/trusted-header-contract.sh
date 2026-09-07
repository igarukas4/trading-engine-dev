#!/usr/bin/env bash
set -euo pipefail

repository_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
compose_file="$repository_root/deploy/compose.production.yml"
caddyfile="$repository_root/deploy/caddy/Caddyfile"
smoke_script="$repository_root/scripts/smoke-release.sh"

grep -Fq 'TRUSTED_ACTOR_HEADER: X-Authenticated-User' "$compose_file"
grep -Fq 'TRUSTED_PROXY_IPS: 172.30.0.2' "$compose_file"
for header in \
  'X-Authenticated-User: forged-smoke-actor' \
  'X-Forwarded-For: 198.51.100.23' \
  'X-Forwarded-Host: forged.example' \
  'X-Forwarded-Proto: http'; do
  grep -Fq -- "--header '$header'" "$smoke_script"
done

if command -v caddy >/dev/null 2>&1; then
  adapted_config=$(
    DOMAIN=dashboard.example.com \
    ACME_EMAIL=ops@example.com \
    CADDY_BASIC_AUTH_USER=operator \
    CADDY_BASIC_AUTH_HASH=HASH_PLACEHOLDER \
      caddy adapt --config "$caddyfile" --adapter caddyfile 2>/dev/null
  )
  ADAPTED_CONFIG="$adapted_config" python3 - <<'PY'
import json
import os

config = json.loads(os.environ["ADAPTED_CONFIG"])
expected = {
    "X-Authenticated-User": ["{http.auth.user.id}"],
    "X-Forwarded-For": ["{http.request.remote.host}"],
    "X-Forwarded-Host": ["{http.request.host}"],
    "X-Forwarded-Proto": ["{http.request.scheme}"],
}

proxies = []
pending = [config]
while pending:
    value = pending.pop()
    if isinstance(value, dict):
        if value.get("handler") == "reverse_proxy":
            proxies.append(value)
        pending.extend(value.values())
    elif isinstance(value, list):
        pending.extend(value)

assert len(proxies) == 1, f"expected one reverse proxy, found {len(proxies)}"
request_headers = proxies[0]["headers"]["request"]["set"]
assert request_headers == expected, request_headers
PY
else
  for directive in \
    'header_up X-Authenticated-User {http.auth.user.id}' \
    'header_up X-Forwarded-For {remote_host}' \
    'header_up X-Forwarded-Host {host}' \
    'header_up X-Forwarded-Proto {scheme}'; do
    grep -Fq "$directive" "$caddyfile"
  done
fi

printf 'trusted header contract passed\n'
