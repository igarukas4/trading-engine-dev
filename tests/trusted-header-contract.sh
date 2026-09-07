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
request = proxies[0]["headers"]["request"]
request_headers = request["set"]
assert request_headers == expected, request_headers
assert not set(expected) & set(request.get("add", {})), request.get("add", {})
PY
else
  CADDYFILE="$caddyfile" python3 - <<'PY'
import os
import re

source = open(os.environ["CADDYFILE"], encoding="utf-8").read()
match = re.search(r"(?ms)^\s*reverse_proxy backend:8000 \{(?P<body>.*?)^\s*\}", source)
assert match, "backend reverse_proxy block not found"
proxy = match.group("body")
expected = {
    "X-Authenticated-User": "{http.auth.user.id}",
    "X-Forwarded-For": "{remote_host}",
    "X-Forwarded-Host": "{host}",
    "X-Forwarded-Proto": "{scheme}",
}
for header, value in expected.items():
    directive = rf"(?m)^\s*header_up {re.escape(header)} {re.escape(value)}\s*$"
    assert re.search(directive, proxy), f"missing overwrite for {header}"
    assert not re.search(rf"(?m)^\s*header_up (?:\+|-){re.escape(header)}\b", proxy), f"ambiguous header operation for {header}"
PY
fi

printf 'trusted header contract passed\n'
