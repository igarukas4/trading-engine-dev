#!/usr/bin/env bash
set -euo pipefail

repository_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
dockerfile="$repository_root/frontend/Dockerfile"
dockerignore="$repository_root/frontend/.dockerignore"
workflow="$repository_root/.github/workflows/publish-frontend-image.yml"

for file in "$dockerfile" "$dockerignore" "$workflow"; do
  [[ -f "$file" ]] || { printf 'missing frontend image artifact: %s\n' "$file" >&2; exit 1; }
done

grep -Fq 'npm run build' "$dockerfile"
grep -Fq 'NEXT_PUBLIC_API_BASE_URL=' "$dockerfile"
grep -Fqx 'USER app' "$dockerfile"
grep -Fq 'CMD ["node", "server.js"]' "$dockerfile"
grep -Fqx 'node_modules/' "$dockerignore"
grep -Fq 'workflow_dispatch:' "$workflow"
grep -Fq 'packages: write' "$workflow"
grep -Fq 'docker/login-action@v3' "$workflow"
grep -Fq 'password: ${{ secrets.GITHUB_TOKEN }}' "$workflow"
grep -Fq 'NEXT_PUBLIC_API_BASE_URL=' "$workflow"
grep -Fq 'Dashboard' "$workflow"
grep -Fq 'provenance: mode=max' "$workflow"
grep -Fq 'sbom: true' "$workflow"
grep -Fq 'FRONTEND_IMAGE=%s@%s' "$workflow"
! grep -Eiq '(password|token):[[:space:]]*[^$[:space:]]+' "$workflow" || {
  printf 'workflow must not contain a literal registry credential\n' >&2
  exit 1
}

printf 'frontend image contract passed\n'
