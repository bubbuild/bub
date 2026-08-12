#!/bin/sh
set -eu

docker_log=/tmp/bub-e2e-dockerd.log
storage_driver=${BUB_E2E_DOCKER_STORAGE_DRIVER:-vfs}

is_loopback_proxy() {
    case "${1:-}" in
        http://127.0.0.1:* | https://127.0.0.1:* | http://localhost:* | https://localhost:*) return 0 ;;
        *) return 1 ;;
    esac
}

if is_loopback_proxy "${HTTP_PROXY:-}"; then unset HTTP_PROXY; fi
if is_loopback_proxy "${HTTPS_PROXY:-}"; then unset HTTPS_PROXY; fi
if is_loopback_proxy "${ALL_PROXY:-}"; then unset ALL_PROXY; fi
if is_loopback_proxy "${http_proxy:-}"; then unset http_proxy; fi
if is_loopback_proxy "${https_proxy:-}"; then unset https_proxy; fi
if is_loopback_proxy "${all_proxy:-}"; then unset all_proxy; fi

dockerd \
    --host=unix:///var/run/docker.sock \
    --storage-driver="$storage_driver" \
    --log-level=error \
    >"$docker_log" 2>&1 &

attempt=0
until docker info >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 60 ]; then
        echo "The nested Docker daemon did not become ready." >&2
        sed -n '1,160p' "$docker_log" >&2
        exit 1
    fi
    sleep 1
done

socat TCP-LISTEN:6379,bind=0.0.0.0,fork,reuseaddr TCP:redis:6379 &
socat TCP-LISTEN:6006,bind=0.0.0.0,fork,reuseaddr TCP:phoenix:6006 &

exec bub-e2e "$@"
