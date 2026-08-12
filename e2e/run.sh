#!/bin/sh
set -eu

repository=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
cd "$repository"

command=${1:-run}
case "$command" in
    check | down | run) ;;
    *)
        echo "command must be run, check, or down" >&2
        exit 2
        ;;
esac

export COMPOSE_PROJECT_NAME=bub-e2e

container_engine=${BUB_E2E_ENGINE:-}
if [ -z "$container_engine" ]; then
    if command -v docker >/dev/null 2>&1; then
        container_engine=docker
    elif command -v podman >/dev/null 2>&1; then
        container_engine=podman
    else
        echo "Docker or Podman with a Compose provider is required" >&2
        exit 1
    fi
fi

podman_proxy() {
    case "${1:-}" in
        http://127.0.0.1:*) printf 'http://10.0.2.2:%s' "${1#http://127.0.0.1:}" ;;
        https://127.0.0.1:*) printf 'https://10.0.2.2:%s' "${1#https://127.0.0.1:}" ;;
        http://localhost:*) printf 'http://10.0.2.2:%s' "${1#http://localhost:}" ;;
        https://localhost:*) printf 'https://10.0.2.2:%s' "${1#https://localhost:}" ;;
        *) printf '%s' "${1:-}" ;;
    esac
}

if [ "$container_engine" = podman ]; then
    BUB_E2E_CONTAINER_HTTP_PROXY=$(podman_proxy "${HTTP_PROXY:-${http_proxy:-}}")
    BUB_E2E_CONTAINER_HTTPS_PROXY=$(podman_proxy "${HTTPS_PROXY:-${https_proxy:-}}")
    BUB_E2E_CONTAINER_ALL_PROXY=
else
    BUB_E2E_CONTAINER_HTTP_PROXY=${HTTP_PROXY:-${http_proxy:-}}
    BUB_E2E_CONTAINER_HTTPS_PROXY=${HTTPS_PROXY:-${https_proxy:-}}
    BUB_E2E_CONTAINER_ALL_PROXY=${ALL_PROXY:-${all_proxy:-}}
fi
export BUB_E2E_CONTAINER_HTTP_PROXY BUB_E2E_CONTAINER_HTTPS_PROXY BUB_E2E_CONTAINER_ALL_PROXY

compose() {
    if [ "$container_engine" = podman ]; then
        case "${HTTP_PROXY:-${http_proxy:-}}" in
            http://127.0.0.1:* | https://127.0.0.1:* | http://localhost:* | https://localhost:*)
                env \
                    -u ALL_PROXY -u HTTPS_PROXY -u HTTP_PROXY \
                    -u all_proxy -u https_proxy -u http_proxy \
                    "$container_engine" compose -f e2e/compose.yaml -f e2e/compose.podman.yaml "$@"
                ;;
            *) "$container_engine" compose -f e2e/compose.yaml -f e2e/compose.podman.yaml "$@" ;;
        esac
    else
        case "${HTTP_PROXY:-${http_proxy:-}}" in
            http://127.0.0.1:* | https://127.0.0.1:* | http://localhost:* | https://localhost:*)
                env \
                    -u ALL_PROXY -u HTTPS_PROXY -u HTTP_PROXY \
                    -u all_proxy -u https_proxy -u http_proxy \
                    "$container_engine" compose -f e2e/compose.yaml "$@"
                ;;
            *) "$container_engine" compose -f e2e/compose.yaml "$@" ;;
        esac
    fi
}

if ! command -v "$container_engine" >/dev/null 2>&1; then
    echo "Container engine '$container_engine' was not found" >&2
    exit 1
fi

output=${BUB_E2E_OUTPUT:-"$repository/.bub-e2e/run"}
mkdir -p "$output"
BUB_E2E_OUTPUT=$(CDPATH= cd -- "$output" && pwd)
export BUB_E2E_OUTPUT

auth_path=${BUB_E2E_CODEX_AUTH:-${CODEX_HOME:-/nonexistent}/auth.json}
if [ -f "$auth_path" ]; then
    auth_directory=$(CDPATH= cd -- "$(dirname "$auth_path")" && pwd)
    BUB_E2E_CODEX_AUTH="$auth_directory/$(basename "$auth_path")"
else
    BUB_E2E_CODEX_AUTH=/dev/null
fi
export BUB_E2E_CODEX_AUTH

if [ "$command" = check ]; then
    compose config --quiet
    uv run --project e2e bub-e2e check --manifest e2e/cases
    exit
fi

if [ "$command" = down ]; then
    compose down --volumes --remove-orphans
    exit
fi

if [ -z "${GITHUB_SHA:-}" ]; then
    GITHUB_SHA=$(git rev-parse HEAD)
    export GITHUB_SHA
fi

compose up --detach --wait redis phoenix
compose build harness

set -- run --manifest e2e/cases --output /evidence
if [ -n "${BUB_E2E_IDS:-}" ]; then
    set -- "$@" --id "$BUB_E2E_IDS"
fi
if [ -n "${BUB_E2E_CATEGORIES:-}" ]; then
    set -- "$@" --category "$BUB_E2E_CATEGORIES"
fi
compose run --rm harness "$@"
