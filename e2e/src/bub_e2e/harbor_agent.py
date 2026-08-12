"""Harbor ACP adapter that installs Bub through supported commands."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from harbor.agents.installed import acp as harbor_acp
from harbor.environments.base import BaseEnvironment

from .models import BubDistribution, PluginSpec

AGENT_ID = "bub-e2e-acp"
REMOTE_BIN_DIR = "/installed-agent/bin"
REMOTE_BUB_HOME = "/installed-agent/bub-home"
REMOTE_BUB_PROJECT = "/installed-agent/bub-project"
REMOTE_CODEX_AUTH = "/run/bub-e2e/codex-auth.json"
REMOTE_CODEX_HOME = "/installed-agent/codex"
REMOTE_TOOL_DIR = "/installed-agent/tools"


class BubAcpAgent(harbor_acp.AcpAgent):
    """Install a declared Bub distribution and its declared plugins."""

    def __init__(
        self,
        *,
        bub: dict[str, Any],
        plugins: list[dict[str, Any]],
        **kwargs: Any,
    ) -> None:
        self._bub = BubDistribution.model_validate(bub)
        self._plugins = tuple(PluginSpec.model_validate(plugin) for plugin in plugins)
        super().__init__(
            registry_entry={
                "id": AGENT_ID,
                "name": "Bub e2e ACP",
                "version": _agent_version(self._bub),
                "description": "Bub installed from an e2e case manifest",
                "distribution": {"uvx": {"package": "bub-acp-server"}},
            },
            distribution_preference=["uvx"],
            auth_policy="disabled",
            permission_mode="allow",
            **kwargs,
        )

    async def install(self, environment: BaseEnvironment) -> None:
        await self.exec_as_root(
            environment,
            command=self._build_dependencies_command("uvx"),
            env={"DEBIAN_FRONTEND": "noninteractive"},
        )
        await self.exec_as_root(environment, command=_install_bub_command(self._bub))
        await self.exec_as_root(environment, command=_install_plugins_command(self._plugins))

        agent_user = shlex.quote(str(environment.default_user or "root"))
        await self.exec_as_root(
            environment,
            command=(
                f"chown -R {agent_user} {REMOTE_BIN_DIR} {REMOTE_BUB_HOME} {REMOTE_BUB_PROJECT} "
                f"{REMOTE_CODEX_HOME} {REMOTE_TOOL_DIR}"
            ),
        )

        launcher_path = self.logs_dir / "bub-e2e-acp-launch.sh"
        launcher_path.write_text(_launcher_script(), encoding="utf-8")
        await environment.upload_file(source_path=launcher_path, target_path=self._LAUNCHER_REMOTE_PATH)
        runner_path = Path(harbor_acp.__file__).with_name("acp_runner.py")
        await environment.upload_file(source_path=runner_path, target_path=self._RUNNER_REMOTE_PATH)
        await environment.exec(
            command=f"chmod a+rx {self._LAUNCHER_REMOTE_PATH} {self._RUNNER_REMOTE_PATH}",
            user="root",
        )
        self._selected_distribution_kind = "uvx"


def _agent_version(bub: BubDistribution) -> str:
    return bub.version or bub.commit or "unknown"


def _tool_environment() -> str:
    return f"UV_TOOL_BIN_DIR={shlex.quote(REMOTE_BIN_DIR)} UV_TOOL_DIR={shlex.quote(REMOTE_TOOL_DIR)}"


def _runtime_environment() -> str:
    return f"BUB_HOME={REMOTE_BUB_HOME} BUB_PROJECT={REMOTE_BUB_PROJECT} CODEX_HOME={REMOTE_CODEX_HOME}"


def _install_bub_command(bub: BubDistribution) -> str:
    uv = f"{harbor_acp.AcpAgent._RUNNER_VENV_PATH}/bin/uv"
    requirement = shlex.quote(bub.requirement())
    return (
        "set -eu; "
        f"mkdir -p {REMOTE_BIN_DIR} {REMOTE_BUB_HOME} {REMOTE_BUB_PROJECT} {REMOTE_CODEX_HOME}; "
        f"if [ -s {REMOTE_CODEX_AUTH} ]; then "
        f"cp {REMOTE_CODEX_AUTH} {REMOTE_CODEX_HOME}/auth.json; "
        f"chmod 600 {REMOTE_CODEX_HOME}/auth.json; "
        "fi; "
        f"{_tool_environment()} {uv} tool install --force {requirement}"
    )


def _install_plugins_command(plugins: tuple[PluginSpec, ...]) -> str:
    runner_bin = f"{harbor_acp.AcpAgent._RUNNER_VENV_PATH}/bin"
    specs = " ".join(shlex.quote(plugin.install_spec()) for plugin in plugins)
    return (
        "set -eu; "
        f"PATH={runner_bin}:{REMOTE_BIN_DIR}:$PATH {_runtime_environment()} {_tool_environment()} "
        f"{REMOTE_BIN_DIR}/bub install {specs}"
    )


def _launcher_script() -> str:
    return f"""#!/usr/bin/env sh
set -eu

bub_bin=$(dirname "$(readlink -f {REMOTE_BIN_DIR}/bub)")

collect_bub_artifacts() {{
    set +e
    mkdir -p /logs/agent
    "$bub_bin/bub" hooks > /logs/agent/bub-hooks.txt 2>&1
    hooks_status=$?
    "$bub_bin/bub" tape-export \
        --scheme fs \
        --config root=/logs/agent/tape-dataset \
        > /logs/agent/tape-export.json 2> /logs/agent/tape-export.err
    tape_status=$?
    cp {REMOTE_BUB_PROJECT}/pyproject.toml /logs/agent/bub-project.toml 2>/dev/null
    cp {REMOTE_BUB_PROJECT}/uv.lock /logs/agent/bub-project.lock 2>/dev/null
    printf '{{"hooks_status":%s,"tape_export_status":%s}}\n' "$hooks_status" "$tape_status" \
        > /logs/agent/bub-collection.json
}}

trap collect_bub_artifacts EXIT
mkdir -p /logs/agent
"$bub_bin/bub" hooks > /logs/agent/bub-hooks.txt 2>&1
"$bub_bin/bub-acp-server" "$@"
"""
