#!/usr/bin/env bash
set -euo pipefail

repository_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
test_directory=$(mktemp -d)
trap 'rm -rf "$test_directory"' EXIT

release_file="$test_directory/release.env"
password_file="$test_directory/basic-auth-password"
mkdir -p "$test_directory/bin"
printf '%s\n' 'DOMAIN=dashboard.example.com' 'CADDY_BASIC_AUTH_USER=operator' >"$release_file"
printf '%s\n' 'safe-smoke-password' >"$password_file"
chmod 600 "$password_file"

cat >"$test_directory/bin/curl" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$CURL_LOG"
if [[ " $* " == *' --config - '* ]]; then
  cat >"$CURL_CONFIG"
  printf 'Dashboard\n'
elif [[ " $* " == *' --write-out '* ]]; then
  if [[ -s ${CURL_STATUS_SEQUENCE:-} ]]; then
    status=$(head -n 1 "$CURL_STATUS_SEQUENCE")
    tail -n +2 "$CURL_STATUS_SEQUENCE" >"${CURL_STATUS_SEQUENCE}.next"
    mv "${CURL_STATUS_SEQUENCE}.next" "$CURL_STATUS_SEQUENCE"
    printf '%s' "$status"
  else
    printf '401'
  fi
else
  printf 'ok\n'
fi
EOF
chmod +x "$test_directory/bin/curl"

cat >"$test_directory/bin/docker" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
chmod +x "$test_directory/bin/docker"

status_sequence="$test_directory/status-sequence"
printf '%s\n' 503 401 >"$status_sequence"

if PATH="$test_directory/bin:$PATH" CURL_LOG="$test_directory/curl.log" CURL_CONFIG="$test_directory/curl.config" "$repository_root/scripts/smoke-release.sh" "$release_file"; then
  printf 'expected smoke check without protected password file to fail\n' >&2
  exit 1
fi

PATH="$test_directory/bin:$PATH" CURL_LOG="$test_directory/curl.log" CURL_CONFIG="$test_directory/curl.config" CURL_STATUS_SEQUENCE="$status_sequence" SMOKE_BASIC_AUTH_PASSWORD_FILE="$password_file" "$repository_root/scripts/smoke-release.sh" "$release_file"
! grep -Fq 'safe-smoke-password' "$test_directory/curl.log" || { printf 'smoke password appeared in curl arguments\n' >&2; exit 1; }
grep -Fxq 'user = "operator:safe-smoke-password"' "$test_directory/curl.config" || { printf 'smoke password was not supplied through curl stdin configuration\n' >&2; exit 1; }
[[ ! -s "$status_sequence" ]] || { printf 'smoke check did not retry Caddy readiness after a 503\n' >&2; exit 1; }
for header in \
  'X-Authenticated-User: forged-smoke-actor' \
  'X-Forwarded-For: 198.51.100.23' \
  'X-Forwarded-Host: forged.example' \
  'X-Forwarded-Proto: http'; do
  grep -Fq -- "--header $header" "$test_directory/curl.log" || { printf 'forged header smoke check missing: %s\n' "$header" >&2; exit 1; }
done
printf 'smoke release regression passed\n'
