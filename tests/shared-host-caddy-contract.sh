#!/usr/bin/env bash
set -euo pipefail

repository_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
compose_file="$repository_root/deploy/compose.shared-host-caddy.yml"
caddyfile="$repository_root/deploy/caddy/Caddyfile.shared-host.example"
release_example="$repository_root/deploy/release.shared-host-caddy.env.example"

grep -Fqx 'DEPLOYMENT_MODE=shared-host-caddy' "$release_example"
grep -Fqx 'DOMAIN=trading.optitek.xyz' "$release_example"
grep -Fq -- '"127.0.0.1:18000:8000"' "$compose_file"
grep -Fq -- '"127.0.0.1:13000:3000"' "$compose_file"
! grep -Eq '^  caddy:' "$compose_file"
! grep -Fq -- '"80:80"' "$compose_file"
! grep -Fq -- '"443:443"' "$compose_file"
grep -Fq 'TRUSTED_PROXY_IPS: 172.31.0.1' "$compose_file"
grep -Fq 'internal: true' "$compose_file"
grep -Fq '  ingress:' "$compose_file"
grep -Fq 'subnet: 172.31.0.0/24' "$compose_file"
backend_block=$(sed -n '/^  backend:$/,/^  postgres:$/p' "$compose_file")
frontend_block=$(sed -n '/^  frontend:$/,/^  backend:$/p' "$compose_file")
postgres_block=$(sed -n '/^  postgres:$/,/^  redis:$/p' "$compose_file")
redis_block=$(sed -n '/^  redis:$/,/^networks:$/p' "$compose_file")
[[ "$backend_block" == *'      - ingress'* ]] || { printf 'backend must use the loopback ingress network\n' >&2; exit 1; }
[[ "$frontend_block" == *'      - ingress'* ]] || { printf 'frontend must use the loopback ingress network\n' >&2; exit 1; }
[[ "$postgres_block" != *'ingress'* && "$redis_block" != *'ingress'* ]] || { printf 'database services must not use the ingress network\n' >&2; exit 1; }
grep -Fq 'basic_auth' "$caddyfile"
grep -Fq 'respond "ok" 200' "$caddyfile"
grep -Fq 'health_uri /health/live' "$caddyfile"
grep -Fq '@backend path /api/* /docs* /openapi.json /ws/* /health/live' "$caddyfile"
grep -Fq 'reverse_proxy 127.0.0.1:13000' "$caddyfile"
for header in \
  'header_up X-Authenticated-User {http.auth.user.id}' \
  'header_up X-Forwarded-For {remote_host}' \
  'header_up X-Forwarded-Host {host}' \
  'header_up X-Forwarded-Proto {scheme}'; do
  grep -Fq "$header" "$caddyfile"
done

if command -v caddy >/dev/null 2>&1; then
  TRADING_CADDY_BASIC_AUTH_USER=operator \
  TRADING_CADDY_BASIC_AUTH_HASH=HASH_PLACEHOLDER \
    caddy adapt --config "$caddyfile" --adapter caddyfile >/dev/null
fi

printf 'shared-host Caddy contract passed\n'
