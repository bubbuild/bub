"""Release helpers: pin bub to the bubling released with it, then check the built distributions.

python scripts/release.py pin 0.6.0a2
python scripts/release.py check 0.6.0a2 dist
"""

from __future__ import annotations

import argparse
import re
import sys
import tarfile
import tomllib
import zipfile
from email.parser import Parser
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"
BUBLING_REQUIREMENT = re.compile(r"bubling(\[[^\]]*\])?(?=[\s<>=!~;]|$)")


def pin(version: str) -> None:
    text = PYPROJECT.read_text(encoding="utf-8")
    project = tomllib.loads(text)["project"]
    requirements = [
        *project["dependencies"],
        *(r for extra in project["optional-dependencies"].values() for r in extra),
    ]
    count = 0
    for requirement in dict.fromkeys(requirements):
        if match := BUBLING_REQUIREMENT.match(requirement):
            text = text.replace(f'"{requirement}"', f'"bubling{match.group(1) or ""}=={version}"')
            count += 1
    if count == 0:
        sys.exit(f"No bubling requirement found in {PYPROJECT}")
    PYPROJECT.write_text(text, encoding="utf-8")
    print(f"Pinned {count} bubling requirement(s) to =={version}")


def _metadata(path: Path) -> dict[str, list[str]]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as wheel:
            name = next(n for n in wheel.namelist() if n.endswith(".dist-info/METADATA"))
            raw = wheel.read(name).decode()
    else:
        with tarfile.open(path) as sdist:
            member = next(m for m in sdist.getmembers() if m.name.count("/") == 1 and m.name.endswith("/PKG-INFO"))
            raw = sdist.extractfile(member).read().decode()  # type: ignore[union-attr]
    message = Parser().parsestr(raw)
    return {key: message.get_all(key) or [] for key in ("Name", "Version", "Requires-Dist")}


def check(version: str, dist: Path) -> None:
    errors: list[str] = []
    seen: set[str] = set()
    for path in sorted([*dist.glob("*.whl"), *dist.glob("*.tar.gz")]):
        metadata = _metadata(path)
        name, found = metadata["Name"][0], metadata["Version"][0]
        seen.add(name)
        if found != version:
            errors.append(f"{path.name}: version {found} != {version}")
        if name == "bub":
            bubling = [req for req in metadata["Requires-Dist"] if req.startswith("bubling")]
            unpinned = [req for req in bubling if f"=={version}" not in req]
            if not bubling or unpinned:
                errors.append(f"{path.name}: bubling is not pinned to =={version}: {bubling}")
    if missing := {"bub", "bubling"} - seen:
        errors.append(f"No distributions of {sorted(missing)} in {dist}")
    if errors:
        sys.exit("\n".join(errors))
    print(f"bub and bubling {version} are built and bub requires bubling=={version}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("pin").add_argument("version")
    checker = commands.add_parser("check")
    checker.add_argument("version")
    checker.add_argument("dist", type=Path)
    args = parser.parse_args()
    if args.command == "pin":
        pin(args.version)
    else:
        check(args.version, args.dist)


if __name__ == "__main__":
    main()
