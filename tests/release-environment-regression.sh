#!/usr/bin/env bash
set -euo pipefail

repository_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
test_directory=$(mktemp -d)
trap 'rm -rf "$test_directory"' EXIT

mkdir -p "$test_directory/deploy/secrets" "$test_directory/deploy/releases" "$test_directory/bin"
cat >"$test_directory/deploy/compose.production.yml" <<'EOF'
services:
  backend:
    image: ${BACKEND_IMAGE:?set BACKEND_IMAGE}
EOF
for secret in app_secret_key caddy_basic_auth_hash postgres_password redis_password; do
  printf 'secret\n' >"$test_directory/deploy/secrets/$secret"
  chmod 600 "$test_directory/deploy/secrets/$secret"
done

release_file="$test_directory/release.env"
rendered_config="$test_directory/rendered-config.yml"
printf '%s\n' \
  'BACKEND_IMAGE=example.invalid/backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' \
  'CADDY_IMAGE=caddy:2.10.2-alpine@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' \
  'TIMESCALEDB_IMAGE=timescale/timescaledb:2.17.2-pg16@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' \
  'REDIS_IMAGE=redis:7.4.2-alpine@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' \
  'DOMAIN=dashboard.example.com' 'ACME_EMAIL=ops@example.com' 'CADDY_BASIC_AUTH_USER=operator' >"$release_file"

cat >"$test_directory/bin/docker" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

if [[ ${1:-} == compose && ${*: -2:1} == config && ${*: -1} == --quiet ]]; then
  rendered_args=()
  for argument in "$@"; do
    [[ $argument == --quiet ]] || rendered_args+=("$argument")
  done
  /usr/bin/docker "${rendered_args[@]}" >"$RENDERED_CONFIG"
fi
EOF
chmod +x "$test_directory/bin/docker"

cat >"$test_directory/smoke" <<'EOF'
#!/usr/bin/env bash
[[ -z ${BACKEND_IMAGE+x} ]] || exit 1
exit 0
EOF
chmod +x "$test_directory/smoke"

if PATH="$test_directory/bin:$PATH" \
  BACKEND_IMAGE='evil.example/backend:latest' \
  RENDERED_CONFIG="$rendered_config" \
  REPOSITORY_ROOT="$test_directory" \
  SMOKE_SCRIPT="$test_directory/smoke" \
  "$repository_root/scripts/release.sh" deploy "$release_file"; then
  :
else
  printf 'release rendering unexpectedly failed\n' >&2
  exit 1
fi

grep -Fqx '    image: example.invalid/backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' "$rendered_config" || {
  printf 'release rendering did not use the pinned backend image\n' >&2
  exit 1
}
! grep -Fq 'evil.example/backend:latest' "$rendered_config" || {
  printf 'ambient BACKEND_IMAGE overrode the pinned backend image\n' >&2
  exit 1
}

duplicate_release_file="$test_directory/duplicate-release.env"
cp "$release_file" "$duplicate_release_file"
printf '%s\n' 'DOMAIN=duplicate.example.com' >>"$duplicate_release_file"
if PATH="$test_directory/bin:$PATH" \
  REPOSITORY_ROOT="$test_directory" \
  SMOKE_SCRIPT="$test_directory/smoke" \
  "$repository_root/scripts/release.sh" deploy "$duplicate_release_file" 2>"$test_directory/duplicate-error"; then
  printf 'duplicate release environment key was accepted\n' >&2
  exit 1
fi
grep -Fxq 'duplicate release environment key: DOMAIN' "$test_directory/duplicate-error" || {
  printf 'duplicate release environment key was not reported\n' >&2
  exit 1
}

printf 'release environment regression passed\n'
