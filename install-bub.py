#!/usr/bin/env python3
"""Install Bub with one surface preset and surface-independent plugins.

This file intentionally uses only the Python standard library so it can run
before Bub and its dependencies are installed.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import venv
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TextIO

MINIMUM_PYTHON = (3, 12)
DEFAULT_PYTHON = "3.12"


class InstallerError(Exception):
    """Raised for an installation request that cannot be completed."""


@dataclass(frozen=True)
class PluginOption:
    """A curated, surface-independent plugin shown by the installer."""

    name: str
    requirement: str
    description: str


@dataclass(frozen=True)
class Preset:
    """Packages required by one Bub execution surface."""

    name: str
    description: str
    required: tuple[str, ...] = ()


@dataclass(frozen=True)
class InstallPlan:
    """A complete, reproducible Bub tool installation request."""

    preset: str
    bub_requirement: str
    python: str
    required_plugins: tuple[str, ...]
    optional_plugins: tuple[str, ...]
    extra_requirements: tuple[str, ...]

    @property
    def injected_requirements(self) -> tuple[str, ...]:
        # Bub's post-install plugin commands invoke uv from the tool environment.
        return _unique(("uv", *self.required_plugins, *self.optional_plugins, *self.extra_requirements))

    def command(self, uv_executable: str = "uv") -> tuple[str, ...]:
        command = [uv_executable, "tool", "install", "--force", "--python", self.python]
        for requirement in self.injected_requirements:
            command.extend(("--with", requirement))
        command.append(self.bub_requirement)
        return tuple(command)


PRESETS = (
    Preset("chat", "Bub's terminal chat and one-shot commands"),
    Preset("lody", "Bub as an ACP agent in Lody", required=("bub-acp-server",)),
)

OPTIONAL_PLUGINS = (
    PluginOption("web-search", "bub-web-search", "Provider-selectable web search tools"),
    PluginOption("mcp-tools", "bub-mcp", "Expose configured MCP servers as Bub tools"),
    PluginOption("semantic-memory", "bub-semantic-memory", "Semantic memory across conversations"),
)


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def preset_by_name(name: str) -> Preset:
    try:
        return next(preset for preset in PRESETS if preset.name == name)
    except StopIteration as exc:
        choices = ", ".join(preset.name for preset in PRESETS)
        raise InstallerError(f"Unknown preset {name!r}; choose one of: {choices}") from exc


def plugin_by_name(name: str) -> PluginOption:
    try:
        return next(plugin for plugin in OPTIONAL_PLUGINS if plugin.name == name)
    except StopIteration as exc:
        choices = ", ".join(plugin.name for plugin in OPTIONAL_PLUGINS)
        raise InstallerError(f"Unknown optional plugin {name!r}; choose one of: {choices}") from exc


def build_plan(
    preset_name: str,
    optional_plugin_names: Sequence[str] = (),
    extra_requirements: Sequence[str] = (),
    *,
    bub_requirement: str = "bub",
    python: str = DEFAULT_PYTHON,
) -> InstallPlan:
    preset = preset_by_name(preset_name)
    selected_options = tuple(plugin_by_name(name) for name in _unique(optional_plugin_names))
    return InstallPlan(
        preset=preset.name,
        bub_requirement=bub_requirement,
        python=python,
        required_plugins=preset.required,
        optional_plugins=tuple(option.requirement for option in selected_options),
        extra_requirements=_unique(extra_requirements),
    )


def _read_answer(prompt: str, input_stream: TextIO, output_stream: TextIO) -> str:
    output_stream.write(prompt)
    output_stream.flush()
    answer = input_stream.readline()
    if answer == "":
        raise InstallerError("Interactive input is unavailable; use --no-interactive with --preset.")
    return answer.strip()


def choose_preset(input_stream: TextIO, output_stream: TextIO) -> str:
    output_stream.write("Choose how this Bub installation will be used:\n")
    for index, preset in enumerate(PRESETS, start=1):
        output_stream.write(f"  {index}. {preset.name:<8} {preset.description}\n")

    while True:
        answer = _read_answer("Preset [1]: ", input_stream, output_stream) or "1"
        try:
            index = int(answer) - 1
        except ValueError:
            index = -1
        if 0 <= index < len(PRESETS):
            return PRESETS[index].name
        else:
            output_stream.write("Enter one of the listed numbers.\n")


def toggle_optional_plugins(
    selected_names: Sequence[str], input_stream: TextIO, output_stream: TextIO
) -> tuple[str, ...]:
    selected = set(selected_names)
    for name in selected:
        plugin_by_name(name)

    while True:
        output_stream.write("\nOptional plugins (shared by every preset):\n")
        for index, plugin in enumerate(OPTIONAL_PLUGINS, start=1):
            marker = "x" if plugin.name in selected else " "
            output_stream.write(f"  [{marker}] {index}. {plugin.name:<16} {plugin.description}\n")
        answer = _read_answer("Toggle numbers, or press Enter to continue: ", input_stream, output_stream)
        if not answer:
            return tuple(plugin.name for plugin in OPTIONAL_PLUGINS if plugin.name in selected)

        try:
            indexes = _parse_toggle_indexes(answer)
        except InstallerError:
            output_stream.write("Enter comma-separated numbers from the list.\n")
            continue

        for index in indexes:
            name = OPTIONAL_PLUGINS[index].name
            if name in selected:
                selected.remove(name)
            else:
                selected.add(name)


def _parse_toggle_indexes(answer: str) -> set[int]:
    try:
        indexes = {int(item.strip()) - 1 for item in answer.split(",")}
    except ValueError as exc:
        raise InstallerError("Plugin selections must be numbers.") from exc
    if any(index < 0 or index >= len(OPTIONAL_PLUGINS) for index in indexes):
        raise InstallerError("Plugin selection is outside the available range.")
    return indexes


@contextlib.contextmanager
def _terminal_input() -> Iterator[TextIO]:
    if sys.stdin.isatty():
        yield sys.stdin
        return

    terminal_name = "CONIN$" if os.name == "nt" else "/dev/tty"
    try:
        with Path(terminal_name).open(encoding="utf-8") as terminal:
            yield terminal
    except OSError as exc:
        raise InstallerError("No terminal is available; use --no-interactive with --preset.") from exc


def _bootstrap_uv(bootstrap_root: Path) -> Path:
    environment = bootstrap_root / "uv-bootstrap"
    try:
        venv.EnvBuilder(with_pip=True).create(environment)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise InstallerError("uv is not installed and a temporary bootstrap environment could not be created.") from exc

    scripts = environment / ("Scripts" if os.name == "nt" else "bin")
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    uv = scripts / ("uv.exe" if os.name == "nt" else "uv")
    subprocess.run([os.fspath(python), "-m", "pip", "install", "uv"], check=True)
    return uv


@contextlib.contextmanager
def resolve_uv(requested: str | None = None) -> Iterator[str]:
    if requested:
        resolved = shutil.which(requested) if Path(requested).name == requested else requested
        if not resolved or not Path(resolved).is_file():
            raise InstallerError(f"uv executable not found: {requested}")
        yield resolved
        return

    if installed := shutil.which("uv"):
        yield installed
        return

    print("uv was not found; bootstrapping it in a temporary environment.")
    with tempfile.TemporaryDirectory(prefix="bub-installer-") as temporary_directory:
        yield os.fspath(_bootstrap_uv(Path(temporary_directory)))


def default_manifest_path() -> Path:
    bub_home = os.getenv("BUB_HOME")
    return Path(bub_home).expanduser() / "install.json" if bub_home else Path.home() / ".bub" / "install.json"


def write_manifest(plan: InstallPlan, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": 1, **asdict(plan)}
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temporary_file:
        json.dump(payload, temporary_file, indent=2)
        temporary_file.write("\n")
        temporary_path = Path(temporary_file.name)
    temporary_path.replace(path)


def _confirm(plan: InstallPlan, input_stream: TextIO, output_stream: TextIO) -> bool:
    output_stream.write("\nInstallation plan:\n")
    output_stream.write(f"  preset:  {plan.preset}\n")
    output_stream.write(f"  Bub:     {plan.bub_requirement}\n")
    output_stream.write(f"  plugins: {', '.join(plan.injected_requirements[1:]) or '(none)'}\n")
    answer = _read_answer("Install now? [Y/n]: ", input_stream, output_stream).lower()
    return answer in {"", "y", "yes"}


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preset", choices=[preset.name for preset in PRESETS])
    parser.add_argument(
        "--plugin",
        action="append",
        default=[],
        choices=[plugin.name for plugin in OPTIONAL_PLUGINS],
        help="Enable a curated surface-independent plugin; may be repeated.",
    )
    parser.add_argument(
        "--with",
        dest="extra_requirements",
        action="append",
        default=[],
        metavar="REQUIREMENT",
        help="Inject an additional package requirement; may be repeated.",
    )
    parser.add_argument("--bub-requirement", default=os.getenv("BUB_INSTALL_REQUIREMENT", "bub"))
    parser.add_argument("--python", default=DEFAULT_PYTHON, help="Python request passed to uv.")
    parser.add_argument("--uv", help="Path or command name for uv.")
    parser.add_argument("--manifest", type=Path, default=default_manifest_path())
    parser.add_argument("--no-interactive", action="store_true")
    parser.add_argument("--yes", action="store_true", help="Apply the plan without confirmation.")
    parser.add_argument("--dry-run", action="store_true", help="Print the uv command without installing.")
    return parser


def run(
    argv: Sequence[str] | None = None,
    *,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> int:
    args = create_parser().parse_args(argv)
    preset_name = args.preset or "chat"
    selected_plugins = tuple(args.plugin)

    if not args.no_interactive:
        with _terminal_input() as input_stream:
            if args.preset is None:
                preset_name = choose_preset(input_stream, sys.stdout)
            selected_plugins = toggle_optional_plugins(selected_plugins, input_stream, sys.stdout)
            plan = build_plan(
                preset_name,
                selected_plugins,
                args.extra_requirements,
                bub_requirement=args.bub_requirement,
                python=args.python,
            )
            if not args.yes and not _confirm(plan, input_stream, sys.stdout):
                print("Installation cancelled.")
                return 0
    else:
        plan = build_plan(
            preset_name,
            selected_plugins,
            args.extra_requirements,
            bub_requirement=args.bub_requirement,
            python=args.python,
        )

    if args.dry_run:
        print(shlex.join(plan.command(args.uv or "uv")))
        return 0

    with resolve_uv(args.uv) as uv_executable:
        command = plan.command(uv_executable)
        print(f"Running: {shlex.join(command)}")
        run_command(command, check=True, text=True)
        bin_result = run_command([uv_executable, "tool", "dir", "--bin"], check=True, capture_output=True, text=True)

    write_manifest(plan, args.manifest)
    bin_directory = bin_result.stdout.strip()
    print(f"Bub is installed. Executables: {bin_directory}")
    print(f"Selection manifest: {args.manifest}")
    if bin_directory not in os.getenv("PATH", "").split(os.pathsep):
        print(f"Add this directory to PATH and restart your shell: {bin_directory}")
    return 0


def main() -> None:
    if sys.version_info < MINIMUM_PYTHON:
        required = ".".join(map(str, MINIMUM_PYTHON))
        raise SystemExit(f"Python {required} or newer is required to install Bub.")
    try:
        raise SystemExit(run())
    except InstallerError as exc:
        raise SystemExit(f"error: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode) from exc


if __name__ == "__main__":
    main()
