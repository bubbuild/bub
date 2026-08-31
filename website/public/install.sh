#!/usr/bin/env bash

set -euo pipefail

readonly UV_INSTALL_URL="https://astral.sh/uv/install.sh"
readonly DEFAULT_PRESETS_URL="https://bub.build/presets.json"
readonly BUB_PACKAGE="bub"
readonly BUB_PYTHON="3.12"
readonly INQUIRER_PACKAGE="inquirer-textual==0.6.1"

UV_BIN=""
PRESET_FILE=""
RESOLUTION_FILE=""
INTERACTIVE=false
REQUESTED_PRESET=""
EXTRA_DEPENDENCIES=()
COLOR_RESET=""
COLOR_BOLD=""
COLOR_CYAN=""
COLOR_GREEN=""

configure_colors() {
    if [[ "$INTERACTIVE" == true && -z "${NO_COLOR:-}" && "${TERM:-}" != "dumb" ]]; then
        COLOR_RESET=$'\033[0m'
        COLOR_BOLD=$'\033[1m'
        COLOR_CYAN=$'\033[36m'
        COLOR_GREEN=$'\033[32m'
    fi
}

say() {
    printf '%s\n' "$*"
}

say_step() {
    if [[ "$INTERACTIVE" == true ]]; then
        printf '%s==>%s %s\n' "$COLOR_CYAN$COLOR_BOLD" "$COLOR_RESET" "$*"
    else
        say "$*"
    fi
}

fail() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

cleanup() {
    [[ -z "$PRESET_FILE" ]] || rm -f -- "$PRESET_FILE"
    [[ -z "$RESOLUTION_FILE" ]] || rm -f -- "$RESOLUTION_FILE"
}

show_help() {
    cat <<'EOF'
Install Bub and optional plugin presets.

Usage:
  install.sh [--preset PRESET] [--dependency SPEC]...

Options:
  --preset PRESET       Select a preset without prompting.
  --dependency SPEC     Install an extra plugin dependency. Repeatable.
  --plugin SPEC         Alias for --dependency.
  -h, --help            Show this help.

Examples:
  curl -fsSL https://bub.build/install.sh | bash
  curl -fsSL https://bub.build/install.sh | bash -s -- --preset recommended
  curl -fsSL https://bub.build/install.sh | bash -s -- --preset minimal --dependency bub-mcp@main
EOF
}

parse_args() {
    while (($#)); do
        case "$1" in
            --preset)
                (($# >= 2)) || fail "--preset requires a value"
                [[ -z "$REQUESTED_PRESET" ]] || fail "--preset may only be specified once"
                REQUESTED_PRESET=$2
                shift 2
                ;;
            --preset=*)
                [[ -z "$REQUESTED_PRESET" ]] || fail "--preset may only be specified once"
                REQUESTED_PRESET=${1#*=}
                [[ -n "$REQUESTED_PRESET" ]] || fail "--preset requires a value"
                shift
                ;;
            --dependency|--plugin)
                (($# >= 2)) || fail "$1 requires a value"
                EXTRA_DEPENDENCIES+=("$2")
                shift 2
                ;;
            --dependency=*|--plugin=*)
                local dependency=${1#*=}
                [[ -n "$dependency" ]] || fail "${1%%=*} requires a value"
                EXTRA_DEPENDENCIES+=("$dependency")
                shift
                ;;
            -h|--help)
                show_help
                exit 0
                ;;
            --)
                shift
                (($# == 0)) || fail "unexpected positional argument: $1"
                ;;
            *)
                fail "unknown argument: $1"
                ;;
        esac
    done
}

detect_interactive_mode() {
    if [[ -n "$REQUESTED_PRESET" ]]; then
        INTERACTIVE=false
        return
    fi

    if [[ -t 1 && -e /dev/tty ]]; then
        INTERACTIVE=true
        return
    fi

    fail "no interactive terminal is available; pass --preset PRESET (for example: bash -s -- --preset recommended)"
}

install_uv() {
    local uv_install_dir="$HOME/.local/bin"

    say_step "Installing uv"
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

link_bub_executable() {
    local source=$1
    local destination=$2

    mkdir -p "$(dirname "$destination")"
    if [[ -e "$destination" || -L "$destination" ]]; then
        rm -f -- "$destination"
    fi
    ln -s "$source" "$destination"
}

download_presets() {
    local destination=$1
    local presets_url=${BUB_INSTALLER_PRESETS_URL:-$DEFAULT_PRESETS_URL}

    say_step "Loading plugin presets"
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf "$presets_url" -o "$destination"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$destination" "$presets_url"
    else
        fail "curl or wget is required to download plugin presets"
    fi
}

resolve_preset() {
    local catalog=$1
    local resolution=$2
    local mode=noninteractive
    local uv_args=(run --no-project)
    [[ "$INTERACTIVE" == true ]] && mode=interactive
    [[ "$INTERACTIVE" == true ]] && uv_args+=(--with "$INQUIRER_PACKAGE")

    "$UV_BIN" "${uv_args[@]}" python - \
        "$catalog" "$mode" "$REQUESTED_PRESET" "$resolution" "${EXTRA_DEPENDENCIES[@]}" <<'PY'
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path

NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def abort(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def validate_dependency(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        abort(f"{context} must be a non-empty string")
    if value.startswith("-") or any(character in value for character in "\r\n\0"):
        abort(f"{context} is not a safe package specification: {value!r}")
    return value


def load_catalog(path: str) -> list[dict[str, object]]:  # noqa: C901
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        abort(f"could not read preset catalog: {error}")

    if not isinstance(data, dict) or data.get("schema_version") != 1:
        abort("preset catalog must use schema_version 1")
    presets = data.get("presets")
    if not isinstance(presets, list) or not presets:
        abort("preset catalog must contain a non-empty presets list")

    names: set[str] = set()
    default_count = 0
    for index, preset in enumerate(presets):
        context = f"presets[{index}]"
        if not isinstance(preset, dict):
            abort(f"{context} must be an object")
        name = preset.get("name")
        if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
            abort(f"{context}.name must be a lowercase kebab-case string")
        if name in names:
            abort(f"preset name is duplicated: {name}")
        names.add(name)
        for field in ("title", "description"):
            if not isinstance(preset.get(field), str) or not preset[field]:
                abort(f"{context}.{field} must be a non-empty string")
        dependencies = preset.get("dependencies")
        if not isinstance(dependencies, list):
            abort(f"{context}.dependencies must be a list")
        for dependency_index, dependency in enumerate(dependencies):
            validate_dependency(dependency, f"{context}.dependencies[{dependency_index}]")
        if not isinstance(preset.get("default"), bool):
            abort(f"{context}.default must be a boolean")
        default_count += int(preset["default"])

    if default_count != 1:
        abort("preset catalog must contain exactly one default preset")
    return presets


def attach_terminal() -> None:
    if os.name == "nt":
        input_stream = open("CONIN$", encoding="utf-8")  # noqa: SIM115
        output_stream = open("CONOUT$", "w", encoding="utf-8", buffering=1)  # noqa: SIM115
    else:
        input_stream = open("/dev/tty", encoding="utf-8")  # noqa: SIM115
        output_stream = open("/dev/tty", "w", encoding="utf-8", buffering=1)  # noqa: SIM115

    # Textual reads the original standard streams directly.
    sys.stdin = sys.__stdin__ = input_stream
    sys.stdout = sys.__stdout__ = output_stream
    sys.stderr = sys.__stderr__ = output_stream


def choose_preset(presets: list[dict[str, object]]) -> tuple[dict[str, object], list[str]]:
    attach_terminal()

    from inquirer_textual import prompts
    from inquirer_textual.common.Choice import Choice
    from inquirer_textual.common.PromptSettings import PromptSettings

    choices: list[Choice] = []
    default_choice: Choice | None = None
    for preset in presets:
        choice = Choice(f"{preset['title']} — {preset['description']}", data=preset["name"])
        choices.append(choice)
        if preset["default"]:
            default_choice = choice

    settings = PromptSettings(mandatory=True, mouse=True)
    answer = prompts.select("Choose a Bub plugin preset:", choices, default=default_choice, settings=settings).value
    if not isinstance(answer, Choice) or not isinstance(answer.data, str):
        abort("no preset was selected")
    selected = next(preset for preset in presets if preset["name"] == answer.data)

    extra_answer = prompts.text(
        "Additional plugins (space-separated, optional):",
        settings=PromptSettings(mouse=True),
    ).value
    try:
        extras = shlex.split(extra_answer or "")
    except ValueError as error:
        abort(f"invalid plugin list: {error}")
    return selected, extras


def main() -> None:
    catalog_path, mode, requested_preset, resolution_path, *extras = sys.argv[1:]
    presets = load_catalog(catalog_path)
    if mode == "interactive":
        selected, prompted_extras = choose_preset(presets)
        extras.extend(prompted_extras)
    else:
        selected = next((preset for preset in presets if preset["name"] == requested_preset), None)
        if selected is None:
            available = ", ".join(str(preset["name"]) for preset in presets)
            abort(f"unknown preset {requested_preset!r}; available presets: {available}")

    dependencies: list[str] = []
    for index, dependency in enumerate([*selected["dependencies"], *extras]):
        value = validate_dependency(dependency, f"dependency[{index}]")
        if value not in dependencies:
            dependencies.append(value)

    resolution = "\n".join([str(selected["name"]), *dependencies]) + "\n"
    try:
        Path(resolution_path).write_text(resolution, encoding="utf-8")
    except OSError as error:
        abort(f"could not write preset resolution: {error}")


if __name__ == "__main__":
    main()
PY
}

main() {
    parse_args "$@"
    detect_interactive_mode
    configure_colors

    [[ -n "${HOME:-}" ]] || fail "HOME is not set"

    if command -v uv >/dev/null 2>&1; then
        UV_BIN="$(command -v uv)"
        say_step "Using uv at $UV_BIN"
    else
        install_uv
    fi

    PRESET_FILE="$(mktemp "${TMPDIR:-/tmp}/bub-presets.XXXXXX")"
    RESOLUTION_FILE="$(mktemp "${TMPDIR:-/tmp}/bub-resolution.XXXXXX")"
    trap cleanup EXIT
    download_presets "$PRESET_FILE"

    if ! resolve_preset "$PRESET_FILE" "$RESOLUTION_FILE"; then
        fail "failed to resolve Bub preset"
    fi

    local resolved_lines=()
    mapfile -t resolved_lines <"$RESOLUTION_FILE"
    ((${#resolved_lines[@]} >= 1)) || fail "preset resolver returned no selection"
    local selected_preset=${resolved_lines[0]}
    local dependencies=("${resolved_lines[@]:1}")

    local bub_root="$HOME/.bub"
    local venv_dir="$bub_root/.venv"
    local venv_python="$venv_dir/bin/python"
    local venv_bub="$venv_dir/bin/bub"
    local executable_dir="$HOME/.local/bin"
    local bub_bin="$executable_dir/bub"

    say_step "Creating Bub virtual environment"
    mkdir -p "$bub_root"
    "$UV_BIN" venv --python "$BUB_PYTHON" --allow-existing "$venv_dir"

    say_step "Installing Bub"
    "$UV_BIN" pip install --python "$venv_python" --upgrade "$BUB_PACKAGE"
    [[ -x "$venv_bub" ]] || fail "Bub was installed, but $venv_bub is not executable"
    link_bub_executable "$venv_bub" "$bub_bin"

    if ((${#dependencies[@]})); then
        say_step "Installing plugins for preset $selected_preset"
        "$bub_bin" install -- "${dependencies[@]}"
    fi

    if [[ "$INTERACTIVE" == true ]]; then
        say_step "Starting Bub onboarding"
        "$bub_bin" onboard </dev/tty >/dev/tty
    fi

    say
    if [[ "$INTERACTIVE" == true ]]; then
        printf '%sBub was installed successfully.%s\n' "$COLOR_GREEN$COLOR_BOLD" "$COLOR_RESET"
    else
        say "Bub was installed successfully."
    fi
    say "Preset: $selected_preset"
    say "Virtual environment: $venv_dir"
    say "Executable link: $bub_bin"
    say "Ensure $executable_dir is on PATH, then run: bub --help"
}

main "$@"
