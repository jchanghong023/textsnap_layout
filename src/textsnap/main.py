# ruff: noqa: E402
import sys

sys.dont_write_bytecode = True

from collections.abc import Sequence

from textsnap.bootstrap import run_application
from textsnap.paths import BundlePaths


def main(
    paths: BundlePaths | None = None,
    arguments: Sequence[str] | None = None,
    *,
    entry_script: str | None = None,
) -> int:
    """Run using an explicitly located portable bundle and argument sequence."""

    if paths is not None and entry_script is not None:
        raise ValueError("provide paths or entry_script, not both")
    if paths is None:
        script = sys.argv[0] if entry_script is None else entry_script
        paths = BundlePaths.from_entry_script(script)
    selected_arguments = tuple(sys.argv[1:]) if arguments is None else tuple(arguments)
    return run_application(paths, selected_arguments)


if __name__ == "__main__":
    raise SystemExit(main())
