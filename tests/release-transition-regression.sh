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
printf 'CURRENT=%s\nPREVIOUS=%s\n' "$previous_snapshot" "$older_snapshot" >"$test_directory/deploy/releases/state"

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

grep -Fxq "CURRENT=$previous_snapshot" "$test_directory/deploy/releases/state" || { printf 'current metadata changed after failed release\n' >&2; exit 1; }
grep -Fxq "PREVIOUS=$older_snapshot" "$test_directory/deploy/releases/state" || { printf 'previous metadata changed after failed release\n' >&2; exit 1; }
grep -Fq -- "--env-file $previous_snapshot" "$test_directory/docker.log" || { printf 'recorded active release was not restored\n' >&2; exit 1; }
: >"$test_directory/docker.log"

cat >"$test_directory/rollback-smoke" <<'EOF'
#!/usr/bin/env bash
[[ "${1##*/}" != 'older.env' ]]
EOF
chmod +x "$test_directory/rollback-smoke"

if PATH="$test_directory/bin:$PATH" DOCKER_LOG="$test_directory/docker.log" REPOSITORY_ROOT="$test_directory" SMOKE_SCRIPT="$test_directory/rollback-smoke" "$repository_root/scripts/release.sh" rollback; then
  printf 'expected failed rollback smoke check\n' >&2
  exit 1
fi
grep -Fxq "CURRENT=$previous_snapshot" "$test_directory/deploy/releases/state" || { printf 'current metadata changed after failed rollback\n' >&2; exit 1; }
grep -Fxq "PREVIOUS=$older_snapshot" "$test_directory/deploy/releases/state" || { printf 'previous metadata changed after failed rollback\n' >&2; exit 1; }
grep -Fq -- "--env-file $previous_snapshot" "$test_directory/docker.log" || { printf 'recorded active release was not restored after failed rollback\n' >&2; exit 1; }

cat >"$test_directory/successful-smoke" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$test_directory/successful-smoke"

PATH="$test_directory/bin:$PATH" DOCKER_LOG="$test_directory/docker.log" REPOSITORY_ROOT="$test_directory" SMOKE_SCRIPT="$test_directory/successful-smoke" "$repository_root/scripts/release.sh" rollback
grep -Fxq "CURRENT=$older_snapshot" "$test_directory/deploy/releases/state" || { printf 'first rollback did not activate the prior release\n' >&2; exit 1; }
grep -Fxq "PREVIOUS=$previous_snapshot" "$test_directory/deploy/releases/state" || { printf 'first rollback did not retain the formerly active release\n' >&2; exit 1; }

PATH="$test_directory/bin:$PATH" DOCKER_LOG="$test_directory/docker.log" REPOSITORY_ROOT="$test_directory" SMOKE_SCRIPT="$test_directory/successful-smoke" "$repository_root/scripts/release.sh" rollback
grep -Fxq "CURRENT=$previous_snapshot" "$test_directory/deploy/releases/state" || { printf 'second rollback could not return to the formerly active release\n' >&2; exit 1; }
grep -Fxq "PREVIOUS=$older_snapshot" "$test_directory/deploy/releases/state" || { printf 'second rollback did not preserve its previous release\n' >&2; exit 1; }
printf 'release transition regression passed\n'
