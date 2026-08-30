from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
INSTALLER_PATH = ROOT / "install-bub.py"


def load_installer():
    spec = importlib.util.spec_from_file_location("install_bub", INSTALLER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


installer = load_installer()


def test_chat_plan_contains_only_surface_independent_selection() -> None:
    plan = installer.build_plan("chat", ["web-search"])

    assert plan.required_plugins == ()
    assert plan.optional_plugins == ("bub-web-search",)
    assert plan.injected_requirements == ("uv", "bub-web-search")


def test_lody_plan_always_contains_acp_server() -> None:
    plan = installer.build_plan("lody", ["mcp-tools"])

    assert plan.required_plugins == ("bub-acp-server",)
    assert plan.optional_plugins == ("bub-mcp",)
    assert plan.command() == (
        "uv",
        "tool",
        "install",
        "--force",
        "--python",
        "3.12",
        "--with",
        "uv",
        "--with",
        "bub-acp-server",
        "--with",
        "bub-mcp",
        "bub",
    )


def test_plan_deduplicates_exact_requirements() -> None:
    plan = installer.build_plan("lody", extra_requirements=["bub-acp-server", "uv", "uv"])

    assert plan.injected_requirements == ("uv", "bub-acp-server")


def test_unknown_optional_plugin_is_rejected() -> None:
    with pytest.raises(installer.InstallerError, match="Unknown optional plugin"):
        installer.build_plan("chat", ["acp-server"])


def test_optional_plugin_prompt_toggles_until_confirmed() -> None:
    input_stream = io.StringIO("1,3\n3\n\n")
    output_stream = io.StringIO()

    selected = installer.toggle_optional_plugins((), input_stream, output_stream)

    assert selected == ("web-search",)
    assert output_stream.getvalue().count("Optional plugins") == 3


def test_preset_prompt_rejects_zero_instead_of_selecting_last_item() -> None:
    input_stream = io.StringIO("0\n2\n")
    output_stream = io.StringIO()

    selected = installer.choose_preset(input_stream, output_stream)

    assert selected == "lody"
    assert "Enter one of the listed numbers." in output_stream.getvalue()


def test_manifest_records_intent_separately_from_uv_receipt(tmp_path: Path) -> None:
    plan = installer.build_plan("lody", ["web-search"], ["example-plugin>=1"])
    manifest = tmp_path / "bub" / "install.json"

    installer.write_manifest(plan, manifest)

    payload = json.loads(manifest.read_text())
    assert payload == {
        "schema": 1,
        "preset": "lody",
        "bub_requirement": "bub",
        "python": "3.12",
        "required_plugins": ["bub-acp-server"],
        "optional_plugins": ["bub-web-search"],
        "extra_requirements": ["example-plugin>=1"],
    }


def test_non_interactive_dry_run_does_not_resolve_uv(capsys: pytest.CaptureFixture[str]) -> None:
    result = installer.run([
        "--preset",
        "lody",
        "--plugin",
        "web-search",
        "--no-interactive",
        "--dry-run",
    ])

    assert result == 0
    assert capsys.readouterr().out.strip() == (
        "uv tool install --force --python 3.12 --with uv --with bub-acp-server --with bub-web-search bub"
    )
