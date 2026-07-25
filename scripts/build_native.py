"""Build the deterministic Unicode Win32 launcher with x86_64 MinGW."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from scripts.release_pipeline import PeValidationError, inspect_pe, sha256_file


def _run(
    command: list[str], *, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def build_launcher(
    *,
    source_root: Path,
    output: Path,
    icon_output: Path | None = None,
    toolchain_prefix: str = "x86_64-w64-mingw32-",
) -> dict[str, Any]:
    gcc = f"{toolchain_prefix}gcc"
    windres = f"{toolchain_prefix}windres"
    for executable in (gcc, windres):
        if shutil.which(executable) is None:
            raise RuntimeError(f"required MinGW tool is unavailable: {executable}")
    machine = _run([gcc, "-dumpmachine"]).stdout.strip()
    if not machine.startswith("x86_64-w64-mingw32"):
        raise RuntimeError(f"toolchain must target x86_64-w64-mingw32, got {machine!r}")
    gcc_version = _run([gcc, "-dumpfullversion", "-dumpversion"]).stdout.strip()
    windres_version = _run([windres, "--version"]).stdout.splitlines()[0].strip()
    if not gcc_version or not windres_version:
        raise RuntimeError("MinGW toolchain version identity is unavailable")

    native = source_root.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="textsnap-native-") as temporary_name:
        temporary = Path(temporary_name)
        for filename in (
            "textsnap.rc",
            "TextSnapLayout.manifest",
        ):
            shutil.copyfile(native / filename, temporary / filename)
        # Importing the generator executes no target code and avoids relying on a
        # particular host Python executable name.
        from native.generate_icon import generate_icon

        generate_icon(temporary / "textsnap.ico")
        icon_sha256, icon_size = sha256_file(temporary / "textsnap.ico")
        if icon_output is not None:
            icon_output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(temporary / "textsnap.ico", icon_output)
        resource_object = temporary / "resources.o"
        _run(
            [
                windres,
                "--target=pe-x86-64",
                "--input-format=rc",
                "--output-format=coff",
                str(temporary / "textsnap.rc"),
                str(resource_object),
            ],
            cwd=temporary,
        )
        temporary_output = temporary / "TextSnapLayout.exe"
        _run(
            [
                gcc,
                "-std=c11",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-Os",
                "-s",
                "-municode",
                "-mwindows",
                "-fno-ident",
                f"-ffile-prefix-map={native}=.",
                "-Wl,--no-insert-timestamp",
                "-Wl,--dynamicbase",
                "-Wl,--nxcompat",
                "-Wl,--high-entropy-va",
                "-Wl,--subsystem,windows",
                "-o",
                str(temporary_output),
                str(native / "launcher.c"),
                str(resource_object),
                "-lshell32",
                "-luser32",
            ]
        )
        info = inspect_pe(temporary_output, display_path="TextSnapLayout.exe")
        if info.machine != 0x8664 or info.optional_magic != 0x20B:
            raise PeValidationError("launcher is not AMD64 PE32+")
        if info.subsystem != 2:
            raise PeValidationError("launcher does not use GUI subsystem")
        if info.timestamp != 0:
            raise PeValidationError("launcher COFF timestamp is not zero")
        shutil.copyfile(temporary_output, output)
    digest, size = sha256_file(output)
    return {
        "path": output.name,
        "sha256": digest,
        "size": size,
        "toolchain": machine,
        "gcc_version": gcc_version,
        "windres_version": windres_version,
        "machine": "AMD64",
        "format": "PE32+",
        "subsystem": "Windows GUI",
        "coff_timestamp": 0,
        "tray_icon": (
            {
                "path": icon_output.name,
                "sha256": icon_sha256,
                "size": icon_size,
            }
            if icon_output is not None
            else None
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "native",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--icon-output", type=Path)
    parser.add_argument("--toolchain-prefix", default="x86_64-w64-mingw32-")
    arguments = parser.parse_args(argv)
    result = build_launcher(
        source_root=arguments.source_root,
        output=arguments.output,
        icon_output=arguments.icon_output,
        toolchain_prefix=arguments.toolchain_prefix,
    )
    print(result["sha256"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
