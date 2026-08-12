"""Command-line entry point for Bub end-to-end cases."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .models import load_cases, select_cases
from .runner import rescore, run_cases
from .settings import HarnessSettings


def main() -> None:
    parser = argparse.ArgumentParser(prog="bub-e2e")
    subparsers = parser.add_subparsers(dest="command", required=True)

    check_parser = subparsers.add_parser("check", help="validate case manifests without running them")
    check_parser.add_argument("--manifest", type=Path, default=Path("e2e/cases"))

    run_parser = subparsers.add_parser("run", help="run selected cases through Harbor")
    run_parser.add_argument("--manifest", type=Path, default=Path("e2e/cases"))
    run_parser.add_argument("--id", action="append", default=[])
    run_parser.add_argument("--category", action="append", default=[])
    run_parser.add_argument("--output", type=Path)

    rescore_parser = subparsers.add_parser("rescore", help="evaluate preserved artifacts without rerunning Bub")
    rescore_parser.add_argument("run", type=Path)
    rescore_parser.add_argument("--output", type=Path)

    args = parser.parse_args()
    settings = HarnessSettings()
    if args.command == "check":
        cases = load_cases(args.manifest)
        print(f"validated {len(cases)} Bub e2e case(s)")
        raise SystemExit(0)
    if args.command == "rescore":
        passed = rescore(args.run, output_dir=args.output)
    else:
        cases = select_cases(load_cases(args.manifest), ids=tuple(args.id), categories=tuple(args.category))
        output = args.output.expanduser().resolve() if args.output else settings.output_path()
        passed = asyncio.run(run_cases(cases, output_dir=output, settings=settings))
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
