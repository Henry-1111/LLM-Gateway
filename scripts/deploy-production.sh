#!/bin/sh
set -eu

env_file="${1:-.env.production}"

compose() {
    docker compose \
        --env-file "$env_file" \
        -f compose.yaml \
        -f compose.production.yaml \
        "$@"
}

./scripts/check-production-env.sh "$env_file"

# Pull before touching running services so a registry failure cannot interrupt them.
compose pull

# Start stateful dependencies, then run exactly one migration job.
compose up -d --wait postgres redis rabbitmq
compose run --rm gateway alembic upgrade head

# Reconcile the application, monitoring, and TLS proxy after migrations succeed.
compose up -d --wait --remove-orphans
compose exec -T gateway python -c \
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=5)"
compose ps

printf '%s\n' "Deployment completed: https://$(sed -n 's/^DOMAIN=//p' "$env_file" | tail -n 1)"
