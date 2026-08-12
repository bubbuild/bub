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

compose="docker compose -f e2e/compose.yaml"
export COMPOSE_PROJECT_NAME=bub-e2e

if [ "$command" = check ]; then
    $compose config --quiet
    uv run --project e2e bub-e2e check --manifest e2e/cases
    exit
fi

if [ "$command" = down ]; then
    $compose down --volumes --remove-orphans
    exit
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

if [ -z "${GITHUB_SHA:-}" ]; then
    GITHUB_SHA=$(git rev-parse HEAD)
    export GITHUB_SHA
fi

$compose up --detach --wait redis phoenix
$compose build harness

set -- run --manifest e2e/cases --output /evidence
if [ -n "${BUB_E2E_IDS:-}" ]; then
    set -- "$@" --id "$BUB_E2E_IDS"
fi
if [ -n "${BUB_E2E_CATEGORIES:-}" ]; then
    set -- "$@" --category "$BUB_E2E_CATEGORIES"
fi
$compose run --rm harness "$@"
