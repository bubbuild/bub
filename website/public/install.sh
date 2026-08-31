#!/usr/bin/env bash

set -euo pipefail

readonly UV_INSTALL_URL="https://astral.sh/uv/install.sh"
readonly BUB_PACKAGE="bub@latest"

say() {
    printf '%s\n' "$*"
}

fail() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

install_uv() {
    local uv_install_dir="$HOME/.local/bin"

    say "Installing uv..."
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf "$UV_INSTALL_URL" | env UV_INSTALL_DIR="$uv_install_dir" sh
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- "$UV_INSTALL_URL" | env UV_INSTALL_DIR="$uv_install_dir" sh
    else
        fail "curl or wget is required to install uv"
    fi

    UV_BIN="$uv_install_dir/uv"
    [[ -x "$UV_BIN" ]] || fail "uv was installed, but $UV_BIN is not executable"
}

main() {
    [[ -n "${HOME:-}" ]] || fail "HOME is not set"

    if command -v uv >/dev/null 2>&1; then
        UV_BIN="$(command -v uv)"
        say "Using uv at $UV_BIN"
    else
        install_uv
    fi

    say "Installing Bub..."
    "$UV_BIN" tool install "$BUB_PACKAGE"

    # Keep installation successful even when uv cannot identify a supported
    # shell profile. The final message still shows the executable directory.
    if ! "$UV_BIN" tool update-shell; then
        say "warning: uv could not update your shell profile" >&2
    fi

    local tool_bin
    tool_bin="$("$UV_BIN" tool dir --bin)"

    say
    say "Bub was installed successfully."
    say "Executable directory: $tool_bin"
    say "Restart your shell, then run: bub --help"
}

main "$@"
