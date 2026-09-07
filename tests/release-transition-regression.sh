#!/usr/bin/env bash
set -euo pipefail

repository_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
test_directory=$(mktemp -d)
trap 'rm -rf "$test_directory"' EXIT

mkdir -p "$test_directory/deploy/secrets" "$test_directory/deploy/releases" "$test_directory/bin"
touch "$test_directory/deploy/compose.production.yml"
for secret in app_secret_key caddy_basic_auth_hash postgres_password redis_password; do
  printf 'secret\n' >"$test_directory/deploy/secrets/$secret"
  chmod 600 "$test_directory/deploy/secrets/$secret"
done

previous_snapshot="$test_directory/deploy/releases/active.env"
older_snapshot="$test_directory/deploy/releases/older.env"
candidate="$test_directory/candidate.env"
for file in "$previous_snapshot" "$older_snapshot" "$candidate"; do
  printf '%s\n' 'BACKEND_IMAGE=example.invalid/backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' 'DOMAIN=dashboard.example.com' 'ACME_EMAIL=ops@example.com' 'CADDY_BASIC_AUTH_USER=operator' >"$file"
done
printf '%s\n' "$previous_snapshot" >"$test_directory/deploy/releases/current"
printf '%s\n' "$older_snapshot" >"$test_directory/deploy/releases/previous"

cat >"$test_directory/bin/docker" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$DOCKER_LOG"
EOF
chmod +x "$test_directory/bin/docker"
cat >"$test_directory/smoke" <<'EOF'
#!/usr/bin/env bash
case "${1##*/}" in
  release-*) exit 1 ;;
esac
EOF
chmod +x "$test_directory/smoke"

if PATH="$test_directory/bin:$PATH" DOCKER_LOG="$test_directory/docker.log" REPOSITORY_ROOT="$test_directory" SMOKE_SCRIPT="$test_directory/smoke" "$repository_root/scripts/release.sh" deploy "$candidate"; then
  printf 'expected failed candidate smoke check\n' >&2
  exit 1
fi

[[ $(<"$test_directory/deploy/releases/current") == "$previous_snapshot" ]] || { printf 'current metadata changed after failed release\n' >&2; exit 1; }
[[ $(<"$test_directory/deploy/releases/previous") == "$older_snapshot" ]] || { printf 'previous metadata changed after failed release\n' >&2; exit 1; }
grep -Fq -- "--env-file $previous_snapshot" "$test_directory/docker.log" || { printf 'recorded active release was not restored\n' >&2; exit 1; }
printf 'release transition regression passed\n'
