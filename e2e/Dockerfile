# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:0.10.12 AS uv
FROM docker.io/library/docker:27.5.1-dind AS docker

FROM python:3.12-slim-bookworm

COPY --from=uv /uv /uvx /bin/
COPY --from=docker /usr/local/bin/ /usr/local/bin/
COPY --from=docker /usr/local/libexec/docker/cli-plugins/ /usr/local/libexec/docker/cli-plugins/

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates git iproute2 iptables pigz socat \
    && rm -rf /var/lib/apt/lists/* \
    && docker compose version

ENV PATH="/workspace/bub/e2e/.venv/bin:$PATH" \
    BUB_E2E_REPOSITORY=/workspace/bub \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /workspace/bub
COPY . .

RUN --mount=type=cache,id=bub-e2e-uv,target=/root/.cache/uv \
    uv sync --project e2e --frozen --no-dev --no-editable

COPY --chmod=0755 e2e/container-entrypoint.sh /usr/local/bin/bub-e2e-container

ENTRYPOINT ["bub-e2e-container"]
