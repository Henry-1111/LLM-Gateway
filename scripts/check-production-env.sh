#!/bin/sh
set -eu

env_file="${1:-.env.production}"

if [ ! -r "$env_file" ]; then
    printf '%s\n' "Production environment file is not readable: $env_file" >&2
    exit 1
fi

value_of() {
    sed -n "s/^$1=//p" "$env_file" | tail -n 1
}

require_value() {
    key="$1"
    value="$(value_of "$key")"
    if [ -z "$value" ]; then
        printf '%s\n' "Missing required production setting: $key" >&2
        exit 1
    fi
    case "$value" in
        *replace-with*|*example.com*)
            printf '%s\n' "Production setting still contains a placeholder: $key" >&2
            exit 1
            ;;
    esac
}

private_mode() {
    target="$1"
    if stat -f '%Lp' "$target" >/dev/null 2>&1; then
        stat -f '%Lp' "$target"
    else
        stat -c '%a' "$target"
    fi
}

require_private_file() {
    target="$1"
    label="$2"
    mode="$(private_mode "$target")"
    case "$mode" in
        400|600) ;;
        *)
            printf '%s\n' "$label must have mode 400 or 600, found $mode: $target" >&2
            exit 1
            ;;
    esac
}

for key in \
    DOMAIN ACME_EMAIL GATEWAY_IMAGE POSTGRES_PASSWORD RABBITMQ_PASSWORD \
    GRAFANA_ADMIN_USER PROVIDER_API_KEY_FILE DATABASE_URL_FILE REDIS_URL_FILE \
    RABBITMQ_URL_FILE POSTGRES_PASSWORD_FILE GRAFANA_ADMIN_PASSWORD_FILE
do
    require_value "$key"
done

case "$(value_of GATEWAY_IMAGE)" in
    *:latest)
        printf '%s\n' "GATEWAY_IMAGE must use an immutable version tag or digest, not latest" >&2
        exit 1
        ;;
esac

require_private_file "$env_file" "Production environment file"

for key in \
    PROVIDER_API_KEY_FILE DATABASE_URL_FILE REDIS_URL_FILE RABBITMQ_URL_FILE \
    POSTGRES_PASSWORD_FILE GRAFANA_ADMIN_PASSWORD_FILE
do
    secret_file="$(value_of "$key")"
    if [ ! -s "$secret_file" ]; then
        printf '%s\n' "Secret file is missing or empty: $secret_file ($key)" >&2
        exit 1
    fi
    require_private_file "$secret_file" "Secret file"
done

docker compose \
    --env-file "$env_file" \
    -f compose.yaml \
    -f compose.production.yaml \
    config --quiet

printf '%s\n' "Production preflight passed"
