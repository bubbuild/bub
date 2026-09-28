"""Install an explicit unreleased wheel in a fresh Bub environment and prove consumption."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import unquote, urlsplit
from zipfile import ZipFile


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--source-commit", required=True, help="Full Republic source commit used to build this wheel")
    parser.add_argument("--python", default="3.12", help="Bub-compatible interpreter available to uv")
    args = parser.parse_args()
    wheel = args.wheel.resolve(strict=True)
    uv = shutil.which("uv")
    if uv is None:
        parser.error("uv is required")
    if not re.fullmatch(r"[0-9a-f]{40}", args.source_commit):
        parser.error("--source-commit must be a full lowercase Git SHA")
    with ZipFile(wheel) as archive:
        metadata = BytesParser().parsebytes(
            archive.read(next(p for p in archive.namelist() if p.endswith(".dist-info/METADATA")))
        )
    wheel_version = str(metadata["Version"])
    revision = re.search(r"\+g([0-9a-f]+)(?:\.|$)", wheel_version)
    if metadata["Name"] != "republic" or revision is None or not args.source_commit.startswith(revision[1]):
        parser.error("Expected a local Republic VCS wheel matching --source-commit")
    if re.search(r"\.d[0-9]{8}(?:\.|$)", wheel_version):
        parser.error("Build Republic from a clean commit, not a dirty checkout")
    root = Path(__file__).resolve().parents[1]
    directory = Path(tempfile.mkdtemp(prefix="bub-republic-wheel-"))
    environment = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(directory / "venv")}

    def run(*command: str, capture: bool = False) -> str:
        result = subprocess.run(
            command,
            cwd=root,
            env=environment,
            check=True,
            timeout=180,
            text=True,
            stdout=subprocess.PIPE if capture else None,
        )
        return result.stdout or ""

    run(uv, "venv", "--python", args.python, str(directory / "venv"))
    constraints = directory / "constraints.txt"
    constraints.write_text(
        run(
            uv,
            "export",
            "--locked",
            "--extra",
            "trace",
            "--no-hashes",
            "--no-emit-project",
            "--no-emit-package",
            "republic",
            capture=True,
        )
    )
    python = str(directory / "venv" / "bin" / "python")
    # uv's project build cache does not key every source file. Build an explicit
    # consumer wheel so a second run cannot reuse an earlier Bub implementation.
    run(uv, "build", "--wheel", "--out-dir", str(directory / "bub-wheel"))
    bub_wheel = next((directory / "bub-wheel").glob("*.whl"))
    run(
        uv,
        "pip",
        "install",
        "--python",
        python,
        "--requirements",
        str(constraints),
        str(bub_wheel),
        str(wheel),
    )
    run(uv, "pip", "check", "--python", python)
    probe = """
import json, sys
from zipfile import ZipFile
from importlib.metadata import distribution, version, PackageNotFoundError
from importlib.util import find_spec
import bub, republic
from republic.providers.openai import OpenAIChatCompletions, OpenAIResponses
from republic.providers.anthropic import AnthropicMessages
assert '/site-packages/' in bub.__file__ and '/site-packages/' in republic.__file__
assert find_spec('any_llm') is None
try:
    distribution('any-llm-sdk')
except PackageNotFoundError:
    pass
else:
    raise AssertionError('Removed SDK must not be installed')
assert any(requirement.startswith('republic') for requirement in distribution('bub').requires)
assert 'model_backend' not in __import__('bub.builtin.settings', fromlist=['AgentSettings']).AgentSettings.model_fields
for package, artifact in [('republic', sys.argv[1]), ('bub', sys.argv[2])]:
    with ZipFile(artifact) as archive:
        for name in archive.namelist():
            if name.startswith(package + '/') and not name.endswith('/'):
                assert distribution(package).locate_file(name).read_bytes() == archive.read(name), name
print(json.dumps({'bub_import': bub.__file__, 'republic_import': republic.__file__,
                  'version': version('republic'), 'direct_url': json.loads(distribution('republic').read_text('direct_url.json'))}))
"""
    installed = json.loads(run(python, "-I", "-c", probe, str(wheel), str(bub_wheel), capture=True))
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    installed_url = urlsplit(installed["direct_url"]["url"])
    if (
        installed["version"] != wheel_version
        or installed_url.scheme != "file"
        or installed_url.netloc
        or Path(unquote(installed_url.path)) != wheel
    ):
        raise RuntimeError("Installed distribution does not match the selected wheel")
    run(
        python,
        "-I",
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "--basetemp",
        str(directory / "tests"),
        "tests",
    )
    report = {
        "source_commit": args.source_commit,
        "bub_wheel": str(bub_wheel),
        "bub_wheel_sha256": hashlib.sha256(bub_wheel.read_bytes()).hexdigest(),
        "wheel": str(wheel),
        "sha256": digest,
        "installed": installed,
        "removed_sdk_absent": True,
        "dependency_check": "uv pip check",
        "evidence": "official SDK / HTTP fixtures, not live service acceptance",
    }
    path = directory / "report.json"
    path.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Installed-wheel integration evidence: {path}")


if __name__ == "__main__":
    main()
