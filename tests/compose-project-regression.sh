#!/usr/bin/env bash
set -euo pipefail

repository_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
test_directory=$(mktemp -d)
trap 'rm -rf "$test_directory"' EXIT

mkdir -p "$test_directory/deploy/secrets" "$test_directory/deploy/releases" "$test_directory/bin"
touch "$test_directory/deploy/compose.production.yml"
for secret in app_secret_key caddy_basic_auth_hash postgres_password redis_password; do
  if [[ "$secret" == postgres_password ]]; then
    printf 'deployed-postgres-password\n' >"$test_directory/deploy/secrets/$secret"
  else
    printf 'secret\n' >"$test_directory/deploy/secrets/$secret"
  fi
  chmod 600 "$test_directory/deploy/secrets/$secret"
done

release_file="$test_directory/release.env"
printf '%s\n' \
  'BACKEND_IMAGE=example.invalid/backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' \
  'CADDY_IMAGE=caddy:2.10.2-alpine@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' \
  'TIMESCALEDB_IMAGE=timescale/timescaledb:2.17.2-pg16@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' \
  'REDIS_IMAGE=redis:7.4.2-alpine@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' \
  'DOMAIN=dashboard.example.com' 'ACME_EMAIL=ops@example.com' 'CADDY_BASIC_AUTH_USER=operator' >"$release_file"

cat >"$test_directory/bin/docker" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

if [[ ${1:-} != compose || ${2:-} != -p || ${3:-} != trading-engine ]]; then
  printf 'Compose project was not pinned: %s\n' "$*" >&2
  exit 1
fi
printf '%s\n' "$*" >>"$DOCKER_LOG"

if [[ "$*" == *' port backend 8000' ]]; then
  exit 1
fi

if [[ "$*" == *'exec -T postgres pg_dump'* ]]; then
  printf 'legacy unauthenticated pg_dump invocation\n' >&2
  exit 1
fi

if [[ "$*" == *'exec -T postgres sh -ec'* ]]; then
  docker_arguments="$*"
  [[ "$docker_arguments" == *'cat /run/secrets/postgres_password'* ]] || {
    printf 'backup did not read the deployed PostgreSQL secret inside the container\n' >&2
    exit 1
  }
  [[ "$docker_arguments" == *'export PGPASSFILE='* ]] || {
    printf 'backup did not export PGPASSFILE inside the container\n' >&2
    exit 1
  }
  [[ "$docker_arguments" == *'pg_dump -U trading_engine -d trading_engine --format=custom'* ]] || {
    printf 'backup did not invoke pg_dump with explicit database credentials\n' >&2
    exit 1
  }
  [[ "$docker_arguments" != *'deployed-postgres-password'* ]] || {
    printf 'backup interpolated the PostgreSQL secret into command arguments\n' >&2
    exit 1
  }
  printf 'mock postgres dump\n'
fi
EOF
chmod +x "$test_directory/bin/docker"

cat >"$test_directory/smoke" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
exit 0
EOF
chmod +x "$test_directory/smoke"

docker_log="$test_directory/docker.log"
PATH="$test_directory/bin:$PATH" \
  COMPOSE_PROJECT_NAME=ambient-project-must-not-win \
  DOCKER_LOG="$docker_log" \
  REPOSITORY_ROOT="$test_directory" \
  SMOKE_SCRIPT="$test_directory/smoke" \
  "$repository_root/scripts/release.sh" deploy "$release_file"

password_file="$test_directory/basic-auth-password"
printf 'safe-smoke-password\n' >"$password_file"
chmod 600 "$password_file"

cat >"$test_directory/bin/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

if [[ " $* " == *' --config - '* ]]; then
  cat >/dev/null
elif [[ " $* " == *' --write-out '* ]]; then
  printf '401'
else
  printf 'ok\n'
fi
EOF
chmod +x "$test_directory/bin/curl"

PATH="$test_directory/bin:$PATH" \
  COMPOSE_PROJECT_NAME=ambient-project-must-not-win \
  DOCKER_LOG="$docker_log" \
  SMOKE_BASIC_AUTH_PASSWORD_FILE="$password_file" \
  "$repository_root/scripts/smoke-release.sh" "$release_file"

backup_directory="$test_directory/backups"
PATH="$test_directory/bin:$PATH" \
  COMPOSE_PROJECT_NAME=ambient-project-must-not-win \
  DOCKER_LOG="$docker_log" \
  "$repository_root/scripts/backup-postgres.sh" "$release_file" "$backup_directory"

grep -Fq 'compose -p trading-engine --env-file' "$docker_log" || {
  printf 'no Compose invocation used the pinned project name\n' >&2
  exit 1
}
[[ $(find "$backup_directory" -maxdepth 1 -type f -name '*.dump' | wc -l) == 1 ]] || {
  printf 'backup script did not create exactly one dump\n' >&2
  exit 1
}

printf 'Compose project regression passed\n'
