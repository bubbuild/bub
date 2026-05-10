FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# Update system and install base dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    build-essential \
    git \
    curl \
    wget \
    vim \
    sudo \
    procps \
    openssh-client \
    ca-certificates \
    tini

# Install development tools
RUN apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    nodejs \
    npm \
    golang \
    jq \
    htop \
    tree \
    unzip \
    protobuf-compiler \
    zip fd-find gh

# Install pnpm and OpenAI Codex CLI
RUN npm install -g pnpm && npm install -g @openai/codex

WORKDIR /app
# Install Python dependencies first (cached layer)
COPY pyproject.toml uv.lock README.md LICENSE entrypoint.sh ./
# Copy the full source and install
COPY src ./src
RUN uv sync --no-dev --no-editable && uv pip install "any-llm-sdk[gemini,xai]" && \
    chmod +x /app/entrypoint.sh

WORKDIR /workspace

VOLUME /root/.bub

ENTRYPOINT ["/usr/bin/tini", "--"]

CMD ["/app/entrypoint.sh"]
