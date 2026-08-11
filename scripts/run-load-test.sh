#!/bin/sh
set -eu

compose_files="-f compose.yaml -f compose.loadtest.yaml"

docker compose $compose_files up --build -d gateway prometheus grafana

tenant_output="$(docker compose $compose_files exec -T gateway \
  llm-gateway create-tenant --name "Load test $(date -u +%Y%m%dT%H%M%SZ)" \
  --token-quota-limit 1000000000)"
tenant_id="$(printf '%s\n' "$tenant_output" | sed -n 's/^tenant_id=//p')"

if [ -z "$tenant_id" ]; then
  printf '%s\n' "Could not create load-test tenant" >&2
  exit 1
fi

key_output="$(docker compose $compose_files exec -T gateway \
  llm-gateway create-key --tenant-id "$tenant_id" --name k6)"
api_key="$(printf '%s\n' "$key_output" | sed -n 's/^api_key=//p')"

if [ -z "$api_key" ]; then
  printf '%s\n' "Could not create load-test API key" >&2
  exit 1
fi

printf '%s\n' "Running k6 against tenant $tenant_id"
docker compose $compose_files run --rm --no-deps \
  -e API_KEY="$api_key" \
  -e VUS="${VUS:-10}" \
  -e DURATION="${DURATION:-30s}" \
  -e THINK_TIME_SECONDS="${THINK_TIME_SECONDS:-0.1}" \
  -e P95_THRESHOLD="${P95_THRESHOLD:-p(95)<1000}" \
  -e ERROR_THRESHOLD="${ERROR_THRESHOLD:-rate<0.01}" \
  k6 run /scripts/k6.js
