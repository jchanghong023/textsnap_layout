"""Unified non-interactive CLI for the Windows x64 portable release."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from scripts.release_pipeline import (
    PRODUCT_NAME,
    PRODUCT_VERSION,
    LockSet,
    PipelineError,
    build_deterministic_zip,
    fetch_locked_inputs,
    stage_portable_tree,
    static_verify_staging,
    validate_lock_set,
    validate_zip_archive,
    validate_wheel_closure,
)


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent


def _common_lock_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--lock-dir",
        type=Path,
        default=REPOSITORY_ROOT / "vendor-lock",
    )


def _common_cache_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cache-dir", type=Path, required=True)


def _stage_arguments(parser: argparse.ArgumentParser) -> None:
    _common_lock_argument(parser)
    _common_cache_argument(parser)
    parser.add_argument("--stage-dir", type=Path, required=True)
    parser.add_argument(
        "--source-package",
        type=Path,
        default=REPOSITORY_ROOT / "src" / "textsnap",
    )
    parser.add_argument(
        "--entry-script",
        type=Path,
        default=REPOSITORY_ROOT / "src" / "textsnap" / "main.py",
    )
    parser.add_argument(
        "--readme",
        type=Path,
        default=REPOSITORY_ROOT / "README.zh-CN.md",
    )
    parser.add_argument(
        "--python-for-bytecode",
        type=Path,
        required=True,
        help="exact CPython 3.13.14 host executable",
    )
    parser.add_argument(
        "--native-source",
        type=Path,
        default=REPOSITORY_ROOT / "native",
    )
    parser.add_argument(
        "--toolchain-prefix",
        default="x86_64-w64-mingw32-",
    )
    parser.add_argument(
        "--profile",
        choices=("private-use",),
        default="private-use",
    )


def _stage_from_arguments(arguments: argparse.Namespace) -> dict[str, Any]:
    return stage_portable_tree(
        locks=LockSet.load(arguments.lock_dir),
        cache_root=arguments.cache_dir.resolve(),
        output_stage=arguments.stage_dir.resolve(),
        source_package=arguments.source_package.resolve(),
        entry_script=arguments.entry_script.resolve(),
        readme=arguments.readme.resolve() if arguments.readme else None,
        python_for_bytecode=arguments.python_for_bytecode.resolve(),
        native_source=arguments.native_source.resolve(),
        toolchain_prefix=arguments.toolchain_prefix,
        profile=arguments.profile,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build TextSnap Layout from exact Windows locks without executing "
            "target code"
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate-lock")
    _common_lock_argument(validate)

    fetch = subparsers.add_parser("fetch")
    _common_lock_argument(fetch)
    _common_cache_argument(fetch)

    closure = subparsers.add_parser("validate-wheel-closure")
    _common_lock_argument(closure)
    _common_cache_argument(closure)

    stage = subparsers.add_parser("stage")
    _stage_arguments(stage)

    verify = subparsers.add_parser("verify")
    _common_lock_argument(verify)
    verify.add_argument("--stage-dir", type=Path, required=True)

    package = subparsers.add_parser(
        "package",
        description="Package a saved profile=private-use staging",
    )
    _common_lock_argument(package)
    package.add_argument("--stage-dir", type=Path, required=True)
    package.add_argument("--output-dir", type=Path, required=True)

    all_command = subparsers.add_parser("all")
    _stage_arguments(all_command)
    all_command.add_argument("--output-dir", type=Path, required=True)
    return parser


def _run(arguments: argparse.Namespace) -> dict[str, Any]:
    if arguments.command == "validate-lock":
        return validate_lock_set(LockSet.load(arguments.lock_dir))
    if arguments.command == "fetch":
        locks = LockSet.load(arguments.lock_dir)
        return {
            "lock": validate_lock_set(locks),
            "fetched": fetch_locked_inputs(locks, arguments.cache_dir.resolve()),
        }
    if arguments.command == "validate-wheel-closure":
        return validate_wheel_closure(
            LockSet.load(arguments.lock_dir), arguments.cache_dir.resolve()
        )
    if arguments.command == "stage":
        return _stage_from_arguments(arguments)
    if arguments.command == "verify":
        return static_verify_staging(
            locks=LockSet.load(arguments.lock_dir),
            stage_root=arguments.stage_dir.resolve(),
        )
    if arguments.command == "package":
        stage = arguments.stage_dir.resolve()
        locks = LockSet.load(arguments.lock_dir)
        output = (
            arguments.output_dir.resolve()
            / f"{PRODUCT_NAME}-{PRODUCT_VERSION}-win-x64.zip"
        )
        result = build_deterministic_zip(stage, output, locks=locks)
        result["validation"] = validate_zip_archive(output)
        return result
    if arguments.command == "all":
        locks = LockSet.load(arguments.lock_dir)
        fetched = fetch_locked_inputs(locks, arguments.cache_dir.resolve())
        staged = _stage_from_arguments(arguments)
        result: dict[str, Any] = {"fetched": fetched, "staged": staged}
        output = (
            arguments.output_dir.resolve()
            / f"{PRODUCT_NAME}-{PRODUCT_VERSION}-win-x64.zip"
        )
        result["package"] = build_deterministic_zip(
            arguments.stage_dir.resolve(),
            output,
            locks=locks,
        )
        result["package"]["validation"] = validate_zip_archive(output)
        return result
    raise AssertionError(arguments.command)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    try:
        result = _run(arguments)
    except (PipelineError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": type(exc).__name__,
                    "message": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps({"ok": True, "result": result}, ensure_ascii=False, sort_keys=True)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
