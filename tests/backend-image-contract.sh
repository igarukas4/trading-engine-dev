#!/usr/bin/env bash
set -euo pipefail

repository_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
dockerfile="$repository_root/backend/Dockerfile"
dockerignore="$repository_root/backend/.dockerignore"
workflow="$repository_root/.github/workflows/publish-backend-image.yml"

for file in "$dockerfile" "$dockerignore" "$workflow"; do
  [[ -f "$file" ]] || { printf 'missing backend image artifact: %s\n' "$file" >&2; exit 1; }
done

grep -Fqx 'USER app' "$dockerfile"
grep -Fq 'app.main:app' "$dockerfile"
grep -Fq -- '--port", "8000"' "$dockerfile"
grep -Fqx '__pycache__/' "$dockerignore"
grep -Fq 'workflow_dispatch:' "$workflow"
grep -Fq 'packages: write' "$workflow"
grep -Fq 'docker/login-action@v3' "$workflow"
grep -Fq 'password: ${{ secrets.GITHUB_TOKEN }}' "$workflow"
grep -Fq 'GET /health/live' "$workflow" || grep -Fq '/health/live' "$workflow"
grep -Fq 'provenance: mode=max' "$workflow"
grep -Fq 'sbom: true' "$workflow"
grep -Fq 'BACKEND_IMAGE=%s@%s' "$workflow"
! grep -Eiq '(password|token):[[:space:]]*[^$[:space:]]+' "$workflow" || {
  printf 'workflow must not contain a literal registry credential\n' >&2
  exit 1
}

printf 'backend image contract passed\n'
