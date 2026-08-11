#!/bin/sh
set -eu

load_secret() {
    secret_name="$1"
    eval "secret_file=\${${secret_name}_FILE:-}"
    if [ -z "$secret_file" ]; then
        return
    fi
    if [ ! -r "$secret_file" ]; then
        printf '%s\n' "Secret file for $secret_name is not readable: $secret_file" >&2
        exit 1
    fi
    secret_value="$(sed -e 's/[[:space:]]*$//' "$secret_file")"
    if [ -z "$secret_value" ]; then
        printf '%s\n' "Secret file for $secret_name is empty: $secret_file" >&2
        exit 1
    fi
    export "$secret_name=$secret_value"
}

load_secret PROVIDER_API_KEY
load_secret PROVIDERS
load_secret DATABASE_URL
load_secret REDIS_URL
load_secret RABBITMQ_URL

if [ "${RUN_DATABASE_MIGRATIONS:-true}" = "true" ]; then
  alembic upgrade head
fi

exec "$@"
