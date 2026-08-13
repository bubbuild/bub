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

bridge_proxy() {
    proxy_url=$1
    listen_port=$2
    proxy_address=${proxy_url#*://}
    proxy_address=${proxy_address%%/*}
    proxy_host=${proxy_address%:*}
    proxy_port=${proxy_address##*:}
    socat "TCP-LISTEN:$listen_port,bind=0.0.0.0,fork,reuseaddr" "TCP:$proxy_host:$proxy_port" \
        >/dev/null 2>&1 &
}

daemon_http_proxy=${HTTP_PROXY:-${http_proxy:-}}
daemon_https_proxy=${HTTPS_PROXY:-${https_proxy:-}}
daemon_all_proxy=${ALL_PROXY:-${all_proxy:-}}

HTTP_PROXY=$daemon_http_proxy HTTPS_PROXY=$daemon_https_proxy ALL_PROXY=$daemon_all_proxy dockerd \
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

docker_gateway=$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')

if [ -n "$daemon_http_proxy" ]; then
    bridge_proxy "$daemon_http_proxy" 18080
    proxy_scheme=${daemon_http_proxy%%://*}
    HTTP_PROXY="$proxy_scheme://$docker_gateway:18080"
    http_proxy=$HTTP_PROXY
    export HTTP_PROXY http_proxy
fi
if [ -n "$daemon_https_proxy" ]; then
    bridge_proxy "$daemon_https_proxy" 18081
    proxy_scheme=${daemon_https_proxy%%://*}
    HTTPS_PROXY="$proxy_scheme://$docker_gateway:18081"
    https_proxy=$HTTPS_PROXY
    export HTTPS_PROXY https_proxy
fi
if is_loopback_proxy "$daemon_all_proxy"; then
    unset ALL_PROXY all_proxy
fi

if [ -n "${HTTP_PROXY:-}${HTTPS_PROXY:-}" ]; then
    mkdir -p /root/.docker
    cat > /root/.docker/config.json <<EOF
{
  "proxies": {
    "default": {
      "httpProxy": "${HTTP_PROXY:-}",
      "httpsProxy": "${HTTPS_PROXY:-}",
      "noProxy": "${NO_PROXY:-${no_proxy:-}}"
    }
  }
}
EOF
fi

service_bridge_host=${BUB_E2E_SERVICE_BRIDGE_HOST:-}
if [ -n "$service_bridge_host" ]; then
    socat TCP-LISTEN:6379,bind=0.0.0.0,fork,reuseaddr "TCP:$service_bridge_host:${BUB_E2E_REDIS_PORT:-16379}" &
    socat TCP-LISTEN:6006,bind=0.0.0.0,fork,reuseaddr "TCP:$service_bridge_host:${BUB_E2E_PHOENIX_PORT:-6006}" &
else
    socat TCP-LISTEN:6379,bind=0.0.0.0,fork,reuseaddr TCP:redis:6379 &
    socat TCP-LISTEN:6006,bind=0.0.0.0,fork,reuseaddr TCP:phoenix:6006 &
fi

exec bub-e2e "$@"
